# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""Exercise wipe preparation against sealed synthetic physical handoffs."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from tests.test_abi_eligibility_handoff import (
    candidate_workspace, eligible_run, gate, refresh_receipt, write,
)
from tests.test_fleet_inspection_adapter import workspace, NAMES, RUN, ROOT


spec = importlib.util.spec_from_file_location("wipe_adapter", ROOT / "scripts/prepare-fleet-wipe.py")
wipe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wipe)
WIPE = "20000101T130000Z"
COLLECTION = ROOT.parents[1] / "rhvp.baremetal_ocp"


@pytest.fixture
def wipe_source(eligible_run):
    root, bundle, evidence = eligible_run
    for index, name in enumerate(NAMES):
        node = gate.read_yaml(bundle / "plan.yml")["inspect_fleet_plan"]["authorized_nodes"][index]
        disk = {"name": f"/dev/nvme{index + 1}n1", "by_path": node["rootDeviceHints"]["deviceName"],
                "serial": f"fixture-volume-{index}", "wwn": f"fixture-wwn-{index}",
                "model": "Example Boot Volume", "size": 1000000000}
        (evidence / name / "inventory-raw.json").write_text(json.dumps({"inventory": {
            "disks": [disk, *[
                {"name": f"/dev/sd{letter}", "by_path": f"/dev/disk/by-path/example-data-{letter}",
                 "serial": f"fixture-data-{index}-{letter}", "wwn": f"fixture-data-wwn-{index}-{letter}",
                 "model": "Example Data Volume", "size": 2000000000} for letter in ("a", "b")]],
            "system_vendor": {"manufacturer": "Example", "product_name": "Server",
                                               "serial_number": f"fixture-chassis-{index}"}}}))
        validation = gate.read_yaml(evidence / name / "validation.yaml")
        validation["disk_validation"].update(node=name, selected_disk=disk, disk_match_verified=True,
                                              match_count=1, failures=[])
        write(evidence / name / "validation.yaml", validation)
    refresh_receipt(evidence)
    collection = root / "wipe-collection"
    for relative in wipe.collection_hashes(COLLECTION):
        target = collection / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(COLLECTION / relative, target)
    return root, bundle, evidence, collection


def prepare(source, names=NAMES, run=WIPE):
    return wipe.prepare(source[0], "2-8-5-200", "test01", RUN, run, names, source[3])


def test_sealed_plan_matches_identities_without_credentials_or_authority(wipe_source):
    root, _, evidence, _ = wipe_source
    before = {str(p): p.read_bytes() for p in evidence.rglob("*") if p.is_file()}
    bundle = prepare(wipe_source)
    plan = gate.read_yaml(bundle / "plan.yml")["fleet_native_disk_clean_plan"]
    sealed = gate.read_yaml(bundle / "prepared.yaml")
    assert plan["concurrency_limit"] == 1 and plan["execution_ready"] is False
    assert plan["operation"] == "native_installation_volume_metadata_cleaning"
    assert plan["clean_steps"] == [{"interface": "deploy", "step": "erase_devices_metadata", "args": {}}]
    assert plan["policy"]["optional"] is True
    assert plan["policy"]["require_post_reset_readback"] is False
    for node in plan["authorized_nodes"]:
        assert len(node["observed_disks"]) == 3
        assert len(node["protected_disks"]) == 2
        assert len(node["proposed_skip_block_devices"]) == 2
        assert node["scope_verification"]["live_partition_exclusions_verified"] is False
        assert all("name" not in hint and "by_path" not in hint
                   for hint in node["proposed_skip_block_devices"])
    assert sealed["schema_version"] == 2 and sealed["operation"] == plan["operation"]
    assert "UNVERIFIED" in (bundle / "review.txt").read_text()
    assert sealed["node_names"] == sorted(NAMES)
    assert plan["manifest_sha256"] == gate.read_yaml(evidence / "eligibility.yaml")["manifest_sha256"]
    for name, digest in sealed["sha256"].items():
        assert hashlib.sha256((bundle / name).read_bytes()).hexdigest() == digest
    text = (bundle / "plan.yml").read_text()
    assert "FIXTURE_NOT_A_CREDENTIAL" not in text and "redfish_password" not in text
    assert set(p.name for p in bundle.iterdir()) == {
        "plan.yml", "prepared.yaml", "review.txt", "runtime-plan.yml", "hosts.yml", "nmstate", "early-network"}
    assert sealed["runtime_sealed"] is False
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in bundle.rglob("*") if p.is_file())
    assert all(p.stat().st_mode & 0o777 == 0o700 for p in bundle.rglob("*") if p.is_dir())
    assert not (root / "output/2-8-5-200/test01/reports/wipe").exists()
    assert before == {str(p): p.read_bytes() for p in evidence.rglob("*") if p.is_file()}
    # Preparation leaves the successful inspection-only ISO handoff eligible.
    gate.validate_handoff(root, "2-8-5-200", "test01", RUN)


@pytest.mark.parametrize("case", ["changed_evidence", "changed_manifest", "changed_workbook",
                                 "failed_validation", "missing_disk_serial", "missing_host_serial",
                                 "duplicate_disk", "missing_node", "duplicate_node", "extra_node",
                                 "unsafe_run", "unsafe_site", "missing_plugin", "missing_protected_identity",
                                 "ambiguous_protected_disks", "protected_hint_operator"])
def test_invalid_preparation_creates_no_bundle(wipe_source, case):
    root, _, evidence, collection = wipe_source
    names, run, site = NAMES, WIPE, "test01"
    if case == "changed_evidence":
        (evidence / NAMES[0] / "ports-raw.json").write_text("{}")
    elif case == "changed_manifest":
        (root / "output/2-8-5-200/test01/ocp/agent-config.yaml").write_text("changed")
    elif case == "changed_workbook":
        (root / "input/2-8-5-200/test01/2-8-5-200.xlsx").write_bytes(b"changed")
    elif case == "failed_validation":
        value = gate.read_yaml(evidence / "summary.yaml")
        value["validation_passed"] = False
        write(evidence / "summary.yaml", value)
        refresh_receipt(evidence)
    elif case in ("missing_disk_serial", "missing_host_serial", "duplicate_disk"):
        path = evidence / NAMES[0] / "inventory-raw.json"
        raw = json.loads(path.read_text())
        if case == "missing_disk_serial":
            raw["inventory"]["disks"][0]["serial"] = ""
        elif case == "missing_host_serial":
            raw["inventory"]["system_vendor"]["serial_number"] = ""
        else:
            raw["inventory"]["disks"].append(dict(raw["inventory"]["disks"][0]))
        path.write_text(json.dumps(raw))
        refresh_receipt(evidence)
    elif case in ("missing_protected_identity", "ambiguous_protected_disks", "protected_hint_operator"):
        path = evidence / NAMES[0] / "inventory-raw.json"
        raw = json.loads(path.read_text())
        disks = raw["inventory"]["disks"]
        if case == "missing_protected_identity":
            disks[1]["serial"] = ""
        elif case == "ambiguous_protected_disks":
            disks[2]["wwn"] = disks[1]["wwn"]
        else:
            disks[1]["serial"] = "<or> target <or> data"
        path.write_text(json.dumps(raw))
        refresh_receipt(evidence)
    elif case == "missing_node":
        names = NAMES[:1]
    elif case == "duplicate_node":
        names = NAMES + [NAMES[0]]
    elif case == "extra_node":
        names = NAMES + ["node-extra"]
    elif case == "unsafe_run":
        run = "../unsafe"
    elif case == "unsafe_site":
        site = "../test01"
    else:
        (collection / "plugins/filter/fleet_native_disk_clean_plan.py").unlink()
    with pytest.raises((ValueError, FileNotFoundError)):
        wipe.prepare(root, "2-8-5-200", site, RUN, run, names, collection)
    assert not (root / "output/2-8-5-200/test01/ocp/wipe").exists()


@pytest.mark.parametrize("case", ["existing_bundle", "existing_evidence", "symlink_parent", "symlink_evidence"])
def test_existing_or_symlink_paths_are_preserved(wipe_source, case):
    root = wipe_source[0]
    base = root / "output/2-8-5-200/test01"
    bundle = base / "ocp/wipe/fleet" / WIPE
    evidence = base / "reports/wipe" / WIPE
    if case.startswith("existing"):
        target = bundle if case == "existing_bundle" else evidence
        target.mkdir(parents=True)
        marker = target / "preserve.txt"
        marker.write_text("original")
    else:
        parent = bundle.parent if case == "symlink_parent" else evidence.parent
        parent.parent.mkdir(parents=True, exist_ok=True)
        target = root / "unrelated"
        target.mkdir()
        parent.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError):
        prepare(wipe_source)
    if case.startswith("existing"):
        assert marker.read_text() == "original"
    else:
        assert list(target.iterdir()) == []


def test_repeat_preparation_does_not_rewrite_seal(wipe_source):
    bundle = prepare(wipe_source)
    before = {str(p.relative_to(bundle)): p.read_bytes() for p in bundle.rglob("*") if p.is_file()}
    with pytest.raises(ValueError):
        prepare(wipe_source)
    assert before == {str(p.relative_to(bundle)): p.read_bytes() for p in bundle.rglob("*") if p.is_file()}


def test_real_make_preparation_invokes_only_preflight_driver(wipe_source):
    root, _, _, collection = wipe_source
    shutil.copyfile(ROOT / "Makefile", root / "Makefile")
    shutil.copytree(ROOT / "scripts", root / "scripts")
    bin_dir = root / "bin"
    bin_dir.mkdir()
    capture = root / "unexpected-runtime"
    for name in ("podman", "openshift-install"):
        command = bin_dir / name
        command.write_text("#!/bin/sh\nprintf 'unexpected' > \"$WIPE_RUNTIME_CAPTURE\"\nexit 99\n")
        command.chmod(0o700)
    preflight_capture = root / "preflight-call.json"
    command = bin_dir / "ansible-playbook"
    command.write_text("#!/usr/bin/env python3\nimport json,os,sys\nfrom pathlib import Path\n"
                       "Path(os.environ['PREFLIGHT_CAPTURE']).write_text(json.dumps(sys.argv[1:]))\n")
    command.chmod(0o700)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "WIPE_RUNTIME_CAPTURE": str(capture),
           "PREFLIGHT_CAPTURE": str(preflight_capture)}
    command = ["make", "prepare-fleet-wipe", "ARCH=2-8-5-200", "SITE=test01", f"FLEET_RUN_ID={RUN}",
               f"FLEET_WIPE_RUN_ID={WIPE}", "FLEET_NODES=" + ",".join(NAMES),
               "RHV_BAREMETAL_OCP_ROOT=" + str(collection)]
    result = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not capture.exists()
    assert "playbooks/prepare-fleet-wipe.yml" in json.loads(preflight_capture.read_text())
    assert "no physical authority" in result.stdout


def test_previous_preparation_is_preserved_when_fresh_native_plan_is_created(wipe_source):
    root = wipe_source[0]
    previous = root / "output/2-8-5-200/test01/ocp/wipe/fleet/20000101T123000Z"
    previous.mkdir(parents=True)
    marker = previous / "plan.yml"
    marker.write_text("legacy preparation; execution_ready: false\n")
    before = marker.read_bytes()
    bundle = prepare(wipe_source)
    assert bundle != previous and marker.read_bytes() == before


def seal_bundle(source, bundle):
    runtime = gate.read_yaml(bundle / "runtime-plan.yml")["fleet_wipe_runtime_plan"]
    original = gate.read_yaml(source[1] / "preflight-vars.yaml")
    preflight = {**original, "ironic_deploy_provisioning_ip": "192.0.2.10",
                 "ironic_deploy_provisioning_interface": "eth-test"}
    write(bundle / "preflight-vars.yaml", preflight)
    write(bundle / "callback-routes.yaml", {"nodes": [n["name"] for n in runtime["authorized_nodes"]],
        "routes": [{"item": n, "stdout": json.dumps([{"dst": n["devices"]["bond_ip"],
            "dev": "eth-test", "prefsrc": "192.0.2.10"}])} for n in runtime["authorized_nodes"]]})
    wipe.seal(source[0], "2-8-5-200", "test01", bundle)
    return bundle


def test_fresh_seal_and_exact_authorization_do_not_start_execution(wipe_source):
    bundle = prepare(wipe_source)
    with pytest.raises(ValueError):
        wipe.verify(wipe_source[0], "2-8-5-200", "test01", bundle)
    seal_bundle(wipe_source, bundle)
    authority = wipe.authorize(wipe_source[0], "2-8-5-200", "test01", bundle, WIPE, NAMES)
    assert authority["fleet_wipe_authorization"]["confirmed"] is True
    assert not (wipe_source[0] / "output/2-8-5-200/test01/reports/wipe").exists()
    with pytest.raises(ValueError):
        wipe.authorize(wipe_source[0], "2-8-5-200", "test01", bundle, WIPE, NAMES[:1])


@pytest.mark.parametrize("case", ["executor", "network", "preflight", "route", "source"])
def test_sealed_runtime_changes_are_rejected(wipe_source, case):
    bundle = seal_bundle(wipe_source, prepare(wipe_source))
    path = {"executor": wipe_source[3] / "roles/fleet_native_disk_clean_driver/tasks/clean_node.yml",
            "network": bundle / "nmstate" / (NAMES[0] + ".yaml"),
            "preflight": bundle / "preflight-vars.yaml", "route": bundle / "callback-routes.yaml",
            "source": wipe_source[0] / "input/2-8-5-200/test01/2-8-5-200.xlsx"}[case]
    path.write_bytes(path.read_bytes() + b"\n# changed\n")
    with pytest.raises(ValueError):
        wipe.verify(wipe_source[0], "2-8-5-200", "test01", bundle)
