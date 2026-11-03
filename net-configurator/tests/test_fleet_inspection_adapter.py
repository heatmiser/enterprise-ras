# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""Focused fleet preparation and entry-boundary checks; no physical services."""

import importlib.util
import io
import json
import os
from pathlib import Path
import pty
import select
import shutil
import subprocess
import time

import pytest
import yaml
from tests.test_physical_endpoint_naming import MAPPING, example_inputs
import endpoint_naming

ROOT = Path(__file__).resolve().parent.parent
NAMES = ["server-control-01", "server-gpu-01"]
RUN = "20000101T120000Z"
spec = importlib.util.spec_from_file_location("fleet_adapter", ROOT / "scripts/prepare-fleet-inspection.py")
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


@pytest.fixture
def workspace(tmp_path):
    """Build an isolated example workbook/inventory; no site artifacts or credentials."""
    import openpyxl
    root = tmp_path / "net-configurator"
    settings, site, _ = example_inputs()
    settings.pop("node_endpoints")
    input_dir = root / "input/2-8-5-200/test01"
    inventory = root / "output/2-8-5-200/test01/inventory"
    input_dir.mkdir(parents=True)
    (inventory / "group_vars/all").mkdir(parents=True)
    (inventory / "host_vars").mkdir()
    (input_dir / "ocp-settings.yml").write_text(yaml.safe_dump(settings))
    (inventory / "group_vars/all/main.yml").write_text(yaml.safe_dump(site))
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Settings"
    sheet.append(["OPENSHIFT"])
    sheet.append(["Setting", "Value"])
    sheet.append(["ocp_version", settings["cluster"]["version"]])
    nodes = workbook.create_sheet("Nodes")
    nodes.append(["Function", "Name", "OOB VLAN", "Type", "MAC Address for ZTP",
                  "Mgmt IP Address", "Prefix", "Gateway", "ZTP", "Enabled",
                  "Notes", "Include in Initial ABI"])
    wires = workbook.create_sheet("Wire Map")
    wires.append(["Display in Air", "System Name (A)", "Port (A)", "Port Side (A)",
                  "Cable Split (A)", "System Name (B)", "Port (B)", "Port Side (B)",
                  "Cable Split (B)", "Network Profile", "Kernel NIC Alias", "MAC Address"])
    for index, (name, device) in enumerate(site["devices"].items(), 1):
        nodes.append(["gpu" if "-gpu-" in name else "support", name, 200, "server",
                      device["mac"], device["eth0_ip"], 24, "192.0.2.1", "No", "Yes",
                      None, device["include_in_initial_abi"]])
        (inventory / "host_vars" / f"{name}.yml").write_text(
            yaml.safe_dump({"ansible_host": device["eth0_ip"]}))
        for profile, entries in device["nic_alias_map"].items():
            for nic, entry in enumerate(entries):
                switch = f"oob-{nic + 1:02}" if profile == "oob" else f"core-{nic % 2 + 1:02}"
                port = f"swp{index}s{nic // 2}" if profile != "oob" else f"swp{index}"
                if profile == "gpu": port = f"swp{index + 10}s{nic // 2}"
                wires.append(["Yes" if profile == "cpu" else "No", name, entry["alias"],
                              None, None, switch, port, None, None, profile,
                              entry["alias"], entry["mac"]])
        wires.append(["No", name, "iDRAC", None, None, "oob-01", f"swp{index + 20}",
                      None, None, "oob", "bmc", device["mac"]])
    vlans = workbook.create_sheet("VLANs & Profiles")
    vlans.append(["VLANs"])
    vlans.append(["VLAN ID", "Name", "Purpose", "Subnet", "Gateway", "VRF"])
    vlans.append([300, "cpu_network", "CPU", site["common"]["cpu_network"],
                  site["common"]["cpu_gateway"], "INBAND"])
    workbook.save(input_dir / "2-8-5-200.xlsx")
    workbook.close()
    collection = tmp_path / "collection"
    config = collection / "inventories/test01/cluster-vars.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(yaml.safe_dump({"ocp_ee_image": "fixture-ee",
        "ocp_nodes": [{"name": name, "bmc": {"driver": "idrac", "host": site["devices"][name]["eth0_ip"]}}
                      for name in NAMES]}))
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
        "ironic_preprovisioning_media_nmstate_dir": str(bundle / "nmstate"),
        "ironic_preprovisioning_media_probe_node": node["name"],
        "ironic_preprovisioning_media_early_network_path": str(
            bundle / "early-network" / f"{node['name']}.yaml"),
        "ironic_preprovisioning_media_artifacts": {"fixture": "no services"},
        **document["inspect_fleet_plan"].get("httpd_tuning", {})})
    adapter.write_yaml(bundle / "callback-routes.yaml", {"fixture": "No real route queries executed"})
    adapter.seal(bundle)


def test_example_cpu_identities_and_desired_ports_survive_preparation(workspace):
    bundle = prepare(workspace)
    document = adapter.read_yaml(bundle / "plan.yml")
    plan = document["inspect_fleet_plan"]
    assert [node["name"] for node in plan["authorized_nodes"]] == NAMES
    assert plan["expected_node_count"] == plan["concurrency_limit"] == 2
    assert len(plan["wiremap"]) == 12
    assert {purpose: sum(link["purpose"] == purpose for link in plan["wiremap"])
            for purpose in ("cpu", "gpu", "host_oob")} == {"cpu": 4, "gpu": 4, "host_oob": 4}
    k8s, gpu = plan["authorized_nodes"]
    assert [node["devices"]["bmc_ip"] for node in (k8s, gpu)] == ["192.0.2.101", "192.0.2.201"]
    assert k8s["rootDeviceHints"]["deviceName"] == "/dev/disk/by-path/pci-0000:01:00.0-nvme-1"
    assert gpu["rootDeviceHints"]["deviceName"] == "/dev/disk/by-path/pci-0000:81:00.0-nvme-1"
    assert [(link["interface"], link["expected_switch"], link["expected_port"])
            for link in plan["wiremap"] if link["node"] == NAMES[0] and link["purpose"] == "cpu"] == [
        ("ns-nic0", "core-01", "swp1s0"),
        ("ns-nic1", "core-02", "swp1s0")]
    assert {link["interface"] for link in plan["wiremap"] if link["purpose"] == "host_oob"} == {
        "host-oob0", "host-oob1"}
    assert {link["interface"] for link in plan["wiremap"] if link["purpose"] == "gpu"} == {
        "rail0", "rail1", "rail2", "rail3"}
    assert all(link["mac"] not in {"02:ff:00:00:00:01", "02:ff:00:00:00:04"}
               for link in plan["wiremap"])  # BMC endpoints are not IPA host NICs.
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


@pytest.mark.parametrize("profile", ["gpu", "oob"])
def test_preparation_refuses_stale_auxiliary_mac_before_output(workspace, profile):
    path = workspace[0] / "output/2-8-5-200/test01/inventory/group_vars/all/main.yml"
    document = adapter.read_yaml(path)
    document["devices"][NAMES[1]]["nic_alias_map"][profile][0]["mac"] = "02:00:00:00:ee:ee"
    path.write_text(yaml.safe_dump(document))
    with pytest.raises(ValueError, match=f"Workbook {profile} identity differs"):
        prepare(workspace)
    assert not (workspace[0] / "output/2-8-5-200/test01/ocp/inspection/fleet" / RUN).exists()


def test_revised_workbook_gpu_links_match_recorded_physical_ports():
    """Optional operator-supplied physical evidence; identifiers stay outside the repository."""
    configured = [os.environ.get(key) for key in (
        "FLEET_TEST_WORKBOOK", "FLEET_TEST_REPORT", "FLEET_TEST_NODE")]
    if not all(configured):
        pytest.skip("Supply FLEET_TEST_WORKBOOK, FLEET_TEST_REPORT and FLEET_TEST_NODE")
    workbook, report_path = map(Path, configured[:2])
    node_name = configured[2]
    assert workbook.is_file() and report_path.is_file()
    wb = adapter.excel_parser.load_workbook_safe(workbook, data_only=True)
    try:
        wires = adapter.excel_parser._build_wiremap_row_list(wb["Wire Map"])
    finally:
        wb.close()
    device = {"nic_alias_map": adapter.excel_parser.build_nic_alias_map(wires, node_name)}
    links = adapter.auxiliary_links(node_name, wires, device)
    assert links  # Validate all supplied auxiliary links without assuming a rail count.
    raw = yaml.safe_load(report_path.read_text())["raw_ironic_responses"][node_name]
    ports = raw["ports"]["ports"]
    nics = raw["inventory"]["inventory"]["interfaces"]
    for link in links:
        matching_ports = [port for port in ports if port["address"] == link["mac"]]
        matching_nics = [nic for nic in nics if nic["mac_address"] == link["mac"] and nic["pci_address"]]
        assert len(matching_ports) == len(matching_nics) == 1
        assert matching_nics[0]["has_carrier"] is True
        assert matching_ports[0]["local_link_connection"]["switch_info"] == link["expected_switch"]
        assert matching_ports[0]["local_link_connection"]["port_id"] == link["expected_port"]


def test_preparation_refuses_missing_bmc_declaration(workspace):
    path = workspace[1] / "inventories/test01/cluster-vars.yaml"
    document = adapter.read_yaml(path)
    document["ocp_nodes"].pop()
    path.write_text(yaml.safe_dump(document))
    with pytest.raises(ValueError, match="unique workbook/collection node"):
        prepare(workspace)


@pytest.mark.parametrize("matching", [True, False])
def test_named_bmc_endpoints_keep_physical_authority_and_verify_resolution(workspace, monkeypatch, matching):
    root, collection = workspace
    settings_path = root / "input/2-8-5-200/test01/ocp-settings.yml"
    settings = adapter.read_yaml(settings_path)
    settings["cluster"].update(name="cluster-01", domain="example.com")
    settings["node_endpoints"] = MAPPING
    settings_path.write_text(yaml.safe_dump(settings))
    cluster_path = collection / "inventories/test01/cluster-vars.yaml"
    cluster = adapter.read_yaml(cluster_path)
    for node in cluster["ocp_nodes"]:
        node["bmc"]["host"] = MAPPING[node["name"]]["bmc_fqdn"]
    cluster_path.write_text(yaml.safe_dump(cluster))
    expected = {MAPPING[NAMES[0]]["bmc_fqdn"]: "192.0.2.101",
                MAPPING[NAMES[1]]["bmc_fqdn"]: "192.0.2.201"}
    looked_up = []

    def resolve(host, *args, **kwargs):
        looked_up.append(host)
        ip = expected[host] if matching else "192.0.2.99"
        return [(2, 1, 6, "", (ip, 0))]

    monkeypatch.setattr(endpoint_naming.socket, "getaddrinfo", resolve)
    if not matching:
        with pytest.raises(ValueError, match="BMC DNS does not match workbook IP"):
            prepare(workspace)
        assert not (root / "output/2-8-5-200/test01/ocp/inspection/fleet" / RUN).exists()
        return
    bundle = prepare(workspace)
    plan = adapter.read_yaml(bundle / "plan.yml")["inspect_fleet_plan"]
    assert [node["name"] for node in plan["authorized_nodes"]] == NAMES
    assert [node["bmc"]["host"] for node in plan["authorized_nodes"]] == list(expected)
    assert [node["devices"]["bmc_ip"] for node in plan["authorized_nodes"]] == list(expected.values())
    assert looked_up == list(expected)
    assert [node["devices"]["bond_ip"] for node in plan["authorized_nodes"]] == ["198.51.100.101", "198.51.100.201"]


@pytest.mark.parametrize("case", ["matching", "wrong_run", "wrong_nodes", "changed_network", "changed_bmc_ip",
                                 "existing_evidence"])
def test_exact_authority_and_sealed_inputs(workspace, case):
    bundle = prepare(workspace)
    seal(bundle)
    run, names = RUN, NAMES
    if case == "wrong_run":
        run = "20000101T120001Z"
    elif case == "wrong_nodes":
        names = NAMES[:1]
    elif case == "changed_network":
        (bundle / "nmstate" / f"{NAMES[0]}.yaml").write_text("changed input")
    elif case == "changed_bmc_ip":
        document = adapter.read_yaml(bundle / "plan.yml")
        document["inspect_fleet_plan"]["authorized_nodes"][0]["devices"]["bmc_ip"] = "192.0.2.99"
        (bundle / "plan.yml").write_text(yaml.safe_dump(document))
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
    assert f"@{bundle}/preflight-vars.yaml" not in arguments
    authority = next(json.loads(arg) for arg in arguments if arg.startswith('{"fleet_inspection_driver_physical_authorization"'))
    assert authority["fleet_inspection_driver_physical_authorization"]["node_names"] == NAMES


@pytest.mark.parametrize("authorized", [True, False])
def test_actual_adapter_guard_with_harmless_collection_entry(workspace, authorized):
    """Run production per-node network preparation; stop before runtime or BMC operations."""
    root, _ = workspace
    bundle = prepare(workspace)
    seal(bundle)
    shutil.copytree(ROOT / "scripts", root / "scripts")
    playbooks = root / "playbooks"
    playbooks.mkdir()
    marker = root / "entered.yaml"
    stub = playbooks / "collection-fixture.yml"
    stub.write_text(yaml.safe_dump([{
        "name": "Prepare selected networks without any service or hardware",
        "hosts": "bootstrap", "gather_facts": False,
        "tasks": [{"name": "Validate and prepare real per-node collection networking",
                   "ansible.builtin.include_role": {
                       "name": "rhvp.baremetal_ocp.fleet_inspection_driver", "tasks_from": "validate_plan"},
                   "vars": {"fleet_inspection_driver_prepare_network": True}},
                  {"name": "Record exact selected handoff", "ansible.builtin.copy": {
            "dest": str(marker), "mode": "0600",
            "content": "{{ {'names': fleet_inspection_driver_bmc_credentials.keys() | list, "
                       "'networks': __fleet_inspection_driver_prepared_nodes, "
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
                             "-e", f"@{bundle}/plan.yml",
                             "-e", f"@{inputs_path}"], text=True, capture_output=True, timeout=60)
    if authorized:
        assert result.returncode == 0, result.stdout + result.stderr
        handoff = yaml.safe_load(marker.read_text())
        assert handoff["names"] == NAMES
        assert handoff["authority"]["node_names"] == NAMES
        nodes = adapter.read_yaml(bundle / "plan.yml")["inspect_fleet_plan"]["authorized_nodes"]
        networks = handoff["networks"]
        assert set(networks) == set(NAMES)
        assert [networks[name]["callback_ip"] for name in NAMES] == ["198.51.100.101", "198.51.100.201"]
        for node in nodes:
            network = networks[node["name"]]
            assert network["nmstate_path"] == str(bundle / "nmstate" / f"{node['name']}.yaml")
            assert network["early_network_path"] == str(bundle / "early-network" / f"{node['name']}.yaml")
            assert f"ip={node['devices']['bond_ip']}::" in network["kernel_append_params"]
            for interface in node["interfaces"]:
                assert f"ifname={interface['name']}:{interface['macAddress']}" in network["kernel_append_params"]
            other = next(candidate for candidate in nodes if candidate["name"] != node["name"])
            assert all(interface["macAddress"] not in network["kernel_append_params"]
                       for interface in other["interfaces"])
        assert not Path(adapter.read_yaml(bundle / "plan.yml")["inspect_fleet_plan"]["artifact_root"]).exists()
    else:
        assert result.returncode != 0
        assert not marker.exists()


@pytest.mark.parametrize("purpose", ["auto", "node-lifecycle"])
def test_node_lifecycle_inspection_does_not_read_obsolete_abi_candidates(workspace, purpose):
    root, collection = workspace
    # A declared Day 2 worker remains a valid inspection target.
    import openpyxl
    workbook_path = root / "input/2-8-5-200/test01/2-8-5-200.xlsx"
    workbook = openpyxl.load_workbook(workbook_path)
    sheet = workbook["Nodes"]
    headers = [cell.value for cell in sheet[1]]
    for row in sheet.iter_rows(min_row=2):
        if row[headers.index("Name")].value == NAMES[1]:
            row[headers.index("Include in Initial ABI")].value = "No"
    workbook.save(workbook_path)
    workbook.close()
    site_path = root / "output/2-8-5-200/test01/inventory/group_vars/all/main.yml"
    site = adapter.read_yaml(site_path)
    site["devices"][NAMES[1]]["include_in_initial_abi"] = "No"
    site_path.write_text(yaml.safe_dump(site))
    candidates = root / "output/2-8-5-200/test01/ocp"
    candidates.mkdir()
    path = candidates / "agent-config.yaml"
    path.write_text("obsolete ABI input\n")
    if purpose == "auto":
        with pytest.raises((ValueError, KeyError)):
            adapter.prepare(root, "2-8-5-200", "test01", NAMES[1:], RUN, collection, 1, purpose)
    else:
        bundle = adapter.prepare(root, "2-8-5-200", "test01", NAMES[1:], RUN, collection, 1, purpose)
        document = adapter.read_yaml(bundle / "plan.yml")
        assert document["inspect_fleet_plan"]["inspection_purpose"] == purpose
        assert "abi_handoff" not in document["inspect_fleet_plan"]
        assert str(path) not in document["fleet_adapter_source_sha256"]
        assert [n["name"] for n in document["inspect_fleet_plan"]["authorized_nodes"]] == NAMES[1:]
    assert path.read_text() == "obsolete ABI input\n"


def test_httpd_snapshot_is_sealed_and_site_changes_apply_only_to_future_preparations(workspace):
    root, _ = workspace
    policy_path = root / "input/2-8-5-200/test01/ironic-httpd.yml"
    policy_path.write_text(yaml.safe_dump({"ironic_deploy_httpd_tuning": True}))
    bundle = prepare(workspace)
    plan = adapter.read_yaml(bundle / "plan.yml")["inspect_fleet_plan"]
    assert plan["httpd_tuning"]["ironic_deploy_httpd_tuning"] is True
    assert plan["httpd_tuning"]["ironic_deploy_httpd_manage_sysctl"] is False
    assert "HTTPD profile: True" in (bundle / "review.txt").read_text()
    seal(bundle)
    policy_path.write_text("ironic_deploy_httpd_tuning: false\n")
    adapter.authorize(bundle, RUN, NAMES)
    preflight = adapter.read_yaml(bundle / "preflight-vars.yaml")
    preflight["ironic_deploy_httpd_manage_sysctl"] = True
    (bundle / "preflight-vars.yaml").write_text(yaml.safe_dump(preflight))
    with pytest.raises(ValueError, match="Prepared handoff changed"):
        adapter.authorize(bundle, RUN, NAMES)


def test_inspection_seal_rejects_policy_different_from_reviewed_snapshot(workspace, monkeypatch):
    root, _ = workspace
    (root / "input/2-8-5-200/test01/ironic-httpd.yml").write_text("ironic_deploy_httpd_tuning: true\n")
    bundle = prepare(workspace)
    monkeypatch.setattr(adapter, "seal", lambda bundle: None)
    seal(bundle)
    preflight = adapter.read_yaml(bundle / "preflight-vars.yaml")
    preflight["ironic_deploy_httpd_tuning"] = False
    (bundle / "preflight-vars.yaml").write_text(yaml.safe_dump(preflight))
    # Restore the actual seal function before checking its input contract.
    monkeypatch.undo()
    with pytest.raises(ValueError, match="HTTPD preflight settings differ"):
        adapter.seal(bundle)


def generated_initial_fleet(workspace, include_all=False):
    """Generate matching candidates and declarations for the seven-host example."""
    import openpyxl
    root, collection = workspace
    base = root / 'output/2-8-5-200/test01'
    site_path = base / 'inventory/group_vars/all/main.yml'
    site = adapter.read_yaml(site_path)
    if include_all:
        workbook_path = root / 'input/2-8-5-200/test01/2-8-5-200.xlsx'
        workbook = openpyxl.load_workbook(workbook_path)
        for row in workbook['Nodes'].iter_rows(min_row=2):
            row[11].value = 'Yes'
        workbook.save(workbook_path)
        workbook.close()
        for device in site['devices'].values():
            device['include_in_initial_abi'] = 'Yes'
        site_path.write_text(yaml.safe_dump(site))
    settings = adapter.read_yaml(root / 'input/2-8-5-200/test01/ocp-settings.yml')
    roles = adapter.OCP.build_role_map(settings, site['devices'], '2-8-5-200', 'test01')
    initial, _ = adapter.OCP.split_abi_membership(roles, site['devices'])
    cluster_path = collection / 'inventories/test01/cluster-vars.yaml'
    cluster = adapter.read_yaml(cluster_path)
    cluster['ocp_nodes'] = [
        {'name': name, 'bmc': {'driver': 'idrac', 'host': device['eth0_ip']}}
        for name, device in site['devices'].items()
    ]
    cluster_path.write_text(yaml.safe_dump(cluster))
    (base / 'ocp').mkdir(exist_ok=True)
    agent = adapter.OCP.build_agent_config(settings, initial, {}, site, '2-8-5-200')
    (base / 'ocp/agent-config.yaml').write_text(yaml.safe_dump(agent))
    (base / 'ocp/install-config.yaml').write_text(yaml.safe_dump({
        'metadata': {'name': settings['cluster']['name']},
        'baseDomain': settings['cluster']['domain'],
        'controlPlane': {'replicas': sum(role == 'control_plane' for role in initial.values())},
        'compute': [{'replicas': sum(role != 'control_plane' for role in initial.values())}],
        'networking': {'machineNetwork': [{'cidr': site['common']['cpu_network']}]},
        'platform': {'baremetal': {'apiVIPs': [settings['cluster']['api_vip']],
                                  'ingressVIPs': [settings['cluster']['ingress_vip']]}}
    }))
    return sorted(initial)


@pytest.mark.parametrize('include_all,count', [(False, 6), (True, 7)])
def test_default_nodes_follow_verified_initial_abi_membership(workspace, include_all, count):
    root, collection = workspace
    expected = generated_initial_fleet(workspace, include_all)
    bundle = adapter.prepare(root, '2-8-5-200', 'test01', None, RUN, collection, 2)
    plan = adapter.read_yaml(bundle / 'plan.yml')['inspect_fleet_plan']
    assert plan['expected_node_count'] == count
    assert [node['name'] for node in plan['authorized_nodes']] == expected
    assert plan['abi_handoff']['initial_node_names'] == expected
    assert 'Concurrency: 2' in (bundle / 'review.txt').read_text()
    assert not (root / '.era-context').exists()  # Not selected before preflight/seal.
    if include_all:
        assert sum(node['role'] == 'master' for node in plan['authorized_nodes']) == 3
        assert sum(node['role'] == 'worker' for node in plan['authorized_nodes']) == 4


@pytest.mark.parametrize('case', ['missing', 'stale_agent', 'stale_membership'])
def test_automatic_selection_rejects_missing_or_stale_abi_inputs(workspace, case):
    import openpyxl
    root, collection = workspace
    generated_initial_fleet(workspace)
    path = root / 'output/2-8-5-200/test01/ocp/agent-config.yaml'
    if case == 'missing':
        path.unlink()
        path.with_name('install-config.yaml').unlink()
    elif case == 'stale_agent':
        document = adapter.read_yaml(path)
        document['hosts'][0]['hostname'] = 'unexpected.example.com'
        path.write_text(yaml.safe_dump(document))
    else:
        path = root / 'input/2-8-5-200/test01/2-8-5-200.xlsx'
        workbook = openpyxl.load_workbook(path)
        for row in workbook['Nodes'].iter_rows(min_row=2):
            if row[1].value == 'server-gpu-03':
                row[11].value = 'Yes'
        workbook.save(path)
        workbook.close()
    with pytest.raises(ValueError):
        adapter.prepare(root, '2-8-5-200', 'test01', None, RUN, collection, 2)
    assert not (root / 'output/2-8-5-200/test01/ocp/inspection/fleet' / RUN).exists()
    assert not (root / '.era-context').exists()


def test_node_lifecycle_still_requires_explicit_scope(workspace):
    root, collection = workspace
    with pytest.raises(ValueError, match='requires explicit nodes'):
        adapter.prepare(root, '2-8-5-200', 'test01', None, RUN, collection, 1, 'node-lifecycle')


def test_explicit_node_subset_and_run_are_retained(workspace):
    root, collection = workspace
    bundle = adapter.prepare(root, '2-8-5-200', 'test01', NAMES[1:], RUN, collection, 1)
    plan = adapter.read_yaml(bundle / 'plan.yml')['inspect_fleet_plan']
    assert plan['run_id'] == RUN
    assert [node['name'] for node in plan['authorized_nodes']] == NAMES[1:]


def test_fresh_run_is_utc_and_existing_directory_is_refused(workspace, monkeypatch):
    from datetime import datetime, timezone
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz == timezone.utc
            return cls(2000, 1, 1, 12, 0, 0, tzinfo=tz)
    monkeypatch.setattr(adapter, 'datetime', Clock)
    root, collection = workspace
    bundle = adapter.prepare(root, '2-8-5-200', 'test01', NAMES, '', collection, 2)
    before = (bundle / 'plan.yml').read_bytes()
    assert bundle.name == RUN
    with pytest.raises(FileExistsError):
        adapter.prepare(root, '2-8-5-200', 'test01', NAMES, '', collection, 2)
    assert (bundle / 'plan.yml').read_bytes() == before


@pytest.mark.parametrize('old_context,retain_os', [
    (None, False),
    ('arch: 2-8-5-200\nsite: test01\nserver_os: rhcos\nfleet_run_id: 19990101T000000Z\n', True),
    ('arch: 2-8-5-200\nsite: other-site\nserver_os: rhcos\n', False),
])
def test_only_sealed_preparation_is_selected_and_context_preserved(workspace, old_context, retain_os):
    root, _ = workspace
    context = root / '.era-context'
    if old_context is not None:
        context.write_text(old_context)
    bundle = prepare(workspace)
    with pytest.raises(ValueError):
        adapter.select_prepared(root, '2-8-5-200', 'test01', bundle)
    assert context.read_text() == old_context if old_context is not None else not context.exists()
    seal(bundle)
    assert adapter.select_prepared(root, '2-8-5-200', 'test01', bundle) == RUN
    value = adapter.CONTEXT.read_context(context)
    assert value['arch'] == '2-8-5-200' and value['site'] == 'test01'
    assert value['fleet_run_id'] == RUN
    assert (value.get('server_os') == 'rhcos') == retain_os
    selected = context.read_bytes()
    adapter.select_prepared(root, '2-8-5-200', 'test01', bundle)
    assert context.read_bytes() == selected
    assert not (root / 'output/2-8-5-200/test01/reports/inspection' / RUN).exists()


@pytest.mark.parametrize('case', ['context_changed', 'seal_changed', 'source_changed',
                                 'foreign_site', 'executed_run', 'context_symlink'])
def test_selection_failure_preserves_context(workspace, case):
    root, _ = workspace
    context = root / '.era-context'
    context.write_text('arch: 2-8-5-200\nsite: test01\nserver_os: rhcos\n')
    bundle = prepare(workspace)
    seal(bundle)
    if case == 'context_changed':
        context.write_text('arch: 2-8-5-200\nsite: other-site\n')
    elif case == 'seal_changed':
        (bundle / 'nmstate' / f'{NAMES[0]}.yaml').write_text('changed')
    elif case == 'source_changed':
        (root / 'input/2-8-5-200/test01/ocp-settings.yml').write_text('changed')
    elif case == 'executed_run':
        (root / 'output/2-8-5-200/test01/reports/inspection' / RUN).mkdir(parents=True)
    elif case == 'context_symlink':
        original = root / 'original-context'
        context.rename(original)
        context.symlink_to(original)
    before = context.read_bytes()
    with pytest.raises(ValueError):
        adapter.select_prepared(root, '2-8-5-200', 'other-site' if case == 'foreign_site' else 'test01', bundle)
    assert context.read_bytes() == before


@pytest.fixture
def make_preparation(workspace):
    root, collection = workspace
    expected = generated_initial_fleet(workspace, include_all=True)
    shutil.copyfile(ROOT / 'Makefile', root / 'Makefile')
    shutil.copytree(ROOT / 'scripts', root / 'scripts')
    (root / 'vars').mkdir()
    shutil.copyfile(adapter.OCP.DISK_DEFAULTS_PATH, root / 'vars' / adapter.OCP.DISK_DEFAULTS_PATH.name)
    (root / '.era-context').write_text(
        'arch: 2-8-5-200\nsite: test01\nserver_os: rhcos\nfleet_run_id: 19990101T000000Z\n')
    bin_dir = root / 'bin'
    bin_dir.mkdir()
    script = bin_dir / 'ansible-playbook'
    script.write_text('''#!/usr/bin/env python3
import importlib.util, os, sys
from pathlib import Path
assert sys.argv[1] == 'playbooks/prepare-fleet-inspection.yml'
bundle = Path(sys.argv[sys.argv.index('-i') + 1]).parent
if os.environ.get('FAIL_PREFLIGHT'):
    sys.exit(1)
spec = importlib.util.spec_from_file_location('local_adapter', 'scripts/prepare-fleet-inspection.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
doc = mod.read_yaml(bundle / 'plan.yml')
node = doc['inspect_fleet_plan']['authorized_nodes'][0]
mod.write_yaml(bundle / 'preflight-vars.yaml', {
    'inspection_preflight_provenance': {
        'installer_version': doc['fleet_adapter_expected_version'],
        'callback_target_ip': node['devices']['bond_ip']},
    'preflight_inspection_nodes': [node],
    **doc['inspect_fleet_plan'].get('httpd_tuning', {})})
mod.write_yaml(bundle / 'callback-routes.yaml', {'fixture': 'no route or hardware calls'})
mod.seal(bundle)
''')
    script.chmod(0o700)
    env = {key: value for key, value in os.environ.items()
           if not key.startswith('FLEET_') and key not in {
               'ARCH', 'SITE', 'SERVER_OS', 'MAKEFLAGS', 'MFLAGS', 'MAKEOVERRIDES',
               'RHV_NATIVE_OPERATION_LOCK_FD', 'RHV_NATIVE_OPERATION_SITE'}}
    env['PATH'] = f'{bin_dir}:{env["PATH"]}'
    return root, collection, expected, env


@pytest.mark.parametrize('case', ['automatic', 'explicit', 'preflight_failure'])
def test_make_preparation_selects_only_after_success(make_preparation, case):
    root, collection, expected, env = make_preparation
    context = root / '.era-context'
    before = context.read_bytes()
    args = ['make', '--no-print-directory', 'prepare-fleet-inspection',
            f'RHV_BAREMETAL_OCP_ROOT={collection}']
    if case == 'explicit':
        args.extend([f'FLEET_RUN_ID={RUN}', f'FLEET_NODES={",".join(NAMES)}', 'FLEET_CONCURRENCY=1'])
        expected = NAMES
    if case == 'preflight_failure':
        env['FAIL_PREFLIGHT'] = '1'
    result = subprocess.run(args, cwd=root, env=env, capture_output=True, text=True, timeout=30)
    if case == 'preflight_failure':
        assert result.returncode != 0
        assert context.read_bytes() == before
        assert 'Selected inspection run:' not in result.stdout
        return
    assert result.returncode == 0, result.stdout + result.stderr
    selected = adapter.CONTEXT.read_context(context)
    assert selected['server_os'] == 'rhcos'
    run = selected['fleet_run_id']
    assert run != '19990101T000000Z'
    if case == 'explicit':
        assert run == RUN
    bundle = root / 'output/2-8-5-200/test01/ocp/inspection/fleet' / run
    assert (bundle / 'prepared.yaml').exists()
    plan = adapter.read_yaml(bundle / 'plan.yml')['inspect_fleet_plan']
    assert [node['name'] for node in plan['authorized_nodes']] == expected
    assert plan['concurrency_limit'] == (1 if case == 'explicit' else 2)
    assert not Path(plan['artifact_root']).exists()


class InspectionTerminal(io.StringIO):
    def __init__(self, answer, on_read=None):
        super().__init__(answer)
        self.on_read = on_read

    def isatty(self):
        return True

    def readline(self, *args):
        if self.on_read is not None:
            self.on_read()
        return super().readline(*args)


def test_confirmation_reviews_seven_hosts_and_returns_exact_authority(workspace):
    root, collection = workspace
    expected = generated_initial_fleet(workspace, include_all=True)
    (root / 'input/2-8-5-200/test01/ironic-httpd.yml').write_text(
        'ironic_deploy_httpd_tuning: true\nironic_deploy_httpd_manage_sysctl: true\n')
    bundle = adapter.prepare(root, '2-8-5-200', 'test01', None, RUN, collection, 7)
    seal(bundle)
    output = io.StringIO()

    result = adapter.confirm_inspection(bundle, RUN, InspectionTerminal('inspect\n'), output)

    assert result == adapter.authorize(bundle, RUN, expected)
    assert f'Inspection run: {RUN}' in output.getvalue()
    assert f'Nodes (7): {", ".join(expected)}' in output.getvalue()
    assert 'Concurrency: 7' in output.getvalue()
    assert 'HTTPD profile: True; host sysctls: True' in output.getvalue()
    assert not (root / 'output/2-8-5-200/test01/reports/inspection' / RUN).exists()


@pytest.mark.parametrize('answer', ['', '\n', 'no\n', 'yes\n', 'INSPECT\n'])
def test_confirmation_cancellation_preserves_preparation(workspace, answer):
    bundle = prepare(workspace)
    seal(bundle)
    before = {path.relative_to(bundle): path.read_bytes()
              for path in bundle.rglob('*') if path.is_file()}
    with pytest.raises(ValueError, match='cancelled'):
        adapter.confirm_inspection(bundle, RUN, InspectionTerminal(answer), io.StringIO())
    assert before == {path.relative_to(bundle): path.read_bytes()
                      for path in bundle.rglob('*') if path.is_file()}


def test_piped_confirmation_is_rejected_without_consuming_answer(workspace):
    bundle = prepare(workspace)
    seal(bundle)
    answers = io.StringIO('inspect\n')
    with pytest.raises(ValueError, match='requires a terminal'):
        adapter.confirm_inspection(bundle, RUN, answers, io.StringIO())
    assert answers.tell() == 0


@pytest.mark.parametrize('case', ['wrong_run', 'changed_network', 'missing_seal'])
def test_confirmation_rejects_invalid_preparation_before_prompt(workspace, case):
    bundle = prepare(workspace)
    seal(bundle)
    run = RUN
    if case == 'wrong_run':
        run = '20000101T130000Z'
    elif case == 'changed_network':
        (bundle / 'nmstate' / f'{NAMES[0]}.yaml').write_text('changed')
    else:
        (bundle / 'prepared.yaml').unlink()
    output = io.StringIO()
    with pytest.raises(ValueError):
        adapter.confirm_inspection(bundle, run, InspectionTerminal('inspect\n'), output)
    assert not output.getvalue()


@pytest.mark.parametrize('reseal', [False, True])
def test_confirmation_rejects_inputs_changed_while_prompting(workspace, reseal):
    bundle = prepare(workspace)
    seal(bundle)

    def change():
        path = bundle / 'plan.yml'
        doc = adapter.read_yaml(path)
        doc['inspect_fleet_plan']['concurrency_limit'] = 1
        path.write_text(yaml.safe_dump(doc))
        if reseal:
            receipt_path = bundle / 'prepared.yaml'
            receipt = adapter.read_yaml(receipt_path)
            receipt['sha256'] = adapter.handoff_files(bundle, doc)
            receipt_path.write_text(yaml.safe_dump(receipt))

    with pytest.raises(ValueError, match='changed'):
        adapter.confirm_inspection(bundle, RUN, InspectionTerminal('inspect\n', change), io.StringIO())


def test_confirmation_keyboard_interrupt_cancels(workspace):
    bundle = prepare(workspace)
    seal(bundle)
    def interrupt():
        raise KeyboardInterrupt
    with pytest.raises(ValueError, match='cancelled'):
        adapter.confirm_inspection(bundle, RUN, InspectionTerminal('', interrupt), io.StringIO())


@pytest.fixture
def inspection_command(workspace):
    root, collection = workspace
    expected = generated_initial_fleet(workspace, include_all=True)
    (root / '.era-context').write_text('arch: 2-8-5-200\nsite: test01\nserver_os: rhcos\n')
    bundle = adapter.prepare(root, '2-8-5-200', 'test01', None, RUN, collection, 7)
    seal(bundle)
    adapter.select_prepared(root, '2-8-5-200', 'test01', bundle)
    shutil.copyfile(ROOT / 'Makefile', root / 'Makefile')
    shutil.copytree(ROOT / 'scripts', root / 'scripts')
    (collection / 'inventories/test01/secrets.yaml').write_text('unused fixture')
    bin_dir = root / 'bin'
    bin_dir.mkdir()
    marker = root / 'executed.json'
    executable = bin_dir / 'ansible-playbook'
    executable.write_text('#!/usr/bin/env python3\nimport json,os,sys\nfrom pathlib import Path\n'
                          'Path(os.environ["FLEET_TEST_CAPTURE"]).write_text(json.dumps(sys.argv[1:]))\n')
    executable.chmod(0o700)
    env = {key: value for key, value in os.environ.items()
           if not key.startswith('FLEET_') and key not in {
               'ARCH', 'SITE', 'SERVER_OS', 'MAKEFLAGS', 'MFLAGS', 'MAKEOVERRIDES'}}
    env.update(PATH=f'{bin_dir}:{env["PATH"]}', FLEET_TEST_CAPTURE=str(marker))
    argv = ['make', '--no-print-directory', 'inspect-fleet', f'RHV_BAREMETAL_OCP_ROOT={collection}']
    return root, bundle, expected, marker, env, argv


def run_inspection_terminal(root, env, argv, answer):
    """Use a real PTY for stdin, with execution replaced by the fixture recorder."""
    master, slave = pty.openpty()
    process = None
    try:
        process = subprocess.Popen(argv, cwd=root, env=env, stdin=slave,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        os.close(slave)
        slave = None
        transcript = bytearray()
        deadline = time.monotonic() + 15
        while b'Type inspect to authorize' not in transcript and time.monotonic() < deadline:
            ready, _, _ = select.select([process.stderr], [], [], 0.2)
            if ready:
                block = os.read(process.stderr.fileno(), 4096)
                if not block:
                    break
                transcript.extend(block)
        assert b'Type inspect to authorize' in transcript, transcript.decode()
        os.write(master, answer.encode())
        stdout, stderr = process.communicate(timeout=15)
        return subprocess.CompletedProcess(argv, process.returncode, stdout, transcript.decode() + stderr)
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.communicate()
        os.close(master)
        if slave is not None:
            os.close(slave)


@pytest.mark.parametrize('answer,accepted', [('inspect\n', True), ('\n', False)])
def test_make_inspect_uses_selected_scope_and_terminal_confirmation(inspection_command, answer, accepted):
    root, _, expected, marker, env, argv = inspection_command
    result = run_inspection_terminal(root, env, argv, answer)
    assert marker.exists() == accepted
    if accepted:
        assert result.returncode == 0, result.stdout + result.stderr
        arguments = json.loads(marker.read_text())
        authority = next(json.loads(arg) for arg in arguments
                         if arg.startswith('{"fleet_inspection_driver_physical_authorization"'))
        assert authority['fleet_adapter_authorized_run'] == RUN
        assert authority['fleet_adapter_authorized_nodes'] == expected
        assert 'Nodes (7):' in result.stderr and 'Concurrency: 7' in result.stderr
        assert 'Physical inspection will boot' not in result.stdout  # AUTH contains JSON only.
    else:
        assert result.returncode != 0
        assert 'cancelled' in result.stderr


@pytest.mark.parametrize('case', ['no_terminal', 'partial_run', 'partial_nodes', 'wrong_scope', 'explicit'])
def test_make_inspect_noninteractive_authorization_contract(inspection_command, case):
    root, _, expected, marker, env, argv = inspection_command
    if case in {'partial_run', 'wrong_scope', 'explicit'}:
        argv.append(f'FLEET_AUTHORIZE_RUN={RUN}')
    if case in {'partial_nodes', 'wrong_scope', 'explicit'}:
        names = expected[:1] if case == 'wrong_scope' else expected
        argv.append(f'FLEET_AUTHORIZE_NODES={",".join(names)}')
    result = subprocess.run(argv, cwd=root, env=env, input='inspect\n',
                            capture_output=True, text=True, timeout=15)
    assert marker.exists() == (case == 'explicit')
    if case == 'explicit':
        assert result.returncode == 0, result.stdout + result.stderr
    else:
        assert result.returncode != 0
        if case == 'no_terminal':
            assert 'requires a terminal' in result.stderr
        if case in {'partial_run', 'partial_nodes'}:
            assert 'supply both FLEET_AUTHORIZE_RUN and FLEET_AUTHORIZE_NODES' in result.stdout
    assert 'Type inspect to authorize' not in result.stderr
