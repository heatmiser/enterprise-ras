# SPDX-License-Identifier: MIT
"""Existing-ISO preparation rejects drift without generating or booting anything."""

import importlib.util
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import openpyxl
import pytest
import yaml

from tests.test_abi_eligibility_handoff import candidate_workspace, eligible_run
from tests.test_fleet_inspection_adapter import workspace, ROOT, RUN

COLLECTION = ROOT.parent.parent / "rhvp.baremetal_ocp"
spec = importlib.util.spec_from_file_location("abi_install", ROOT / "scripts/prepare-abi-install.py")
install = importlib.util.module_from_spec(spec)
spec.loader.exec_module(install)


@pytest.fixture
def oem_workspace(candidate_workspace):
    root, collection = candidate_workspace
    path = root / "input/2-8-5-200/test01/ocp-settings.yml"
    settings = yaml.safe_load(path.read_text())
    settings["oem"] = "dell"
    path.write_text(yaml.safe_dump(settings))
    book_path = path.parent / "2-8-5-200.xlsx"
    book = openpyxl.load_workbook(book_path)
    book["Settings"].append(["ocp_oem", "dell"])
    book.save(book_path)
    book.close()
    for role in ("ironic_deploy", "ironic_cleanup", "ironic_image_resolve"):
        shutil.copytree(COLLECTION / "roles" / role, collection / "roles" / role)
    for name in ("serve_abi_iso.yml", "_abi_iso_runtime.yml"):
        target = collection / "playbooks" / name
        target.parent.mkdir(exist_ok=True)
        shutil.copyfile(COLLECTION / "playbooks" / name, target)
    target = collection / "plugins/filter/runtime_diagnostics.py"
    target.parent.mkdir(parents=True)
    shutil.copyfile(COLLECTION / "plugins/filter/runtime_diagnostics.py", target)
    return root, collection


@pytest.fixture
def staged_install(oem_workspace, eligible_run, monkeypatch):
    root, collection = oem_workspace
    _, bundle, evidence = eligible_run
    stage = root / "output/2-8-5-200/test01/ocp-iso" / RUN
    stage.mkdir(parents=True)
    shutil.copyfile(evidence / "eligibility.yaml", stage / "eligibility.yaml")
    shutil.copyfile(root / "output/2-8-5-200/test01/ocp/endpoint-map.yaml", stage / "endpoint-map.yaml")
    plan = yaml.safe_load((bundle / "plan.yml").read_text())["inspect_fleet_plan"]
    master = next(node for node in plan["authorized_nodes"] if node["role"] == "master")
    (stage / "rendezvousIP").write_text(master["devices"]["bond_ip"] + "\n")
    (stage / "agent.x86_64.iso").write_bytes(b"fixture ISO payload\n")
    (stage / ".openshift_install_state.json").write_text('{"fixture":true}')
    (stage / "auth").mkdir()
    (stage / "auth/kubeconfig").write_text("PRIVATE_FIXTURE_CONFIG")
    (stage / "auth/kubeadmin-password").write_text("PRIVATE_FIXTURE_PASSWORD")
    monkeypatch.setattr(install.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        stdout=json.dumps([{"dev": "eno1", "prefsrc": "192.0.2.10"}])))
    return root, collection, stage


def prepare(staged_install):
    root, collection, _ = staged_install
    return install.prepare(root, "2-8-5-200", "test01", RUN, collection)


def verify(staged_install):
    root, collection, _ = staged_install
    return install.verify(root, "2-8-5-200", "test01", RUN, collection)


def test_existing_iso_and_private_state_remain_unchanged(staged_install):
    _, _, stage = staged_install
    before = {str(path): path.read_bytes() for path in stage.rglob("*") if path.is_file()}
    bundle = prepare(staged_install)
    _, plan = verify(staged_install)
    assert plan["physical_authority"] is False
    assert plan["publisher_driver"] == "idrac"
    assert plan["iso_sha256"] == install.digest(stage / "agent.x86_64.iso")
    assert not (bundle / "media").exists()
    assert not any("PRIVATE_FIXTURE" in path.read_text() for path in bundle.iterdir())
    assert before == {str(path): path.read_bytes() for path in stage.rglob("*") if path.is_file()}


@pytest.mark.parametrize("filename", ["agent.x86_64.iso", "auth/kubeconfig", "endpoint-map.yaml"])
def test_serving_rejects_changed_generated_assets(staged_install, filename):
    prepare(staged_install)
    path = staged_install[2] / filename
    path.write_bytes(path.read_bytes() + b"changed")
    with pytest.raises(ValueError):
        verify(staged_install)


def test_serving_rejects_changed_membership(staged_install):
    bundle = prepare(staged_install)
    document = yaml.safe_load((bundle / "plan.yml").read_text())
    document["abi_install_plan"]["node_names"].pop()
    (bundle / "plan.yml").write_text(yaml.safe_dump(document))
    with pytest.raises(ValueError, match="Prepared plan changed"):
        verify(staged_install)


def test_serving_rejects_collection_drift(staged_install):
    prepare(staged_install)
    path = staged_install[1] / "playbooks/_abi_iso_runtime.yml"
    path.write_text(path.read_text() + "\n# changed\n")
    with pytest.raises(ValueError, match="Collection serving implementation changed"):
        verify(staged_install)


def test_preparation_cannot_overwrite_a_reviewed_plan(staged_install):
    prepare(staged_install)
    with pytest.raises(ValueError, match="preparation exists"):
        prepare(staged_install)


def test_changed_route_blocks_serving(staged_install, monkeypatch):
    prepare(staged_install)
    monkeypatch.setattr(install.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        stdout=json.dumps([{"dev": "eno2", "prefsrc": "192.0.2.11"}])))
    with pytest.raises(ValueError, match="Routes or BMC identities changed"):
        verify(staged_install)
