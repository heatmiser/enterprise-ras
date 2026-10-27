# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""Verify operator restart scope, native probe boundaries and preserved terminal evidence."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

from tests.test_fleet_cleaning_state import terminal
from tests.test_fleet_wipe_adapter import wipe_source as inherited_wipe_source, workspace, fresh_preflight
from tests.test_abi_eligibility_handoff import eligible_run, candidate_workspace
from tests.test_fleet_inspection_adapter import NAMES, ROOT


SPEC = importlib.util.spec_from_file_location("restart_adapter", ROOT / "scripts/restart-fleet-wipe.py")
restart = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(restart)
OLD = "20000101T130000Z"
NEW = "20000101T140000Z"
wipe_source = inherited_wipe_source


@pytest.fixture
def operation(wipe_source, monkeypatch):
    base, folder, bundle = terminal(wipe_source, "FAILED")
    history = {str(path.relative_to(folder)): path.read_bytes() for path in folder.rglob("*") if path.is_file()}
    sealed = {str(path.relative_to(bundle)): path.read_bytes() for path in bundle.rglob("*") if path.is_file()}
    monkeypatch.setattr(restart, "new_run_id", lambda: NEW)
    calls = []

    def probe(*args):
        calls.append("probe")

    def preflight(root, argv, collections):
        calls.append(argv)
        assert "wipe-fleet-disks" not in argv
        runtime = Path(argv[argv.index("-e") + 1][1:])
        fresh_preflight(wipe_source, runtime.parent)
        restart.wipe.seal(root, "2-8-5-200", "test01", runtime.parent)

    monkeypatch.setattr(restart, "probe", probe)
    monkeypatch.setattr(restart, "run_command", preflight)
    return wipe_source, base, folder, history, bundle, sealed, calls


def invoke(operation, concurrency=2, confirm=lambda _: "yes", source=OLD):
    source_data = operation[0]
    return restart.restart(source_data[0], "2-8-5-200", "test01", source, concurrency,
                           source_data[3], str(source_data[3].parent), confirm=confirm)


def assert_history(operation):
    _, _, folder, history, bundle, sealed, _ = operation
    assert history == {name: (folder / name).read_bytes() for name in history}
    assert sealed == {str(path.relative_to(bundle)): path.read_bytes()
                      for path in bundle.rglob("*") if path.is_file()}


def test_prepares_recorded_fleet_and_prints_exact_authorization_without_execution(operation, capsys):
    bundle = invoke(operation)
    source_data, base, folder, _, _, _, calls = operation
    plan, _ = restart.wipe.verify(source_data[0], "2-8-5-200", "test01", bundle)
    assert plan["run_id"] == NEW and plan["concurrency_limit"] == 2
    assert sorted(node["name"] for node in plan["authorized_nodes"]) == sorted(NAMES)
    assert [step["step"] for step in plan["clean_steps"]] == ["clean_uefi_nvram", "erase_devices_metadata"]
    restart.state.check(base)
    disposition = restart.state.read(folder / "disposition.yaml")
    assert disposition["receipt_sha256"] == restart.state.digest(folder / "receipt.yaml")
    assert "Operator confirmed" in disposition["reason"]
    assert not (base / "reports/wipe" / NEW).exists()
    assert set(restart.state.registry(base)["runs"]) == {OLD}
    assert calls[0] == "probe" and len(calls) == 2
    output = capsys.readouterr().out
    assert "make wipe-fleet-disks" in output and "FLEET_AUTHORIZE_WIPE_RUN=" + NEW in output
    assert "FLEET_AUTHORIZE_WIPE_NODES=" + ",".join(sorted(NAMES)) in output
    assert_history(operation)


def test_cancelled_disposition_writes_no_restart_or_disposition(operation):
    with pytest.raises(ValueError, match="cancelled"):
        invoke(operation, confirm=lambda _: "no")
    assert not (operation[2] / "disposition.yaml").exists()
    assert not (operation[1] / "ocp/wipe/restarts").exists()
    assert operation[-1] == []
    assert_history(operation)


@pytest.mark.parametrize("limit", [0, -1, 99, True])
def test_invalid_concurrency_is_rejected_before_any_preparation(operation, limit):
    with pytest.raises(ValueError, match="Concurrency"):
        invoke(operation, concurrency=limit)
    assert operation[-1] == []
    assert not (operation[2] / "disposition.yaml").exists()
    assert_history(operation)


@pytest.mark.parametrize("case", ["tampered", "retained", "orphan", "started", "disposition"])
def test_invalid_or_unsettled_prior_evidence_blocks_without_disposition(operation, case):
    _, base, folder, _, _, _, calls = operation
    if case == "tampered":
        (folder / NAMES[0] / "outcome.yaml").write_text("changed")
    elif case == "retained":
        runtime = restart.state.read(folder / "runtime.yaml")
        runtime["runtime_retained"] = True
        restart.state.write(folder / "runtime.yaml", runtime)
        receipt = restart.state.read(folder / "receipt.yaml")
        receipt["evidence_sha256"]["runtime.yaml"] = restart.state.digest(folder / "runtime.yaml")
        restart.state.write(folder / "receipt.yaml", receipt)
    elif case == "orphan":
        (base / "reports/wipe/20000101T150000Z").mkdir()
    elif case == "started":
        (folder / "receipt.yaml").unlink()
    else:
        restart.state.write(folder / "disposition.yaml", {"action": "wrong"})
    with pytest.raises(ValueError):
        invoke(operation)
    assert calls == []
    assert not (base / "ocp/wipe/restarts").exists()


def test_probe_failure_retains_id_without_disposition_and_retry_reuses_it(operation, monkeypatch):
    original = restart.probe

    def fail(*args):
        raise ValueError("native startup failed")

    monkeypatch.setattr(restart, "probe", fail)
    with pytest.raises(ValueError, match="native startup"):
        invoke(operation)
    record = operation[1] / "ocp/wipe/restarts" / (OLD + ".yaml")
    assert restart.state.read(record)["run_id"] == NEW
    assert not (operation[2] / "disposition.yaml").exists()
    assert not (operation[1] / "ocp/wipe/fleet" / NEW).exists()
    monkeypatch.setattr(restart, "new_run_id", lambda: "20000101T160000Z")
    monkeypatch.setattr(restart, "probe", original)
    assert invoke(operation).name == NEW
    assert_history(operation)


def test_preflight_failure_resumes_same_seed_without_overwriting_historical_evidence(operation, monkeypatch):
    original = restart.run_command

    def fail(*args):
        raise ValueError("preflight failed")

    monkeypatch.setattr(restart, "run_command", fail)
    with pytest.raises(ValueError, match="preflight failed"):
        invoke(operation)
    seed = operation[1] / "ocp/wipe/fleet" / NEW
    before = (seed / "plan.yml").read_bytes()
    monkeypatch.setattr(restart, "run_command", original)
    assert invoke(operation).name == NEW
    assert (seed / "plan.yml").read_bytes() == before
    assert_history(operation)


def test_completed_preparation_revalidates_without_repeating_probe_or_rewriting(operation):
    bundle = invoke(operation)
    before = {str(p.relative_to(bundle)): p.read_bytes() for p in bundle.rglob("*") if p.is_file()}
    calls = list(operation[-1])
    assert invoke(operation) == bundle
    assert operation[-1] == calls
    assert before == {str(p.relative_to(bundle)): p.read_bytes() for p in bundle.rglob("*") if p.is_file()}
    with pytest.raises(ValueError, match="selection differs"):
        invoke(operation, concurrency=1)
    assert_history(operation)


def test_existing_fresh_execution_blocks_reuse(operation):
    invoke(operation)
    (operation[1] / "reports/wipe" / NEW).mkdir()
    with pytest.raises(ValueError):
        invoke(operation)


def test_existing_disposition_is_preserved_without_reprompting(operation):
    base, folder = operation[1:3]
    restart.state.disposition(base, OLD, sorted(NAMES), "Previously reviewed failed attempt")
    before = (folder / "disposition.yaml").read_bytes()

    def no_prompt(_):
        pytest.fail("Existing valid disposition must not prompt again")

    invoke(operation, confirm=no_prompt)
    assert (folder / "disposition.yaml").read_bytes() == before
    assert_history(operation)


def test_later_failed_subset_uses_its_own_disposition_and_preserves_source_fleet(operation, monkeypatch):
    import tests.test_fleet_cleaning_state as fixtures

    source_data = operation[0]
    restart.state.disposition(operation[1], OLD, sorted(NAMES), "Reviewed before starting the subsequent subset")
    prior_disposition = (operation[2] / "disposition.yaml").read_bytes()
    second = "20000101T133000Z"
    original_prepare = fixtures.prepare
    monkeypatch.setattr(fixtures, "WIPE", second)
    monkeypatch.setattr(fixtures, "prepare", lambda source, names: original_prepare(source, names=names, run=second))
    _, folder, _ = fixtures.terminal(source_data, "FAILED", names=NAMES[:1])
    questions = []
    invoke(operation, confirm=lambda question: questions.append(question) or "yes")
    assert len(questions) == 1
    assert restart.state.read(folder / "disposition.yaml")["node_names"] == sorted(NAMES[:1])
    assert restart.state.read(operation[2] / "disposition.yaml")["node_names"] == sorted(NAMES)
    assert (operation[2] / "disposition.yaml").read_bytes() == prior_disposition
    restart.state.check(operation[1])
    assert_history(operation)


def test_wrong_site_does_not_search_other_sites(operation):
    source_data = operation[0]
    with pytest.raises(ValueError, match="not registered"):
        restart.restart(source_data[0], "2-8-5-200", "another-site", OLD, 2,
                        source_data[3], str(source_data[3].parent))
    assert not (operation[2] / "disposition.yaml").exists()
    assert_history(operation)


@pytest.mark.parametrize("bootstrap", [True, False])
def test_probe_uses_existing_role_and_requires_bootstrap_source_and_observation_only(wipe_source, monkeypatch,
                                                                                  bootstrap):
    root, _, _, collection = wipe_source
    work = root / "output/2-8-5-200/test01/ocp/wipe/restarts" / NEW
    work.mkdir(parents=True)
    runtime = {"run_id": OLD, "native_runtime": {"ironic_image": "registry.example/ironic@sha256:" + "a" * 64},
               "deadline_context": {"run_id": OLD}}
    calls = []

    def command(root_arg, argv, collections):
        calls.append(argv)
        play = yaml.safe_load(Path(argv[-1]).read_text())[0]
        task = play["tasks"][0]
        assert task["ansible.builtin.include_role"]["tasks_from"] == "prepare_redfish_retry"
        assert task["vars"]["ironic_deploy_native_disk_clean"] is False
        assert task["vars"]["ironic_deploy_physical_lldp_hook"] is False
        assertions = play["tasks"][1]["ansible.builtin.assert"]["that"]
        assert any("bootstrap_verified" in value for value in assertions)
        restart.state.write(work / "native-provenance.json", {
            "bootstrap_verified": bootstrap, "compatibility_verified": True, "physical_execution": False,
            "force_power_state_during_sync": False, "source_sha256": play["vars"]["restart_expected_sources"],
            "image": runtime["native_runtime"]["ironic_image"]})

    monkeypatch.setattr(restart, "run_command", command)
    if bootstrap:
        restart.probe(root, "2-8-5-200", "test01", collection, str(collection.parent), runtime, work)
    else:
        with pytest.raises(ValueError, match="native-bootstrap"):
            restart.probe(root, "2-8-5-200", "test01", collection, str(collection.parent), runtime, work)
    assert len(calls) == 1 and calls[0][0] == "ansible-playbook"


def test_child_environment_retains_lease_scrubs_authority_and_preserves_dependency_roots(tmp_path, monkeypatch):
    monkeypatch.setenv("FLEET_AUTHORIZE_WIPE_RUN", "foreign")
    monkeypatch.setenv("MAKEFLAGS", "--eval=untrusted")
    monkeypatch.setenv("ANSIBLE_COLLECTIONS_PATHS", "foreign")
    base = tmp_path / "site"
    with restart.state.Lease(base):
        descriptor = int(os.environ["RHV_NATIVE_OPERATION_LOCK_FD"])

        def command(argv, **kwargs):
            assert kwargs["pass_fds"] == (descriptor,)
            env = kwargs["env"]
            assert env["RHV_NATIVE_OPERATION_SITE"] == str(base)
            assert "FLEET_AUTHORIZE_WIPE_RUN" not in env and "MAKEFLAGS" not in env
            assert "ANSIBLE_COLLECTIONS_PATHS" not in env
            assert env["ANSIBLE_COLLECTIONS_PATH"].startswith("/reviewed/collections:")
            assert env["ANSIBLE_COLLECTIONS_SCAN_SYS_PATH"] == "false"
            return subprocess.CompletedProcess(argv, 0)

        monkeypatch.setattr(restart.subprocess, "run", command)
        restart.run_command(tmp_path, ["ansible-playbook", "prepare.yml"], "/reviewed/collections")


@pytest.mark.parametrize("origin", ["environment", "command-line"])
def test_real_make_target_passes_selection_without_shell_interpolation(tmp_path, origin):
    root = tmp_path
    (root / "scripts").mkdir()
    (root / "input/2-8-5-200/test01").mkdir(parents=True)
    shutil.copyfile(ROOT / "Makefile", root / "Makefile")
    script = root / "scripts/restart-fleet-wipe.py"
    script.write_text("import json,sys; print(json.dumps(sys.argv[1:]))\n")
    malicious = "$(shell touch SHOULD_NOT_EXIST)"
    env = dict(os.environ)
    argv = ["make", "--no-print-directory", "restart-fleet-wipe", "ARCH=2-8-5-200",
            "SITE=test01", "CONCURRENCY=6"]
    if origin == "environment":
        env["FROM_RUN"] = malicious
    else:
        argv.append("FROM_RUN=" + malicious)
    result = subprocess.run(argv, cwd=root, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    args = json.loads(result.stdout.splitlines()[-1])
    assert args[args.index("--from-run") + 1] == malicious
    assert args[args.index("--concurrency") + 1] == "6"
    assert not (root / "SHOULD_NOT_EXIST").exists()
