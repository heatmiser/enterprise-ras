"""Verify optional cleaning evidence and site leases without hardware or disk writes."""

import copy
import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tests.test_fleet_wipe_adapter import wipe, wipe_source, prepare, seal_bundle, WIPE, COLLECTION
from tests.test_abi_eligibility_handoff import eligible_run, candidate_workspace, write
from tests.test_fleet_inspection_adapter import workspace, NAMES, RUN

state = wipe.state
spec = importlib.util.spec_from_file_location("native_result_fixture", COLLECTION / "tests/scripts/test_fleet_native_disk_clean_result.py")
results = importlib.util.module_from_spec(spec)
spec.loader.exec_module(results)


def terminal(source, status="SUCCEEDED"):
    root = source[0]
    bundle = seal_bundle(source, prepare(source))
    plan = state.read(bundle / "plan.yml")["fleet_native_disk_clean_plan"]
    base = state.site_root(root, "2-8-5-200", "test01")
    with state.Lease(base):
        state.begin(base, plan, state.digest(bundle / "prepared.yaml"))
    folder = state.evidence_root(base, WIPE)
    runtime = {"run_id": WIPE, "status": "removed", "cleanup_status": "complete",
               "runtime_retained": False, "lock_owned": False,
               "uuids": {name: f"00000000-0000-4000-8000-{index + 1:012d}" for index, name in enumerate(NAMES)},
               "agent_endpoints": {name: f"http://192.0.2.{index + 1}:9999" for index, name in enumerate(NAMES)},
               "agent_metadata": {"version": "11.6.fixture"},
               "native_extension": {"source_sha256": {k: "c" * 64 for k in (
                   "rhvp_ironic_native_disk_clean.py", "fleet_native_disk_clean_scope.py", "runtime_diagnostics.py")}}}
    request = {"preparation": plan, "authorization": {"action": "erase_devices_metadata", "run_id": WIPE,
        "node_names": sorted(NAMES), "preparation_sha256": state.fingerprint(plan), "node_uuids": runtime["uuids"],
        "agent_endpoints": runtime["agent_endpoints"], "agent_version": runtime["agent_metadata"]["version"],
        "conductor_sha256": "c" * 64, "scope_validator_sha256": "c" * 64, "redactor_sha256": "c" * 64}}
    outcomes = {}
    for target in plan["authorized_nodes"]:
        name = target["name"]
        _, raw = results.fixture()
        raw.update(name=name, uuid=runtime["uuids"][name])
        binding = {"node": name, "uuid": raw["uuid"], "request_sha256": state.fingerprint(request),
                   "preparation_sha256": state.fingerprint(plan)}
        info = raw["driver_internal_info"]
        info["rhvp_native_disk_clean_boot"].update(binding)
        record = info["rhvp_native_disk_clean"]
        record.update(binding, run_id=WIPE, inspection_run_id=RUN)
        record["scope"].update(node=name, node_plan_sha256=state.fingerprint(target), host_identity=target["host_identity"])
        if status == "FAILED" and name == NAMES[0]:
            raw["provision_state"] = "clean failed"
            record.update(phase="NATIVE_FAILED", native_result_succeeded=False)
            record["command_outcome"]["command_status"] = "FAILED"
        outcome = results.contract.fleet_native_disk_clean_result(raw, request, name, raw["uuid"], True)
        outcome["cleanup"] = {"verified": True, "power_state": "Off", "media_detached": True}
        outcomes[name] = outcome
        state.write(folder / name / "outcome.yaml", outcome)
    summary = results.contract.fleet_native_disk_clean_receipt(plan, outcomes, runtime)
    state.write(folder / "summary.yaml", summary)
    state.write(folder / "runtime.yaml", runtime)
    paths = ["summary.yaml", "runtime.yaml", "execution-intent.yaml"] + [name + "/outcome.yaml" for name in sorted(NAMES)]
    state.write(folder / "receipt.yaml", {**summary, "evidence_sha256": {p: state.digest(folder / p) for p in paths}})
    return base, folder, bundle


def test_no_cleaning_and_prepared_only_allow_inspection_path(wipe_source):
    base = state.site_root(wipe_source[0], "2-8-5-200", "test01")
    state.check(base)
    seal_bundle(wipe_source, prepare(wipe_source))
    state.check(base)


def test_successful_native_receipt_allows_progression(wipe_source):
    base, _, _ = terminal(wipe_source)
    state.check(base)


def test_terminal_failure_requires_exact_recorded_disposition(wipe_source):
    base, folder, _ = terminal(wipe_source, "FAILED")
    with pytest.raises(ValueError):
        state.check(base)
    with pytest.raises(ValueError):
        state.disposition(base, WIPE, NAMES[:1], "Proceed with installation")
    state.disposition(base, WIPE, NAMES, "Operator accepts installation attempt after failed optional cleaning")
    state.check(base)
    assert state.read(folder / "disposition.yaml")["receipt_sha256"] == state.digest(folder / "receipt.yaml")
    with pytest.raises(ValueError):
        state.disposition(base, WIPE, NAMES, "Do not overwrite")


@pytest.mark.parametrize("case", ["missing_receipt", "changed_outcome", "changed_seal", "changed_plan", "retained_runtime",
                                  "scope", "command", "cleanup", "orphan", "registry"])
def test_incomplete_changed_or_unsafe_evidence_blocks_progression(wipe_source, case):
    base, folder, bundle = terminal(wipe_source)
    if case == "missing_receipt":
        (folder / "receipt.yaml").unlink()
    elif case == "changed_outcome":
        (folder / NAMES[0] / "outcome.yaml").write_text("changed")
    elif case in ("changed_seal", "changed_plan"):
        path = bundle / ("prepared.yaml" if case == "changed_seal" else "plan.yml")
        path.write_text(path.read_text() + "\n# changed\n")
    elif case == "orphan":
        (base / "reports/wipe/20000101T140000Z").mkdir()
    elif case == "registry":
        (base / "ocp/cleaning-registry.yaml").unlink()
    else:
        path = folder / ("runtime.yaml" if case == "retained_runtime" else NAMES[0] + "/outcome.yaml")
        value = state.read(path)
        if case == "retained_runtime":
            value["runtime_retained"] = True
        elif case == "scope":
            value["scope"]["skip_block_devices"] = []
        elif case == "command":
            value["command"]["command_id"] = "00000000-0000-4000-8000-000000000099"
        else:
            value["cleanup"]["verified"] = False
        state.write(path, value)
        receipt = state.read(folder / "receipt.yaml")
        receipt["evidence_sha256"][str(path.relative_to(folder))] = state.digest(path)
        state.write(folder / "receipt.yaml", receipt)
    with pytest.raises((ValueError, KeyError)):
        state.check(base)


def test_unknown_started_run_has_no_disposition(wipe_source):
    bundle = seal_bundle(wipe_source, prepare(wipe_source))
    base = state.site_root(wipe_source[0], "2-8-5-200", "test01")
    plan = state.read(bundle / "plan.yml")["fleet_native_disk_clean_plan"]
    with state.Lease(base):
        state.begin(base, plan, state.digest(bundle / "prepared.yaml"))
    with pytest.raises(ValueError):
        state.disposition(base, WIPE, NAMES, "Cannot override running or unknown work")


def test_kernel_lease_survives_recursive_make_and_blocks_competitor(tmp_path):
    helper = Path(state.__file__)
    base = state.site_root(tmp_path, "arch", "site")
    makefile = tmp_path / "Makefile"
    verifier = tmp_path / "verify.py"
    verifier.write_text("import importlib.util,os,subprocess,sys\nfrom pathlib import Path\n"
        f"s=importlib.util.spec_from_file_location('lease', {str(helper)!r})\n"
        "m=importlib.util.module_from_spec(s);s.loader.exec_module(m)\n"
        "base=Path(os.environ['RHV_NATIVE_OPERATION_SITE'])\n"
        "with m.Lease(base):\n"
        " env=dict(os.environ);env.pop('RHV_NATIVE_OPERATION_LOCK_FD');env.pop('RHV_NATIVE_OPERATION_SITE')\n"
        f" result=subprocess.run([sys.executable,{str(helper)!r},'check','--root',{str(tmp_path)!r},'--arch','arch','--site','site'],env=env,capture_output=True)\n"
        " assert result.returncode != 0\n")
    makefile.write_text("outer:\n\t$(MAKE) --no-print-directory inner\ninner:\n\t" + sys.executable + " " + str(verifier) + "\n")
    result = subprocess.run([sys.executable, str(helper), "lease", "--root", str(tmp_path), "--arch", "arch", "--site", "site",
                             "--", "make", "--no-print-directory", "outer"], cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    with state.Lease(base):
        state.check(base)
