# SPDX-License-Identifier: MIT
"""Strict preparation policy parsing and execution override rejection without services."""

import importlib.util
from pathlib import Path
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("httpd_settings", ROOT / "scripts/ironic-httpd-settings.py")
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)


def test_missing_file_and_legacy_preflight_have_disabled_defaults(tmp_path):
    assert policy.read(tmp_path / "absent.yml") == policy.DEFAULTS
    policy.verify_preflight({}, {})


@pytest.mark.parametrize("value", [None, [], {"unknown": True},
    {"ironic_deploy_httpd_tuning": "true"}, {"ironic_deploy_httpd_tuning": 1},
    {"ironic_deploy_httpd_manage_sysctl": "false"}, {"ironic_deploy_httpd_manage_sysctl": True}])
def test_reject_invalid_types_and_unapproved_sysctl(value):
    with pytest.raises(ValueError):
        policy.normalize(value)


@pytest.mark.parametrize("path", ["relative", "/", "/tmp/../httpd", "/tmp/./httpd", "/tmp//httpd",
    "/tmp/httpd:rw", "/tmp/httpd path", "/tmp/httpd\n", None])
def test_reject_unsafe_mount_paths(path):
    with pytest.raises(ValueError):
        policy.normalize({"ironic_deploy_httpd_tuning_path": path})


def test_symlink_policy_is_rejected(tmp_path):
    target = tmp_path / "regular.yml"
    target.write_text("{}")
    link = tmp_path / "link.yml"
    link.symlink_to(target)
    with pytest.raises(ValueError):
        policy.read(link)


def test_enabled_profile_is_separate_from_host_settings(tmp_path):
    path = tmp_path / "ironic-httpd.yml"
    path.write_text("ironic_deploy_httpd_tuning: true\n")
    actual = policy.read(path)
    assert actual["ironic_deploy_httpd_tuning"] is True
    assert actual["ironic_deploy_httpd_manage_sysctl"] is False
    with pytest.raises(ValueError):
        policy.verify_preflight({}, actual)


@pytest.mark.parametrize("adapter,expression", [
    ("inspect-fleet.yml", "inspect_fleet_plan.httpd_tuning"),
    ("wipe-fleet-install-disks.yml", "fleet_wipe_runtime_plan.httpd_tuning")])
@pytest.mark.parametrize("legacy", [False, True])
def test_actual_adapter_guards_reject_injected_execution_options(tmp_path, adapter, expression, legacy):
    source = yaml.safe_load((ROOT / "playbooks" / adapter).read_text())[0]
    guard = next(t for t in source["tasks"] if t["name"] == "Require effective HTTPD settings match the sealed preparation")
    plan_var = expression.split(".")[0]
    snapshot = policy.normalize({"ironic_deploy_httpd_tuning": True}) if not legacy else policy.DEFAULTS
    plays = []
    for key in policy.DEFAULTS:
        variables = {plan_var: {} if legacy else {"httpd_tuning": snapshot}, **snapshot}
        variables[key] = not snapshot[key] if type(snapshot[key]) is bool else "/var/tmp/changed"
        plays.append({"name": "Reject override of " + key, "hosts": "localhost", "gather_facts": False,
            "vars": variables, "tasks": [{"name": "Exercise actual guard", "block": [guard,
                {"name": "Reject acceptance", "ansible.builtin.fail": {"msg": "Unexpected acceptance"}}],
                "rescue": [{"name": "Require guard rejection", "ansible.builtin.assert": {"that": [
                    "ansible_failed_result.msg != 'Unexpected acceptance'",
                    "ansible_failed_task.name == 'Require effective HTTPD settings match the sealed preparation'"]}}]}]})
    path = tmp_path / "guard.yml"
    path.write_text(yaml.safe_dump(plays, sort_keys=False))
    result = subprocess.run(["ansible-playbook", "-i", "localhost,", str(path)], capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
