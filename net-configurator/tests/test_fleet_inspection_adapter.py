# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""Focused fleet preparation and entry-boundary checks; no physical services."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
NAMES = ["ipp5-285-rh-k8s-01", "ipp5-285-rh-gpu-01"]
RUN = "20261006T120000Z"
spec = importlib.util.spec_from_file_location("fleet_adapter", ROOT / "scripts/prepare-fleet-inspection.py")
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


@pytest.fixture
def workspace(tmp_path):
    """Use this project's actual workbook, generated interfaces and disk defaults."""
    root = tmp_path / "net-configurator"
    for relative in ("input/2-8-5-200/test01", "output/2-8-5-200/test01/inventory"):
        shutil.copytree(ROOT / relative, root / relative)
    collection = tmp_path / "collection"
    config = collection / "inventories/test01/cluster-vars.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(yaml.safe_dump({"ocp_ee_image": "fixture-ee",
        "ocp_nodes": [{"name": NAMES[0], "bmc": {"driver": "idrac", "host": "10.78.220.146"}},
                      {"name": NAMES[1], "bmc": {"driver": "idrac", "host": "10.78.220.141"}}]}))
    return root, collection


def prepare(workspace):
    root, collection = workspace
    return adapter.prepare(root, "2-8-5-200", "test01", NAMES, RUN, collection, 2)


def seal(bundle):
    document = adapter.read_yaml(bundle / "plan.yml")
    node = document["inspect_fleet_plan"]["authorized_nodes"][0]
    adapter.write_yaml(bundle / "preflight-vars.yaml", {
        "inspection_preflight_provenance": {
            "installer_version": document["fleet_adapter_expected_version"],
            "callback_target_ip": node["devices"]["bond_ip"]},
        "preflight_inspection_nodes": [node],
        "ironic_image_resolve_ocp_release_image": "fixture-release",
        "ironic_image_resolve_oc_container_image": "fixture-ee",
        "ironic_deploy_provisioning_ip": "192.0.2.10",
        "ironic_deploy_provisioning_interface": "fixture0",
        "ironic_preprovisioning_media_artifacts": {"fixture": "no services"}})
    adapter.write_yaml(bundle / "callback-routes.yaml", {"fixture": "No real route queries executed"})
    adapter.seal(bundle)


def test_recorded_cpu_identities_and_desired_ports_survive_preparation(workspace):
    bundle = prepare(workspace)
    document = adapter.read_yaml(bundle / "plan.yml")
    plan = document["inspect_fleet_plan"]
    assert [node["name"] for node in plan["authorized_nodes"]] == NAMES
    assert plan["expected_node_count"] == plan["concurrency_limit"] == 2
    assert len(plan["wiremap"]) == 4
    k8s, gpu = plan["authorized_nodes"]
    assert k8s["rootDeviceHints"]["deviceName"] == "/dev/disk/by-path/pci-0000:01:00.0-nvme-1"
    assert gpu["rootDeviceHints"]["deviceName"] == "/dev/disk/by-path/pci-0000:81:00.0-nvme-1"
    assert [(link["interface"], link["expected_switch"], link["expected_port"])
            for link in plan["wiremap"] if link["node"] == NAMES[0]] == [
        ("ns-nic0", "core-ipp5-285-rh-01", "swp2s0"),
        ("ns-nic1", "core-ipp5-285-rh-02", "swp2s0")]
    assert "storagePolicy" not in gpu  # Never inherit K8S controller/volume IDs.
    assert not Path(plan["artifact_root"]).exists()
    assert not (workspace[0] / "output/2-8-5-200/test01/ocp/agent-config.yaml").exists()
    for node in plan["authorized_nodes"]:
        nmstate = adapter.read_yaml(bundle / "nmstate" / f"{node['name']}.yaml")
        early = adapter.read_yaml(bundle / "early-network" / f"{node['name']}.yaml")
        assert [interface["name"] for interface in nmstate["interfaces"]] == ["bond0"]
        assert nmstate["interfaces"][0]["link-aggregation"]["port"] == ["ns-nic0", "ns-nic1"]
        assert early["interfaces"] == [{"name": interface["name"], "mac": interface["macAddress"]}
                                       for interface in node["interfaces"]]


def test_preparation_refuses_stale_mac_before_output(workspace):
    path = workspace[0] / "output/2-8-5-200/test01/inventory/group_vars/all/main.yml"
    document = adapter.read_yaml(path)
    document["devices"][NAMES[0]]["nic_alias_map"]["cpu"][0]["mac"] = "00:11:22:33:44:55"
    path.write_text(yaml.safe_dump(document))
    with pytest.raises(ValueError, match="Workbook CPU identity differs"):
        prepare(workspace)
    assert not (workspace[0] / "output/2-8-5-200/test01/ocp/inspection/fleet" / RUN).exists()


def test_preparation_refuses_missing_bmc_declaration(workspace):
    path = workspace[1] / "inventories/test01/cluster-vars.yaml"
    document = adapter.read_yaml(path)
    document["ocp_nodes"].pop()
    path.write_text(yaml.safe_dump(document))
    with pytest.raises(ValueError, match="unique workbook/collection node"):
        prepare(workspace)


@pytest.mark.parametrize("case", ["matching", "wrong_run", "wrong_nodes", "changed_network", "existing_evidence"])
def test_exact_authority_and_sealed_inputs(workspace, case):
    bundle = prepare(workspace)
    seal(bundle)
    run, names = RUN, NAMES
    if case == "wrong_run":
        run = "20261006T120001Z"
    elif case == "wrong_nodes":
        names = NAMES[:1]
    elif case == "changed_network":
        (bundle / "nmstate" / f"{NAMES[0]}.yaml").write_text("changed input")
    elif case == "existing_evidence":
        Path(adapter.read_yaml(bundle / "plan.yml")["inspect_fleet_plan"]["artifact_root"]).mkdir(parents=True)
    if case == "matching":
        auth = adapter.authorize(bundle, run, names)["fleet_inspection_driver_physical_authorization"]
        assert auth == {"confirmed": True, "action": "inspect", "run_id": RUN, "node_names": NAMES}
    else:
        with pytest.raises(ValueError):
            adapter.authorize(bundle, run, names)


def test_make_handoff_and_authorization_guard_without_ansible_services(workspace):
    root, collection = workspace
    shutil.copyfile(ROOT / "Makefile", root / "Makefile")
    shutil.copytree(ROOT / "scripts", root / "scripts")
    bundle = prepare(workspace)
    seal(bundle)
    (collection / "inventories/test01/secrets.yaml").write_text("unused fixture")
    bin_dir = root / "bin"
    bin_dir.mkdir()
    capture = root / "captured.json"
    executable = bin_dir / "ansible-playbook"
    executable.write_text("#!/usr/bin/env python3\nimport json, os, sys\nfrom pathlib import Path\n"
                          "Path(os.environ['FLEET_TEST_CAPTURE']).write_text(json.dumps(sys.argv[1:]))\n")
    executable.chmod(0o700)
    command = ["make", "inspect-fleet", "ARCH=2-8-5-200", "SITE=test01", f"FLEET_RUN_ID={RUN}",
               f"FLEET_AUTHORIZE_RUN={RUN}", f"RHV_BAREMETAL_OCP_ROOT={collection}"]
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "FLEET_TEST_CAPTURE": str(capture)}
    rejected = subprocess.run(command + [f"FLEET_AUTHORIZE_NODES={NAMES[0]}"], cwd=root,
                              env=env, capture_output=True, text=True, timeout=60)
    assert rejected.returncode != 0 and not capture.exists()
    accepted = subprocess.run(command + [f"FLEET_AUTHORIZE_NODES={','.join(NAMES)}"], cwd=root,
                              env=env, capture_output=True, text=True, timeout=60)
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    arguments = json.loads(capture.read_text())
    assert "playbooks/inspect-fleet.yml" in arguments
    assert f"@{bundle}/hosts.yml" not in arguments
    assert str(bundle / "hosts.yml") in arguments
    authority = next(json.loads(arg) for arg in arguments if arg.startswith('{"fleet_inspection_driver_physical_authorization"'))
    assert authority["fleet_inspection_driver_physical_authorization"]["node_names"] == NAMES


@pytest.mark.parametrize("authorized", [True, False])
def test_actual_adapter_guard_with_harmless_collection_entry(workspace, authorized):
    """Execute the adapter tasks; replace only the collection entry with a local marker."""
    root, _ = workspace
    bundle = prepare(workspace)
    seal(bundle)
    shutil.copytree(ROOT / "scripts", root / "scripts")
    playbooks = root / "playbooks"
    playbooks.mkdir()
    marker = root / "entered.yaml"
    stub = playbooks / "collection-fixture.yml"
    stub.write_text(yaml.safe_dump([{
        "name": "Capture selected credentials without any service or hardware",
        "hosts": "bootstrap", "gather_facts": False,
        "tasks": [{"name": "Record exact selected handoff", "ansible.builtin.copy": {
            "dest": str(marker), "mode": "0600",
            "content": "{{ {'names': fleet_inspection_driver_bmc_credentials.keys() | list, "
                       "'authority': fleet_inspection_driver_physical_authorization} | to_nice_yaml }}"}}]}]))
    play = playbooks / "inspect-fleet.yml"
    play.write_text((ROOT / "playbooks/inspect-fleet.yml").read_text().replace(
        "ansible.builtin.import_playbook: rhvp.baremetal_ocp.inspect_fleet",
        "ansible.builtin.import_playbook: collection-fixture.yml"))
    inputs = {"fleet_adapter_authorized_run": RUN,
              "fleet_adapter_authorized_nodes": NAMES if authorized else NAMES[:1],
              "bmc_credentials": {name: {"username": "fixture-user", "password": "fixture-password"}
                                  for name in NAMES + ["unselected-node"]}}
    inputs_path = root / "inputs.json"
    inputs_path.write_text(json.dumps(inputs))
    result = subprocess.run(["ansible-playbook", "-i", str(bundle / "hosts.yml"), str(play),
                             "-e", f"@{bundle}/plan.yml", "-e", f"@{bundle}/preflight-vars.yaml",
                             "-e", f"@{inputs_path}"], text=True, capture_output=True, timeout=60)
    if authorized:
        assert result.returncode == 0, result.stdout + result.stderr
        handoff = yaml.safe_load(marker.read_text())
        assert handoff["names"] == NAMES
        assert handoff["authority"]["node_names"] == NAMES
    else:
        assert result.returncode != 0
        assert not marker.exists()
