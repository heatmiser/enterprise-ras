#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""Prepare explicit fleet inputs and verify their handoff; never contact hardware."""

import argparse
from datetime import datetime
import hashlib
import importlib.util
import ipaddress
import json
from pathlib import Path
import re
import sys

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import excel_parser  # noqa: E402
from endpoint_naming import build_endpoint_manifest, ipv4, verify_bmc_resolution  # noqa: E402
from utils import classify_net_profile  # noqa: E402


def load_module(filename):
    spec = importlib.util.spec_from_file_location(filename.replace("-", "_"), ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


OCP = load_module("generate-ocp-inventory.py")
SETTINGS = load_module("init-ocp-settings.py")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def selection(value):
    names = value.split(",")
    require(all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name) and name not in (".", "..")
                for name in names), "Supply an explicit comma-separated node list without whitespace")
    require(len(names) == len(set(names)), "Duplicate selected node")
    return names


def validate_run(run_id):
    require(bool(re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", run_id)), "Use UTC run ID YYYYMMDDTHHMMSSZ")
    datetime.strptime(run_id, "%Y%m%dT%H%M%SZ")


def read_yaml(path):
    require(path.is_file() and not path.is_symlink(), f"Require regular input: {path}")
    value = yaml.safe_load(path.read_text())
    require(isinstance(value, dict), f"Require mapping: {path}")
    return value


def write_yaml(path, value):
    with path.open("x") as stream:
        stream.write(yaml.safe_dump(value, sort_keys=False))
    path.chmod(0o640)


def fingerprint(path):
    require(path.is_file() and not path.is_symlink(), f"Require regular input: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_storage(policy):
    require(isinstance(policy, dict) and set(policy) == {
        "controller_id", "volume_id", "raid_type", "member_count"}, "Require reviewed storagePolicy fields")
    for key in ("controller_id", "volume_id"):
        value = policy[key]
        require(isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9_.:-]+", value))
                and value not in (".", ".."), f"Invalid {key}")
    require(policy["raid_type"] in ("RAID0", "RAID1", "RAID5", "RAID6", "RAID10", "RAID50", "RAID60"),
            "Unsupported RAID policy")
    require(type(policy["member_count"]) is int and policy["member_count"] > 0, "Invalid RAID member count")
    require(policy["raid_type"] != "RAID1" or policy["member_count"] == 2, "RAID1 requires two members")


def auxiliary_links(name, wires, device):
    """Review GPU/host-OOB links, including rows hidden in Air; exclude BMC endpoints."""
    links = []
    for wire in wires:
        if wire["system_name"] != name:
            continue
        profile = classify_net_profile(wire["net_profile"])
        if profile not in ("gpu", "oob"):
            continue
        if profile == "oob" and re.search(r"bmc|idrac", wire["nic_port"], re.IGNORECASE):
            continue
        alias, mac = wire["nic_alias"], wire["nic_mac"].lower()
        require(bool(alias) and bool(re.fullmatch(r"[0-9a-f]{2}(:[0-9a-f]{2}){5}", mac))
                and bool(wire["switch_name"]) and bool(wire["switch_port"]),
                f"Require reviewed {profile} host NIC alias, MAC, switch and port: {name}:{wire['nic_port']}")
        generated = [entry for entry in device.get("nic_alias_map", {}).get(profile, []) if entry["alias"] == alias]
        require(len(generated) == 1 and generated[0]["mac"].lower() == mac,
                f"Workbook {profile} identity differs from generated inventory: {name}:{alias}")
        links.append({"node": name, "interface": alias, "mac": mac,
                      "purpose": "gpu" if profile == "gpu" else "host_oob",
                      "expected_switch": wire["switch_name"], "expected_port": wire["switch_port"]})
    return links


def prepare(root, arch, site, names, run_id, collection_root, concurrency):
    """Validate every selected node before creating a new run-specific preparation directory."""
    validate_run(run_id)
    require(all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value) and value not in (".", "..")
                for value in (arch, site)), "Invalid architecture/site")
    require(type(concurrency) is int and 0 < concurrency <= len(names), "Invalid selected-node concurrency")
    workbook = root / "input" / arch / site / f"{arch}.xlsx"
    settings_path = workbook.parent / "ocp-settings.yml"
    inventory = root / "output" / arch / site / "inventory"
    inventory_path = inventory / "group_vars/all/main.yml"
    cluster_path = collection_root / "inventories" / site / "cluster-vars.yaml"
    settings = read_yaml(settings_path)
    site_vars = read_yaml(inventory_path)
    cluster = read_yaml(cluster_path)
    require(isinstance(settings.get("node_roles"), dict), "Explicit OCP node_roles required")
    roles = OCP.build_role_map(settings, site_vars.get("devices", {}), arch, site)
    version = SETTINGS.read_required_ocp_version(workbook)
    require(settings.get("cluster", {}).get("version") == version, "OCP settings version differs from canonical workbook")
    require(bool(cluster.get("ocp_ee_image")), "Collection site EE image required")
    source_files = [workbook, settings_path, inventory_path, cluster_path, OCP.DISK_DEFAULTS_PATH]
    for source in source_files:
        fingerprint(source)
    wb = excel_parser.load_workbook_safe(workbook, data_only=True)
    try:
        rows = excel_parser.parse_nodes(wb["Nodes"])
        wires = excel_parser._build_wiremap_row_list(wb["Wire Map"])
        cpu_network, cpu_gateway = SETTINGS.find_cpu_inband_vlan(excel_parser.parse_vlans(wb["VLANs & Profiles"]))
    finally:
        wb.close()
    require(site_vars.get("common", {}).get("cpu_network") == cpu_network
            and site_vars.get("common", {}).get("cpu_gateway") == cpu_gateway,
            "Generated CPU network differs from workbook; regenerate inventory")
    declared = cluster.get("ocp_nodes", [])
    require(isinstance(declared, list), "Collection ocp_nodes must be a list")
    disks = OCP.load_disk_defaults()
    endpoint_manifest = build_endpoint_manifest(settings, site_vars.get("devices", {}), roles)
    endpoints = endpoint_manifest["nodes"] if endpoint_manifest else {}
    overrides = settings.get("install_disk", {}).get("overrides", {})
    nodes, links, network_inputs = [], [], {}
    for name in names:
        matched = [node for node in rows if node["name"] == name]
        configured = [node for node in declared if node.get("name") == name]
        require(len(matched) == len(configured) == 1, f"Require unique workbook/collection node: {name}")
        row, configured = matched[0], configured[0]
        require(row["status"] == "Active", f"Disabled node: {name}")
        role = roles.get(name)
        require(role in OCP.INSTALLER_ROLE, f"Missing explicit OCP role: {name}")
        device = site_vars.get("devices", {}).get(name)
        require(isinstance(device, dict), f"Missing generated device: {name}")
        host_path = inventory / "host_vars" / f"{name}.yml"
        host = read_yaml(host_path)
        source_files.append(host_path)
        bmc = configured.get("bmc", {})
        require(isinstance(bmc, dict) and bool(bmc.get("driver")), f"Missing BMC policy: {name}")
        require(set(bmc) <= {"driver", "host", "system_id", "verify_ca"}, "Keep credentials in the existing vault")
        expected_bmc_ip = ipv4(row["mgmt_ip"])
        require(expected_bmc_ip == ipv4(host.get("ansible_host")) == ipv4(device.get("eth0_ip")),
                f"Workbook/inventory/BMC endpoint mismatch: {name}")
        effective_bmc = dict(bmc)
        if endpoints:
            endpoint = endpoints[name]
            require(bmc.get("host") in (expected_bmc_ip, endpoint["bmc"]["hostname"]),
                    f"Collection BMC name differs from reviewed endpoint: {name}")
            effective_bmc["host"] = endpoint["bmc"]["hostname"]
        verify_bmc_resolution(effective_bmc.get("host", ""), expected_bmc_ip)
        disk, fallback = OCP.resolve_disk(name, role, arch, settings.get("oem"), disks, overrides)
        require(not fallback and isinstance(disk, str) and bool(re.fullmatch(r"/dev/disk/by-path/[A-Za-z0-9_.:-]+", disk)),
                f"Reviewed literal by-path disk hint required: {name}")
        nmstate = OCP.build_inspection_nmstate_network_config(device, site_vars, nic_mode="real-hw")
        early = OCP.build_inspection_early_network_interfaces(device, nmstate, nic_mode="real-hw")
        callback = nmstate["interfaces"][0]["ipv4"]["address"][0]["ip"]
        ipaddress.ip_address(callback)
        node = {"name": name, "role": OCP.INSTALLER_ROLE[role], "bmc": effective_bmc,
                "devices": {"bond_ip": callback, "bmc_ip": expected_bmc_ip},
                "interfaces": [{"name": item["name"], "macAddress": item["mac"]} for item in early],
                "rootDeviceHints": {"deviceName": disk}}
        if "storagePolicy" in configured:
            validate_storage(configured["storagePolicy"])
            node["storagePolicy"] = configured["storagePolicy"]
        for identity in early:
            expected = [wire for wire in wires if wire["system_name"] == name and wire["nic_alias"] == identity["name"]]
            require(len(expected) == 1, f"Require one workbook CPU attachment: {name}:{identity['name']}")
            wire = expected[0]
            require(classify_net_profile(wire["net_profile"]) == "cpu"
                    and wire["nic_mac"].lower() == identity["mac"] and wire["switch_name"] and wire["switch_port"],
                    f"Workbook CPU identity differs from generated inventory: {name}:{identity['name']}")
            links.append({"node": name, "interface": identity["name"], "mac": identity["mac"],
                          "purpose": "cpu",
                          "expected_switch": wire["switch_name"], "expected_port": wire["switch_port"]})
        links.extend(auxiliary_links(name, wires, device))
        nodes.append(node)
        network_inputs[name] = (nmstate, {"interfaces": early})
    require(len({node["devices"]["bond_ip"] for node in nodes}) == len(nodes), "Duplicate callback addresses")
    require(len({link["mac"] for link in links}) == len(links), "Duplicate selected host NIC MACs")
    require(len({(link["node"], link["interface"]) for link in links}) == len(links),
            "Duplicate reviewed host NIC aliases")
    require(len({(link["expected_switch"], link["expected_port"]) for link in links}) == len(links),
            "Duplicate desired host NIC switch attachment")
    base = root / "output" / arch / site
    evidence = base / "reports/inspection" / run_id
    require(not evidence.exists() and not evidence.is_symlink(), "Evidence run already exists")
    bundle = base / "ocp/inspection/fleet" / run_id
    # Validate parent ownership before creating any preparation artifacts.
    require(not any(parent.is_symlink() for path in (bundle, evidence) for parent in (path, *path.parents)),
            "Symlink preparation/evidence parent")
    bundle.mkdir(parents=True, exist_ok=False)
    for directory in ("nmstate", "early-network"):
        (bundle / directory).mkdir()
    for name, (nmstate, early) in network_inputs.items():
        write_yaml(bundle / "nmstate" / f"{name}.yaml", nmstate)
        write_yaml(bundle / "early-network" / f"{name}.yaml", early)
    plan = {"run_id": run_id, "artifact_root": str(evidence), "authorized_nodes": nodes,
            "expected_node_count": len(nodes), "concurrency_limit": concurrency,
            "timeouts": {"manageable": 300, "inspection": 1200, "api_fetch": 30, "total_run": 3600},
            "wiremap": links, "switch_aliases": cluster.get("fleet_inspection_switch_aliases", {})}
    write_yaml(bundle / "plan.yml", {"inspect_fleet_plan": plan,
               "fleet_adapter_bundle": str(bundle), "fleet_adapter_expected_version": version,
               "fleet_adapter_collection_vars": str(cluster_path),
               "fleet_adapter_source_sha256": {str(path): fingerprint(path) for path in source_files},
               "fleet_inspection_driver_nmstate_dir": str(bundle / "nmstate"),
               "fleet_inspection_driver_early_network_dir": str(bundle / "early-network")})
    write_yaml(bundle / "hosts.yml", {"all": {"children": {"bootstrap": {"hosts": {
        "localhost": {"ansible_connection": "local"}}}}}})
    return bundle


def handoff_files(bundle, document):
    paths = [bundle / "plan.yml", bundle / "preflight-vars.yaml", bundle / "hosts.yml",
             bundle / "callback-routes.yaml"]
    for node in document["inspect_fleet_plan"]["authorized_nodes"]:
        paths.extend(bundle / directory / f"{node['name']}.yaml" for directory in ("nmstate", "early-network"))
    for path, expected in document["fleet_adapter_source_sha256"].items():
        require(fingerprint(Path(path)) == expected, f"Source changed after preparation: {path}")
    return {str(path): fingerprint(path) for path in paths}


def seal(bundle):
    document = read_yaml(bundle / "plan.yml")
    runtime = read_yaml(bundle / "preflight-vars.yaml")
    plan = document["inspect_fleet_plan"]
    provenance = runtime.get("inspection_preflight_provenance", {})
    require(provenance.get("installer_version") == document["fleet_adapter_expected_version"], "Preflight version mismatch")
    require(provenance.get("callback_target_ip") == plan["authorized_nodes"][0]["devices"]["bond_ip"],
            "Shared preflight callback mismatch")
    require([node.get("name") for node in runtime.get("preflight_inspection_nodes", [])]
            == [plan["authorized_nodes"][0]["name"]], "Shared preflight candidate mismatch")
    write_yaml(bundle / "prepared.yaml", {"run_id": plan["run_id"],
               "node_names": [node["name"] for node in plan["authorized_nodes"]],
               "sha256": handoff_files(bundle, document)})


def authorize(bundle, run_id, names):
    validate_run(run_id)
    document = read_yaml(bundle / "plan.yml")
    plan = document["inspect_fleet_plan"]
    receipt = read_yaml(bundle / "prepared.yaml")
    require(plan["run_id"] == receipt["run_id"] == run_id, "Authorization run mismatch")
    require(sorted(names) == sorted(node["name"] for node in plan["authorized_nodes"])
            == sorted(receipt["node_names"]), "Authorization node mismatch")
    require(receipt["sha256"] == handoff_files(bundle, document), "Prepared handoff changed; review a new run")
    evidence = Path(plan["artifact_root"])
    require(not evidence.exists() and not any(parent.is_symlink() for parent in (evidence, *evidence.parents)),
            "Evidence path already exists or has symlink parents")
    return {"fleet_inspection_driver_physical_authorization": {
        "confirmed": True, "action": "inspect", "run_id": run_id, "node_names": names},
        "fleet_adapter_authorized_run": run_id, "fleet_adapter_authorized_nodes": names}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare")
    for key in ("arch", "site", "nodes", "run-id", "collection-root"):
        prepare_parser.add_argument(f"--{key}", required=True)
    prepare_parser.add_argument("--concurrency", type=int, default=2)
    prepare_parser.add_argument("--root", type=Path, default=ROOT)
    seal_parser = commands.add_parser("seal")
    seal_parser.add_argument("--bundle", type=Path, required=True)
    authorize_parser = commands.add_parser("authorize")
    authorize_parser.add_argument("--bundle", type=Path, required=True)
    authorize_parser.add_argument("--run-id", required=True)
    authorize_parser.add_argument("--nodes", required=True)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            print(prepare(args.root.resolve(), args.arch, args.site, selection(args.nodes), args.run_id,
                          Path(args.collection_root).expanduser().resolve(), args.concurrency))
        elif args.command == "seal":
            seal(args.bundle.resolve())
        else:
            print(json.dumps(authorize(args.bundle.resolve(), args.run_id, selection(args.nodes))))
    except (ValueError, KeyError, OSError, TypeError, yaml.YAMLError) as exc:
        parser.exit(1, f"ERROR: {exc}\n")


if __name__ == "__main__":
    main()
