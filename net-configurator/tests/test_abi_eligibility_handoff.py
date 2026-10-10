# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""Exercise the physical evidence gate and real Make recipe without hardware or credentials."""

import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

from tests.test_fleet_inspection_adapter import adapter, workspace, NAMES, RUN, ROOT
from tests.test_physical_endpoint_naming import MAPPING

spec = importlib.util.spec_from_file_location("abi_gate", ROOT / "scripts/validate-abi-handoff.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)
IMAGE = "registry.example.com/installer@sha256:" + "a" * 64


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value, sort_keys=False))


@pytest.fixture
def candidate_workspace(workspace, monkeypatch):
    root, collection = workspace
    path = root / "input/2-8-5-200/test01/ocp-settings.yml"
    settings = adapter.read_yaml(path)
    settings["node_roles"] = {"control_plane": [NAMES[0]], "worker_gpu": [NAMES[1]]}
    settings["node_endpoints"] = {name: MAPPING[name] for name in NAMES}
    addresses = {MAPPING[NAMES[0]]["bmc_fqdn"]: "192.0.2.101", MAPPING[NAMES[1]]["bmc_fqdn"]: "192.0.2.201"}
    monkeypatch.setattr(adapter.verify_bmc_resolution.__globals__["socket"], "getaddrinfo",
                        lambda host, *args, **kwargs: [(2, 1, 6, "", (addresses[host], 0))])
    write(path, settings)
    site = adapter.read_yaml(root / "output/2-8-5-200/test01/inventory/group_vars/all/main.yml")
    roles = adapter.OCP.build_role_map(settings, site["devices"], "2-8-5-200", "test01")
    base = root / "output/2-8-5-200/test01/ocp"
    write(base / "agent-config.yaml", adapter.OCP.build_agent_config(settings, roles, {}, site, "2-8-5-200"))
    write(base / "endpoint-map.yaml", adapter.build_endpoint_manifest(settings, site["devices"], roles))
    write(base / "install-config.yaml", {
        "metadata": {"name": "cluster-01"}, "baseDomain": "example.com",
        "controlPlane": {"replicas": 1}, "compute": [{"replicas": 1}],
        "networking": {"machineNetwork": [{"cidr": "198.51.100.0/24"}]},
        "platform": {"baremetal": {"apiVIPs": ["198.51.100.254"], "ingressVIPs": ["198.51.100.253"]}},
        "pullSecret": "FIXTURE_NOT_A_CREDENTIAL", "sshKey": "FIXTURE_PUBLIC_KEY"})
    return workspace


@pytest.fixture
def eligible_run(candidate_workspace):
    root, collection = candidate_workspace
    bundle = adapter.prepare(root, "2-8-5-200", "test01", NAMES, RUN, collection, 2)
    document = adapter.read_yaml(bundle / "plan.yml")
    plan = document["inspect_fleet_plan"]
    write(bundle / "preflight-vars.yaml", {"inspection_preflight_provenance": {
        "installer_version": document["fleet_adapter_expected_version"],
        "callback_target_ip": plan["authorized_nodes"][0]["devices"]["bond_ip"],
        "ee_image": IMAGE, "release_image": "registry.example.com/release@sha256:" + "b" * 64},
        "preflight_inspection_nodes": [plan["authorized_nodes"][0]]})
    write(bundle / "callback-routes.yaml", {"fixture": True})
    adapter.seal(bundle)
    evidence = Path(plan["artifact_root"])
    public = copy.deepcopy(plan)
    for node in public["authorized_nodes"]:
        node["devices"].pop("bmc_ip")
    write(evidence / "plan.yaml", public)
    validation = {k: True for k in ("validation_passed", "wiremap_passed", "physical_attachments_verified",
                                    "host_policy_passed", "disk_match_verified", "storage_policy_passed")}
    outcomes = {}
    for node in plan["authorized_nodes"]:
        name = node["name"]
        cleanup = {"node": name, "run_id": RUN, "power_state": "Off", "cleanup_status": "complete",
                   "cleanup_verified": True, "media_detached": True, "evidence_persisted": True}
        outcomes[name] = {"node": name, "run_id": RUN, "status": "COMPLETED", "inspection_mode": "physical",
                          "cleanup": cleanup}
        write(evidence / name / "cleanup.yaml", cleanup)
        write(evidence / name / "outcome.yaml", outcomes[name])
        write(evidence / name / "validation.yaml", {"node": name, "inspection_mode": "physical", "validation": validation,
            "disk_validation": {"run_id": RUN, "rootDeviceHints": node["rootDeviceHints"]}})
        write(evidence / name / "inventory.yaml", {"schema_version": 3, "inspection_mode": "physical",
            "session": {"run_id": RUN, "candidate_names": [name]}})
        for filename in ("inventory-raw.json", "ports-raw.json"):
            (evidence / name / filename).write_text(json.dumps({"fixture": name}))
    write(evidence / "index.yaml", {"run_id": RUN, "inspection_mode": "physical", "status": "COMPLETED",
        "nodes": outcomes, "validation": {name: validation for name in NAMES}})
    write(evidence / "summary.yaml", {**validation, "run_id": RUN, "inspection_mode": "physical",
        "eligible": True, "eligibility_reason": "physical_inspection_passed", "total_nodes": 2,
        "validated_nodes": 2, "failed_nodes": 0, "cleanup_status": "complete"})
    write(evidence / "runtime.yaml", {"run_id": RUN, "artifact_root": str(evidence), "status": "removed",
        "cleanup_status": "complete", "runtime_retained": False, "lock_owned": False, "ownership_confirmed": True})
    receipt = {"schema_version": 1, "run_id": RUN, "node_names": NAMES,
        "manifest_sha256": plan["abi_handoff"]["manifest_sha256"], "evidence_sha256": {}}
    write(evidence / "eligibility.yaml", receipt)
    refresh_receipt(evidence)
    return root, bundle, evidence


def refresh_receipt(evidence):
    path = evidence / "eligibility.yaml"
    receipt = gate.read_yaml(path)
    receipt["evidence_sha256"] = {str(p.relative_to(evidence)): gate.digest(p)
        for p in evidence.rglob("*") if p.is_file() and p != path}
    write(path, receipt)


def test_completed_physical_run_binds_exact_candidate_bytes(eligible_run):
    root, bundle, evidence = eligible_run
    image, candidates, _ = gate.validate_handoff(root, "2-8-5-200", "test01", RUN)
    assert image == IMAGE
    assert set(candidates) == {"agent-config.yaml", "install-config.yaml", "endpoint-map.yaml"}
    assert all(content == (root / "output/2-8-5-200/test01/ocp" / name).read_bytes()
               for name, content in candidates.items())


@pytest.mark.parametrize("case", ["failed_validation", "synthetic", "retained_runtime", "power_on", "wrong_run",
    "wrong_membership", "changed_evidence", "missing_inventory", "changed_manifest", "changed_workbook",
    "missing_receipt", "wrong_installer"])
def test_ineligible_or_changed_handoff_is_rejected(eligible_run, case):
    root, bundle, evidence = eligible_run
    if case in ("failed_validation", "synthetic"):
        path = evidence / "summary.yaml"
        value = gate.read_yaml(path)
        value["validation_passed" if case == "failed_validation" else "inspection_mode"] = False if case == "failed_validation" else "synthetic"
        write(path, value)
    elif case == "retained_runtime":
        path = evidence / "runtime.yaml"
        value = gate.read_yaml(path)
        value["runtime_retained"] = True
        write(path, value)
    elif case == "power_on":
        path = evidence / NAMES[0] / "cleanup.yaml"
        value = gate.read_yaml(path)
        value["power_state"] = "On"
        write(path, value)
    elif case in ("wrong_run", "wrong_membership"):
        path = evidence / "eligibility.yaml"
        value = gate.read_yaml(path)
        value["run_id" if case == "wrong_run" else "node_names"] = "20000101T120001Z" if case == "wrong_run" else NAMES[:1]
        write(path, value)
    elif case == "changed_evidence":
        (evidence / NAMES[0] / "ports-raw.json").write_text("{}")
    elif case == "missing_inventory":
        (evidence / NAMES[0] / "inventory.yaml").unlink()
    elif case == "changed_manifest":
        (root / "output/2-8-5-200/test01/ocp/agent-config.yaml").write_text("changed")
    elif case == "changed_workbook":
        (root / "input/2-8-5-200/test01/2-8-5-200.xlsx").write_bytes(b"changed")
    elif case == "missing_receipt":
        (evidence / "eligibility.yaml").unlink()
    if case in ("failed_validation", "synthetic", "retained_runtime", "power_on"):
        refresh_receipt(evidence)  # Recompute hashes to exercise semantic checks independently.
    with pytest.raises(ValueError):
        gate.validate_handoff(root, "2-8-5-200", "test01", RUN,
            ee_image="registry.example.com/wrong-image" if case == "wrong_installer" else "")


@pytest.mark.parametrize("field", ["hostname", "role", "rootDeviceHints", "networkConfig"])
def test_stale_candidate_rejected_before_preparation(candidate_workspace, field):
    root, collection = candidate_workspace
    path = root / "output/2-8-5-200/test01/ocp/agent-config.yaml"
    agent = gate.read_yaml(path)
    agent["hosts"][0][field] = "changed" if field in ("hostname", "role") else {}
    write(path, agent)
    with pytest.raises(ValueError, match="candidate differs"):
        adapter.prepare(root, "2-8-5-200", "test01", NAMES, RUN, collection, 2)
    assert not (root / "output/2-8-5-200/test01/ocp/inspection/fleet" / RUN).exists()


def test_real_make_recipe_uses_gate_before_installer(eligible_run):
    root, bundle, evidence = eligible_run
    shutil.copyfile(ROOT / "Makefile", root / "Makefile")
    shutil.copytree(ROOT / "scripts", root / "scripts")
    bin_dir = root / "bin"
    bin_dir.mkdir()
    capture = root / "installer-call.json"
    podman = bin_dir / "podman"
    podman.write_text("#!/usr/bin/env python3\nimport json,os,sys\nfrom pathlib import Path\n"
        "Path(os.environ['ABI_CAPTURE']).write_text(json.dumps(sys.argv[1:]))\n"
        "stage=Path(sys.argv[sys.argv.index('--dir')+1])\n(stage/'agent.x86_64.iso').write_bytes(b'fixture')\n")
    podman.chmod(0o700)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "ABI_CAPTURE": str(capture)}
    command = ["make", "generate-ocp-iso", "ARCH=2-8-5-200", "SITE=test01", f"FLEET_RUN_ID={RUN}"]
    receipt = (evidence / "eligibility.yaml").read_bytes()
    (evidence / "eligibility.yaml").unlink()
    rejected = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, timeout=60)
    assert rejected.returncode != 0 and not capture.exists()
    assert not (root / "output/2-8-5-200/test01/ocp-iso").exists()
    (evidence / "eligibility.yaml").write_bytes(receipt)
    accepted = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, timeout=60)
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    assert IMAGE in json.loads(capture.read_text())
    stage = root / "output/2-8-5-200/test01/ocp-iso" / RUN
    assert (stage / "agent.x86_64.iso").read_bytes() == b"fixture"
    assert (stage / "agent-config.yaml").read_bytes() == (root / "output/2-8-5-200/test01/ocp/agent-config.yaml").read_bytes()
    capture.unlink()
    repeat = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, timeout=60)
    assert repeat.returncode != 0 and not capture.exists()  # Never reuse an installer work directory.
