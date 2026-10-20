"""Verify optional cleaning evidence and site leases without hardware or disk writes."""

import copy
import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tests.test_fleet_wipe_adapter import wipe, wipe_source, prepare, seal_bundle, WIPE, COLLECTION, workspace
from tests.test_abi_eligibility_handoff import eligible_run, candidate_workspace, write
from tests.test_fleet_inspection_adapter import NAMES, RUN

state = wipe.state
spec = importlib.util.spec_from_file_location("native_result_fixture", COLLECTION / "tests/scripts/test_fleet_native_disk_clean_result.py")
results = importlib.util.module_from_spec(spec)
spec.loader.exec_module(results)


def terminal(source, status="SUCCEEDED", legacy=False, names=None):
    names = NAMES if names is None else names
    root = source[0]
    bundle = seal_bundle(source, prepare(source, names=names))
    plan = state.read(bundle / "plan.yml")["fleet_native_disk_clean_plan"]
    if legacy:
        plan.update(schema_version=2, operation=state.LEGACY_OPERATION)
        plan.pop("scope")
        state.write(bundle / "plan.yml", {"fleet_native_disk_clean_plan": plan})
        prepared = state.read(bundle / "prepared.yaml")
        prepared.update(schema_version=2, operation=state.LEGACY_OPERATION, sha256=wipe.bundle_hashes(bundle))
        prepared.pop("scope")
        state.write(bundle / "prepared.yaml", prepared)
    base = state.site_root(root, "2-8-5-200", "test01")
    folder = state.evidence_root(base, WIPE)
    with state.Lease(base):
        if legacy:
            # Model an already started historical run; new execution rejects this operation.
            intent = {"schema_version": 1, "operation": state.LEGACY_OPERATION, "run_id": WIPE,
                      "inspection_run_id": RUN, "node_names": sorted(names),
                      "preparation_sha256": state.fingerprint(plan),
                      "prepared_sha256": state.digest(bundle / "prepared.yaml"),
                      "manifest_sha256": plan["manifest_sha256"], "status": "STARTED"}
            state.write(folder / "execution-intent.yaml", intent)
            state.write(base / "ocp/cleaning-registry.yaml", {"schema_version": 1, "runs": {
                WIPE: {"intent_sha256": state.digest(folder / "execution-intent.yaml")}}})
        else:
            state.begin(base, plan, state.digest(bundle / "prepared.yaml"))
    runtime = {"run_id": WIPE, "status": "removed", "cleanup_status": "complete",
               "runtime_retained": False, "lock_owned": False,
               "uuids": {name: f"00000000-0000-4000-8000-{index + 1:012d}" for index, name in enumerate(names)}}
    authority = {"action": "erase_devices_metadata", "scope": state.SCOPE, "run_id": WIPE,
                 "node_names": sorted(names), "preparation_sha256": state.fingerprint(plan),
                 "node_uuids": runtime["uuids"]}
    if legacy:
        runtime.update(agent_endpoints={name: f"http://192.0.2.{i+1}:9999" for i, name in enumerate(names)},
                       agent_metadata={"version": "11.6.fixture"}, native_extension={"source_sha256": {
                           k: "c" * 64 for k in ("rhvp_ironic_native_disk_clean.py", "fleet_native_disk_clean_scope.py", "runtime_diagnostics.py")}})
        authority.pop("scope")
        authority.update(agent_endpoints=runtime["agent_endpoints"], agent_version="11.6.fixture",
                         conductor_sha256="c" * 64, scope_validator_sha256="c" * 64, redactor_sha256="c" * 64)
    request = {"preparation": plan, "authorization": authority}
    outcomes = {}
    for target in plan["authorized_nodes"]:
        name = target["name"]
        _, raw = results.fixture() if legacy else results.stock_fixture(plan["oem"])
        raw.update(name=name, uuid=runtime["uuids"][name])
        if legacy:
            binding = {"node": name, "uuid": raw["uuid"], "request_sha256": state.fingerprint(request),
                       "preparation_sha256": state.fingerprint(plan)}
            raw["driver_internal_info"]["rhvp_native_disk_clean_boot"].update(binding)
            record = raw["driver_internal_info"]["rhvp_native_disk_clean"]
            record.update(binding, run_id=WIPE, inspection_run_id=RUN)
            record["scope"].update(node=name, node_plan_sha256=state.fingerprint(target), host_identity=target["host_identity"])
        if status == "FAILED" and name == NAMES[0]:
            raw.update(provision_state="clean failed", clean_step={}, last_error="native metadata cleaning failed")
            if legacy:
                record.update(phase="NATIVE_FAILED", native_result_succeeded=False)
                record["command_outcome"]["command_status"] = "FAILED"
        outcome = results.contract.fleet_native_disk_clean_result(raw, request, name, raw["uuid"], True,
            submission={"attempts": 1, "accepted": True, "http_status": 202})
        outcome["cleanup"] = {"verified": True, "power_state": "Off", "media_detached": True}
        outcomes[name] = outcome
        state.write(folder / name / "outcome.yaml", outcome)
    summary = results.contract.fleet_native_disk_clean_receipt(plan, outcomes, runtime)
    state.write(folder / "summary.yaml", summary)
    state.write(folder / "runtime.yaml", runtime)
    paths = ["summary.yaml", "runtime.yaml", "execution-intent.yaml"] + [name + "/outcome.yaml" for name in sorted(names)]
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
            value["scope"] = "installation_volume"
        elif case == "command":
            value["submission"]["accepted"] = False
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


@pytest.mark.parametrize("status", ["SUCCEEDED", "FAILED"])
def test_historical_installation_volume_receipts_remain_readable(wipe_source, status):
    base, folder, _ = terminal(wipe_source, status, legacy=True)
    if status == "FAILED":
        state.disposition(base, WIPE, NAMES, "Historical cleanup verified; continue")
    state.check(base)
    assert state.read(folder / "receipt.yaml")["operation"] == state.LEGACY_OPERATION


def test_begin_rejects_old_scope_even_under_site_lease(wipe_source):
    bundle = seal_bundle(wipe_source, prepare(wipe_source))
    plan = state.read(bundle / "plan.yml")["fleet_native_disk_clean_plan"]
    plan.update(schema_version=2, operation=state.LEGACY_OPERATION)
    base = state.site_root(wipe_source[0], "2-8-5-200", "test01")
    with state.Lease(base), pytest.raises(ValueError, match="fresh all-disk"):
        state.begin(base, plan, state.digest(bundle / "prepared.yaml"))
    assert not (base / "reports/wipe").exists()



def test_single_node_receipt_without_abi_manifests_preserves_site_guards(wipe_source):
    from tests.test_physical_inspection_handoff import make_physical_source
    make_physical_source(wipe_source, NAMES[:1])
    base, folder, _ = terminal(wipe_source, names=NAMES[:1])
    state.check(base)
    receipt = state.read(folder / "receipt.yaml")
    assert receipt["node_names"] == NAMES[:1] and receipt["manifest_sha256"] == {}
    assert receipt["native_completion_verified"] is True


def test_failed_single_node_run_requires_its_exact_disposition(wipe_source):
    base, _, _ = terminal(wipe_source, status="FAILED", names=NAMES[:1])
    with pytest.raises(ValueError):
        state.check(base)
    with pytest.raises(ValueError):
        state.disposition(base, WIPE, NAMES, "Wrong selection")
    state.disposition(base, WIPE, NAMES[:1], "Operator accepts failed optional single-node cleaning")
    state.check(base)
