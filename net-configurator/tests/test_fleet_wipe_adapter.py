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
            "disks": [disk], "system_vendor": {"manufacturer": "Example", "product_name": "Server",
                                               "serial_number": f"fixture-chassis-{index}"}}}))
        validation = gate.read_yaml(evidence / name / "validation.yaml")
        validation["disk_validation"].update(node=name, selected_disk=disk, disk_match_verified=True,
                                              match_count=1, failures=[])
        write(evidence / name / "validation.yaml", validation)
    refresh_receipt(evidence)
    collection = root / "wipe-collection"
    plugin = collection / "plugins/filter/fleet_install_disk_plan.py"
    plugin.parent.mkdir(parents=True)
    shutil.copyfile(COLLECTION / "plugins/filter/fleet_install_disk_plan.py", plugin)
    return root, bundle, evidence, collection


def prepare(source, names=NAMES, run=WIPE):
    return wipe.prepare(source[0], "2-8-5-200", "test01", RUN, run, names, source[3])


def test_sealed_plan_matches_identities_without_credentials_or_authority(wipe_source):
    root, _, evidence, _ = wipe_source
    before = {str(p): p.read_bytes() for p in evidence.rglob("*") if p.is_file()}
    bundle = prepare(wipe_source)
    plan = gate.read_yaml(bundle / "plan.yml")["fleet_install_disk_plan"]
    sealed = gate.read_yaml(bundle / "prepared.yaml")
    assert plan["concurrency_limit"] == 1 and plan["execution_ready"] is False
    assert sealed["node_names"] == sorted(NAMES)
    assert plan["manifest_sha256"] == gate.read_yaml(evidence / "eligibility.yaml")["manifest_sha256"]
    for name, digest in sealed["sha256"].items():
        assert hashlib.sha256((bundle / name).read_bytes()).hexdigest() == digest
    text = (bundle / "plan.yml").read_text()
    assert "FIXTURE_NOT_A_CREDENTIAL" not in text and "redfish_password" not in text
    assert set(bundle.iterdir()) == {bundle / name for name in ("plan.yml", "prepared.yaml", "review.txt")}
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in bundle.iterdir())
    assert not (root / "output/2-8-5-200/test01/reports/wipe").exists()
    assert before == {str(p): p.read_bytes() for p in evidence.rglob("*") if p.is_file()}


@pytest.mark.parametrize("case", ["changed_evidence", "changed_manifest", "changed_workbook",
                                 "failed_validation", "missing_disk_serial", "missing_host_serial",
                                 "duplicate_disk", "missing_node", "duplicate_node", "extra_node",
                                 "unsafe_run", "unsafe_site", "missing_plugin"])
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
        (collection / "plugins/filter/fleet_install_disk_plan.py").unlink()
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
    before = {p.name: p.read_bytes() for p in bundle.iterdir()}
    with pytest.raises(ValueError):
        prepare(wipe_source)
    assert before == {p.name: p.read_bytes() for p in bundle.iterdir()}


def test_real_make_target_never_invokes_runtime_or_installer(wipe_source):
    root, _, _, collection = wipe_source
    shutil.copyfile(ROOT / "Makefile", root / "Makefile")
    shutil.copytree(ROOT / "scripts", root / "scripts")
    bin_dir = root / "bin"
    bin_dir.mkdir()
    capture = root / "unexpected-runtime"
    for name in ("podman", "ansible-playbook", "openshift-install"):
        command = bin_dir / name
        command.write_text("#!/bin/sh\nprintf 'unexpected' > \"$WIPE_RUNTIME_CAPTURE\"\nexit 99\n")
        command.chmod(0o700)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "WIPE_RUNTIME_CAPTURE": str(capture)}
    command = ["make", "prepare-fleet-wipe", "ARCH=2-8-5-200", "SITE=test01", f"FLEET_RUN_ID={RUN}",
               f"FLEET_WIPE_RUN_ID={WIPE}", "FLEET_NODES=" + ",".join(NAMES),
               "RHV_BAREMETAL_OCP_ROOT=" + str(collection)]
    result = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not capture.exists()
    assert "no physical authority" in result.stdout
