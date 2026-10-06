# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""Contract and local execution tests for the controlled inspection driver."""

import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml


NET_CONFIGURATOR = Path(__file__).resolve().parent.parent


def _load_init_settings_module():
    spec = importlib.util.spec_from_file_location(
        "init_ocp_settings", NET_CONFIGURATOR / "scripts" / "init-ocp-settings.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generated_ocp_settings_do_not_require_manual_inspection_contract(tmp_path):
    output = tmp_path / "ocp-settings.yml"
    _load_init_settings_module().write_settings(
        output, "2-8-5-200", "example", "  control_plane:\n    - node-01"
    )

    settings = yaml.safe_load(output.read_text())

    assert "inspection" not in settings


def test_workbook_ocp_version_adapter_requires_exact_three_part_version(monkeypatch):
    module = _load_init_settings_module()
    monkeypatch.setattr(module, "read_ocp_settings", lambda _path: {"ocp_version": "4.22.8"})

    assert module.read_required_ocp_version(Path("ignored.xlsx")) == "4.22.8"

    monkeypatch.setattr(module, "read_ocp_settings", lambda _path: {"ocp_version": "4.22"})
    try:
        module.read_required_ocp_version(Path("ignored.xlsx"))
    except ValueError as exc:
        assert "exact three-part" in str(exc)
    else:
        raise AssertionError("an incomplete workbook version must be rejected")


def test_cpu_inband_vip_source_excludes_support_vlan():
    module = _load_init_settings_module()

    subnet, gateway = module.find_cpu_inband_vlan([
        {"name": "Support", "subnet": "10.78.220.0/24", "gateway": "10.78.220.1"},
        {"name": "CPU/In-Band", "subnet": "10.78.221.0/24", "gateway": "10.78.221.1"},
    ])

    assert (subnet, gateway) == ("10.78.221.0/24", "10.78.221.1")
    assert module.suggest_vips(subnet) == ("10.78.221.254", "10.78.221.253")


def test_generated_vip_comment_names_cpu_inband_subnet(tmp_path):
    output = tmp_path / "ocp-settings.yml"
    _load_init_settings_module().write_settings(
        output, "2-8-5-200", "example", "  control_plane:\n    - node-01",
        api_vip="10.78.221.254", ingress_vip="10.78.221.253",
    )

    rendered = output.read_text()

    assert "suggested from CPU/in-band subnet" in rendered
    assert "suggested from support subnet" not in rendered


def test_driver_uses_shared_vault_and_collection_role_without_bmc_credentials():
    playbook = yaml.safe_load(
        (NET_CONFIGURATOR / "playbooks" / "prepare-inspection-preflight.yml").read_text()
    )[0]
    task_names = [task["name"] for task in playbook["tasks"]]
    rendered = (NET_CONFIGURATOR / "playbooks" / "prepare-inspection-preflight.yml").read_text()

    assert "../.era-secrets/air-secrets.yml" in rendered
    assert "Require shared-vault OCP pull secret" in task_names
    assert "Materialize local pull secret for the collection role" in task_names
    assert "rhvp.baremetal_ocp.inspection_preflight_prepare" in rendered
    assert "ip\n          - -j\n          - route\n          - get" in rendered
    assert "inspection_preflight_prepare_expected_ocp_version" in rendered
    assert "inspection_preflight_expected_ocp_version" in rendered
    assert "inspection_preflight_collection_cluster_vars_path is match('^/')" in rendered
    assert "inspection_preflight_nmstate_dir is match('^/')" in rendered
    assert "inspection_preflight_early_network_path is match('^/')" in rendered
    assert "inspection_preflight_output_path is match('^/')" in rendered
    assert "inspection_preflight_ocp_settings_path" not in rendered
    assert "secrets.yaml" not in rendered
    assert "Redfish" not in rendered


def test_make_target_wires_operational_driver_and_collection_location():
    makefile = (NET_CONFIGURATOR / "Makefile").read_text()

    assert "prepare-inspection-preflight:" in makefile
    assert "RHV_BAREMETAL_OCP_COLLECTIONS_PATH" in makefile
    assert "playbooks/prepare-inspection-preflight.yml" in makefile
    assert "preflight-vars.yaml" in makefile
    assert "--print-ocp-version" in makefile
    assert "inspection_preflight_expected_ocp_version=$$OCP_VERSION" in makefile
    assert 'NMSTATE="$(CURDIR)/output/$(ARCH)/$(SITE)/ocp/inspection/nmstate/' in makefile
    assert 'EARLY_NETWORK="$(CURDIR)/output/$(ARCH)/$(SITE)/ocp/inspection/early-network/' in makefile
    assert "inspection_preflight_early_network_path=$$EARLY_NETWORK" in makefile
    assert "ocp-settings.yml\";" not in makefile
    assert "else VAULT_ARGS+=(--ask-vault-pass); fi;" in makefile


def test_gate6_driver_requires_matching_authorization_and_new_durable_report():
    driver_path = NET_CONFIGURATOR / "playbooks" / "inspect-controlled-candidate.yml"
    driver = yaml.safe_load(driver_path.read_text())
    rendered = driver_path.read_text()
    task_names = [task["name"] for task in driver[0]["tasks"]]

    assert "controlled_inspection_authorize_physical_boot == controlled_inspection_candidate" in rendered
    assert "controlled_inspection_report_path is match('^/')" in rendered
    assert "controlled_inspection_report_path is match('^/tmp(?:/|$)')" in rendered
    assert "preflight_inspection_nodes | default([]) | length == 1" in rendered
    assert "Refuse to overwrite prior inspection evidence" in task_names
    assert driver[1]["import_playbook"] == "{{ controlled_inspection_collection_playbook }}"
    assert driver[1]["vars"]["inspect_cluster_nodes"] == "{{ preflight_inspection_nodes }}"


def test_make_target_wires_gate6_driver_and_explicit_operator_contract():
    makefile = (NET_CONFIGURATOR / "Makefile").read_text()

    assert "inspect-controlled-candidate:" in makefile
    assert "playbooks/inspect-controlled-candidate.yml" in makefile
    assert "INSPECTION_AUTHORIZE_PHYSICAL_BOOT" in makefile
    assert "INSPECTION_REPORT_PATH must be absolute" in makefile
    assert "INSPECTION_REPORT_PATH must not be under /tmp" in makefile
    assert "--ask-vault-pass" in makefile


@pytest.fixture
def durable_workspace():
    with tempfile.TemporaryDirectory(prefix="inspection-make-", dir="/var/tmp") as directory:
        yield Path(directory)


@pytest.mark.parametrize("mode", ["default", "inventory_only", "explicit"])
def test_make_resolves_all_artifact_paths_without_running_ansible(durable_workspace, mode):
    """Execute the real recipe with a capture executable, never a physical play."""
    tmp_path = durable_workspace
    shutil.copyfile(NET_CONFIGURATOR / "Makefile", tmp_path / "Makefile")
    (tmp_path / "input" / "2-8-5-200").mkdir(parents=True)
    collection = tmp_path / "collection"
    inventory = collection / "inventories" / "test-site"
    inventory.mkdir(parents=True)
    for name in ("hosts.yml", "secrets.yaml", "preflight-vars.yaml"):
        (inventory / name).touch()
    (collection / "playbooks").mkdir()
    (collection / "playbooks" / "inspect_cluster.yml").touch()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    capture = tmp_path / "arguments.json"
    executable = bin_dir / "ansible-playbook"
    executable.write_text(
        "#!/usr/bin/env python3\nimport json, os, sys\n"
        "from pathlib import Path\n"
        "Path(os.environ['INSPECTION_TEST_CAPTURE']).write_text(json.dumps(sys.argv[1:]))\n"
    )
    executable.chmod(0o700)
    command = [
        "make", "inspect-controlled-candidate", "ARCH=2-8-5-200", "SITE=test-site",
        "INSPECTION_CANDIDATE=node-01", "INSPECTION_AUTHORIZE_PHYSICAL_BOOT=node-01",
        f"RHV_BAREMETAL_OCP_ROOT={collection}",
    ]
    report = tmp_path / "output/2-8-5-200/test-site/reports/inspection/test/node-01-inventory.yaml"
    failure = report.with_name("node-01-failure.yaml")
    cleanup = report.with_name("node-01-cleanup.yaml")
    if mode != "default":
        report = tmp_path / "custom/node-01-inventory.yaml"
        failure = report.with_name("node-01-failure.yaml")
        cleanup = report.with_name("node-01-cleanup.yaml")
        command.append(f"INSPECTION_REPORT_PATH={report}")
    if mode == "explicit":
        failure = tmp_path / "other/failure.yaml"
        cleanup = tmp_path / "other/cleanup.yaml"
        command += [f"INSPECTION_FAILURE_REPORT_PATH={failure}",
                    f"INSPECTION_CLEANUP_REPORT_PATH={cleanup}"]
    result = subprocess.run(command, cwd=tmp_path, text=True, capture_output=True,
                            env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
                                 "INSPECTION_TEST_CAPTURE": str(capture)})
    assert result.returncode == 0, result.stdout + result.stderr
    arguments = json.loads(capture.read_text())
    assert f"controlled_inspection_report_path={report}" in arguments
    assert f"controlled_inspection_failure_report_path={failure}" in arguments
    assert f"controlled_inspection_cleanup_report_path={cleanup}" in arguments


@pytest.mark.parametrize("case", [
    "default", "generic_stem", "explicit", "inventory_exists", "failure_exists", "cleanup_exists",
    "same_path", "parentheses", "wrong_extension", "temporary_path",
])
def test_adapter_artifact_guard_before_collection_entry(case):
    """Run the actual adapter against a harmless imported collection fixture."""
    with tempfile.TemporaryDirectory(prefix="inspection-adapter-", dir="/var/tmp") as directory:
        workspace = Path(directory)
        marker = workspace / "collection-entered.json"
        collection = workspace / "inspect_cluster.yml"
        collection.write_text(yaml.safe_dump([{
            "name": "Capture adapter handoff without hardware",
            "hosts": "bootstrap", "gather_facts": False,
            "tasks": [{"name": "Record collection entry and resolved paths",
                       "ansible.builtin.copy": {
                           "dest": str(marker), "mode": "0600",
                           "content": "{{ [inspect_cluster_report_path, "
                                      "inspect_cluster_failure_report_path, "
                                      "inspect_cluster_cleanup_report_path] | to_json }}"}}],
        }], sort_keys=False))
        report = workspace / "node-01-inventory.yaml"
        failure = workspace / "node-01-failure.yaml"
        cleanup = workspace / "node-01-cleanup.yaml"
        inputs = {
            "controlled_inspection_candidate": "node-01",
            "controlled_inspection_authorize_physical_boot": "node-01",
            "controlled_inspection_collection_playbook": str(collection),
            "preflight_inspection_nodes": [{"name": "node-01"}],
            "ironic_image_resolve_ocp_release_image": "fixture-release",
            "ironic_image_resolve_pull_secret_path": "fixture-auth-path",
            "ironic_preprovisioning_media_nmstate_dir": "fixture-nmstate",
            "ironic_deploy_provisioning_ip": "192.0.2.1",
            "ironic_deploy_provisioning_interface": "fixture-interface",
        }
        if case == "generic_stem":
            report = workspace / "custom-report.yaml"
            failure = workspace / "custom-report-failure.yaml"
            cleanup = workspace / "custom-report-cleanup.yaml"
        elif case == "explicit":
            report = workspace / "custom-inventory.yaml"
            failure = workspace / "explicit-failure.yaml"
            cleanup = workspace / "explicit-cleanup.yaml"
            inputs.update(controlled_inspection_failure_report_path=str(failure),
                          controlled_inspection_cleanup_report_path=str(cleanup))
        elif case.endswith("_exists"):
            {"inventory_exists": report, "failure_exists": failure,
             "cleanup_exists": cleanup}[case].write_text("retained evidence")
        elif case == "same_path":
            inputs["controlled_inspection_cleanup_report_path"] = str(report)
        elif case == "parentheses":
            report = workspace / "node-(date-inventory.yaml"
        elif case == "wrong_extension":
            report = workspace / "node-inventory.yml"
        elif case == "temporary_path":
            report = Path("/tmp/node-01-inventory.yaml")
        inputs["controlled_inspection_report_path"] = str(report)
        extra_vars = workspace / "inputs.json"
        extra_vars.write_text(json.dumps(inputs))
        inventory = workspace / "hosts.yaml"
        inventory.write_text(yaml.safe_dump({"all": {"children": {"bootstrap": {
            "hosts": {"fixture-bootstrap": {"ansible_connection": "local"}},
        }}}}))
        result = subprocess.run([
            "ansible-playbook", "-i", str(inventory), "-c", "local",
            str(NET_CONFIGURATOR / "playbooks/inspect-controlled-candidate.yml"),
            "-e", f"@{extra_vars}",
        ], text=True, capture_output=True, timeout=60)
        if case in ("default", "generic_stem", "explicit"):
            assert result.returncode == 0, result.stdout + result.stderr
            assert json.loads(marker.read_text()) == [str(report), str(failure), str(cleanup)]
        else:
            assert result.returncode != 0
            assert not marker.exists(), "collection entered despite invalid/existing artifacts"
            if case.endswith("_exists"):
                existing = {"inventory_exists": report, "failure_exists": failure,
                            "cleanup_exists": cleanup}[case]
                assert existing.read_text() == "retained evidence"
                assert "Inspection destination already exists" in result.stdout
