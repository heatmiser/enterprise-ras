# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""Verify node readiness independently of initial ABI installation eligibility."""

import copy
import importlib.util
from pathlib import Path

import pytest

from tests.test_abi_eligibility_handoff import candidate_workspace, eligible_run, gate, write
from tests.test_fleet_inspection_adapter import adapter, NAMES, ROOT, RUN
from tests.test_fleet_wipe_adapter import wipe_source, workspace

spec = importlib.util.spec_from_file_location("physical_gate", ROOT / "scripts/validate-physical-inspection.py")
physical = importlib.util.module_from_spec(spec)
spec.loader.exec_module(physical)


def make_physical_source(source, names=None):
    """Model the durable output of node-lifecycle inspection, without ABI inputs."""
    names = names or NAMES[:1]
    root, bundle, evidence = source[:3]
    document = gate.read_yaml(bundle / "plan.yml")
    plan = document["inspect_fleet_plan"]
    plan.pop("abi_handoff")
    plan["inspection_purpose"] = "node-lifecycle"
    plan["authorized_nodes"] = [n for n in plan["authorized_nodes"] if n["name"] in names]
    plan["expected_node_count"] = len(names)
    plan["concurrency_limit"] = 1
    plan["wiremap"] = [link for link in plan["wiremap"] if link["node"] in names]
    manifests = {"agent-config.yaml", "install-config.yaml", "endpoint-map.yaml"}
    document["fleet_adapter_source_sha256"] = {
        path: checksum for path, checksum in document["fleet_adapter_source_sha256"].items()
        if Path(path).name not in manifests}
    write(bundle / "plan.yml", document)
    original_preflight = gate.read_yaml(bundle / "preflight-vars.yaml")
    original_preflight["preflight_inspection_nodes"] = [plan["authorized_nodes"][0]]
    original_preflight["inspection_preflight_provenance"]["callback_target_ip"] = plan["authorized_nodes"][0]["devices"]["bond_ip"]
    write(bundle / "preflight-vars.yaml", original_preflight)
    (bundle / "prepared.yaml").unlink()
    adapter.seal(bundle)
    public = copy.deepcopy(plan)
    for node in public["authorized_nodes"]:
        node["devices"].pop("bmc_ip")
    write(evidence / "plan.yaml", public)
    index = gate.read_yaml(evidence / "index.yaml")
    for key in ("nodes", "validation"):
        index[key] = {name: value for name, value in index[key].items() if name in names}
    write(evidence / "index.yaml", index)
    summary = gate.read_yaml(evidence / "summary.yaml")
    summary.update(total_nodes=len(names), validated_nodes=len(names), eligible=False,
                   eligibility_reason="abi_candidate_binding_missing_or_membership_mismatch")
    write(evidence / "summary.yaml", summary)
    (evidence / "eligibility.yaml").unlink()
    for filename in manifests:
        (root / "output/2-8-5-200/test01/ocp" / filename).unlink()
    refresh_physical_receipt(evidence, names)
    return source


def refresh_physical_receipt(evidence, names):
    paths = {"plan.yaml", "index.yaml", "summary.yaml", "runtime.yaml"}
    paths.update(f"{name}/{filename}" for name in names for filename in physical.NODE_FILES)
    write(evidence / "physical-readiness.yaml", {
        "schema_version": 1, "operation": "physical_node_inspection", "run_id": RUN,
        "node_names": sorted(names), "evidence_sha256": {p: physical.digest(evidence / p) for p in paths}})


def test_single_node_physical_readiness_does_not_grant_abi_eligibility(wipe_source):
    make_physical_source(wipe_source)
    document, receipt, name = physical.validate_handoff(wipe_source[0], "2-8-5-200", "test01", RUN)
    assert name == "physical-readiness.yaml" and receipt["node_names"] == NAMES[:1]
    assert "abi_handoff" not in document["inspect_fleet_plan"]
    with pytest.raises((ValueError, FileNotFoundError)):
        gate.validate_handoff(wipe_source[0], "2-8-5-200", "test01", RUN)


def test_verified_legacy_abi_receipt_is_supported(wipe_source):
    _, receipt, name = physical.validate_handoff(wipe_source[0], "2-8-5-200", "test01", RUN)
    assert name == "eligibility.yaml" and receipt["node_names"] == NAMES


@pytest.mark.parametrize("case", ["missing_receipt", "changed_evidence", "synthetic", "failed_policy",
                                  "power_on", "retained_runtime", "wrong_receipt", "wrong_membership"])
def test_unready_physical_inspection_is_rejected(wipe_source, case):
    make_physical_source(wipe_source)
    evidence = wipe_source[2]
    if case == "missing_receipt":
        (evidence / "physical-readiness.yaml").unlink()
    elif case == "changed_evidence":
        (evidence / NAMES[0] / "ports-raw.json").write_text("{}")
    elif case in ("wrong_receipt", "wrong_membership"):
        value = physical.read_yaml(evidence / "physical-readiness.yaml")
        value["operation" if case == "wrong_receipt" else "node_names"] = "abi" if case == "wrong_receipt" else NAMES
        write(evidence / "physical-readiness.yaml", value)
    else:
        filename, key, value = {
            "synthetic": ("summary.yaml", "inspection_mode", "synthetic"),
            "failed_policy": ("summary.yaml", "storage_policy_passed", False),
            "power_on": (NAMES[0] + "/cleanup.yaml", "power_state", "On"),
            "retained_runtime": ("runtime.yaml", "runtime_retained", True),
        }[case]
        data = physical.read_yaml(evidence / filename)
        data[key] = value
        write(evidence / filename, data)
        refresh_physical_receipt(evidence, NAMES[:1])
    with pytest.raises((ValueError, FileNotFoundError)):
        physical.validate_handoff(wipe_source[0], "2-8-5-200", "test01", RUN)


def test_invalid_physical_receipt_never_falls_back_to_legacy(wipe_source):
    write(wipe_source[2] / "physical-readiness.yaml", {"schema_version": 99})
    with pytest.raises(ValueError):
        physical.validate_handoff(wipe_source[0], "2-8-5-200", "test01", RUN)
