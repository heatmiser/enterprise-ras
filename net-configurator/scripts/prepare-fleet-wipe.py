#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""Seal optional native metadata-cleaning preparation; never boot nodes or write disks."""

import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import sys

import yaml


ROOT = Path(__file__).resolve().parent.parent


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = load_module(ROOT / "scripts/validate-abi-handoff.py", "wipe_inspection_gate")
state = load_module(ROOT / "scripts/fleet-cleaning-state.py", "wipe_state")


def collection_hashes(collection):
    paths = list((collection / "roles/fleet_native_disk_clean_driver").rglob("*.yml"))
    gate.require(len(paths) == 12, "Native fleet executor is incomplete")
    paths += [collection / relative for relative in (
        "playbooks/wipe_fleet_install_disks.yml", "plugins/filter/fleet_native_disk_clean_result.py",
        "plugins/filter/fleet_native_disk_clean_plan.py",
        "plugins/filter/runtime_diagnostics.py",
        "roles/ironic_deploy/tasks/main.yml", "roles/ironic_deploy/tasks/deploy_ephemeral.yml",
        "roles/ironic_deploy/defaults/main.yml")]
    return {str(p.relative_to(collection)): gate.digest(p) for p in sorted(paths)}


def declared_oem(root, arch, site):
    """Require the workbook's explicit OEM to match generated OCP settings."""
    inputs = root / "input" / arch / site
    reader = load_module(ROOT / "scripts/init-ocp-settings.py", "wipe_settings_reader")
    oem = reader.read_ocp_settings(inputs / f"{arch}.xlsx").get("ocp_oem", "")
    settings_oem = gate.read_yaml(inputs / "ocp-settings.yml").get("oem")
    gate.require(isinstance(oem, str) and bool(oem.strip()) and isinstance(settings_oem, str)
                 and oem.strip().lower() == settings_oem.strip().lower(),
                 "Explicit workbook ocp_oem must match ocp-settings.yml oem")
    return oem.strip().lower()


def prepare(root, arch, site, inspection_run_id, wipe_run_id, names, collection_root):
    root = root.absolute()
    # This existing gate checks immutable physical evidence, exact initial ABI
    # membership, original source inputs, current manifests and terminal cleanup.
    _, _, receipt = gate.validate_handoff(root, arch, site, inspection_run_id)
    adapter = load_module(ROOT / "scripts/prepare-fleet-inspection.py", "wipe_inspection_adapter")
    adapter.validate_run(wipe_run_id)
    gate.require(sorted(names) == sorted(receipt["node_names"])
                 and len(names) == len(set(names)), "Require exact inspected initial membership")
    base = root / "output" / arch / site
    source = base / "reports/inspection" / inspection_run_id
    original = base / "ocp/inspection/fleet" / inspection_run_id
    bundle = base / "ocp/wipe/fleet" / wipe_run_id
    evidence_root = base / "reports/wipe" / wipe_run_id
    plugin_path = collection_root.absolute() / "plugins/filter/fleet_native_disk_clean_plan.py"
    plugin_hash = gate.digest(plugin_path)
    builder = load_module(plugin_path, "wipe_plan_builder")
    oem = declared_oem(root, arch, site)
    profile = builder.fleet_native_disk_clean_profile(oem)
    code_hashes = collection_hashes(collection_root.absolute())
    document = gate.read_yaml(original / "plan.yml")
    gate.require(all(node["bmc"]["driver"] in ("idrac", "redfish") if oem == "dell"
                     else node["bmc"]["driver"] == "redfish"
                     for node in document["inspect_fleet_plan"]["authorized_nodes"]),
                 "Reviewed BMC inputs do not match the OEM credential contract")
    provenance = gate.read_yaml(original / "preflight-vars.yaml")["inspection_preflight_provenance"]
    node_evidence = {}
    for name in names:
        raw = json.loads((source / name / "inventory-raw.json").read_text())
        node_evidence[name] = {
            "inventory": raw["inventory"],
            "disk_validation": gate.read_yaml(source / name / "validation.yaml")["disk_validation"],
        }
    source_hashes = {
        "inspection_receipt": gate.digest(source / "eligibility.yaml"),
        "inspection_prepared": gate.digest(original / "prepared.yaml"),
        "plan_builder": plugin_hash,
    }
    plan = builder.fleet_native_disk_clean_plan(inspection_run_id, wipe_run_id,
        document["inspect_fleet_plan"]["authorized_nodes"], node_evidence, source_hashes, oem)
    plan.update({"artifact_root": str(evidence_root), "inspection_artifact_root": str(source),
                 "manifest_sha256": receipt["manifest_sha256"]})
    # Revalidate after projecting evidence so altered files cannot be sealed into
    # a plan during preparation. No credential-bearing candidate data is written.
    _, _, current_receipt = gate.validate_handoff(root, arch, site, inspection_run_id)
    gate.require(current_receipt == receipt and source_hashes == {
        "inspection_receipt": gate.digest(source / "eligibility.yaml"),
        "inspection_prepared": gate.digest(original / "prepared.yaml"),
        "plan_builder": gate.digest(plugin_path)}, "Preparation sources changed while reading")
    for path in (bundle, evidence_root):
        gate.require(not any(parent.is_symlink() for parent in (path, *path.parents)), "Symlink wipe path")
        gate.require(not path.exists(), "Wipe preparation or evidence run already exists")
    bundle.parent.mkdir(parents=True, exist_ok=True)
    bundle.mkdir(mode=0o700)
    runtime = {"run_id": wipe_run_id, "artifact_root": str(evidence_root), "oem": oem, "native_profile": profile,
               "authorized_nodes": document["inspect_fleet_plan"]["authorized_nodes"],
               "collection_root": str(collection_root.absolute()), "collection_sha256": code_hashes,
               "source_sha256": document["fleet_adapter_source_sha256"],
               "cluster_vars": document["fleet_adapter_collection_vars"],
               "nmstate_dir": str(bundle / "nmstate"), "early_network_dir": str(bundle / "early-network"),
               "original_provenance": provenance,
               "timeouts": {"manage": 300, "clean": 1800, "api": 30, "run": 7200}}
    for directory in ("nmstate", "early-network"):
        (bundle / directory).mkdir(mode=0o700)
        for name in names:
            source_path = original / directory / f"{name}.yaml"
            gate.digest(source_path)
            shutil.copyfile(source_path, bundle / directory / f"{name}.yaml")
            (bundle / directory / f"{name}.yaml").chmod(0o600)
    write_document(bundle / "runtime-plan.yml", {"fleet_wipe_runtime_plan": runtime, "fleet_wipe_bundle": str(bundle),
                                                "fleet_wipe_arch": arch, "fleet_wipe_site": site})
    write_document(bundle / "hosts.yml", {"all": {"children": {"bootstrap": {"hosts": {
        "localhost": {"ansible_connection": "local"}}}}}})
    review = [f"Native all-disk metadata-cleaning preparation: {wipe_run_id}", f"Inspection source: {inspection_run_id}",
              "Status: PREPARED; execution ready: false; no physical authority",
              "SCOPE: ALL NATIVE ELIGIBLE DISKS AND PARTITIONS ON EVERY SELECTED NODE",
              "No installation-volume selector; no operator disk exclusions",
              "IPA native safeguards and device selection remain in force",
              "Manual clean step: deploy.erase_devices_metadata only",
              f"Workbook OEM: {oem}; native hardware type: {profile['driver']}",
              f"Boot: {profile['boot_interface']}; management: {profile['management_interface']}; "
              f"power: {profile['power_interface']}",
              "Credentials: native redfish_* driver_info; configured system IDs preserved",
              "Completion: accepted native request, return to manageable, durable outcome, verified cleanup",
              "Concurrency: 1; stop subsequent nodes after failure", ""]
    for node in plan["authorized_nodes"]:
        review.extend([node["name"], f"  Chassis serial: {node['host_identity']['serial_number']}",
                       "  Previously inspected disks (review information; native discovery selects at execution):"])
        for disk in node["observed_disks"]:
            review.append(f"    {disk['observed_device_name']}; model: {disk['model']}; "
                          f"bytes: {disk['size']}; serial: {disk['serial']}; WWN: {disk['wwn']}")
    for filename, text in {
        "plan.yml": yaml.safe_dump({"fleet_native_disk_clean_plan": plan}, sort_keys=False),
        "review.txt": "\n".join(review) + "\n",
    }.items():
        with (bundle / filename).open("x") as stream:
            stream.write(text)
        (bundle / filename).chmod(0o600)
    prepared = {"schema_version": 3, "operation": plan["operation"], "scope": plan["scope"], "run_id": wipe_run_id,
                "inspection_run_id": inspection_run_id,
                "execution_ready": False, "node_names": sorted(names), "source_sha256": source_hashes,
                "runtime_sealed": False,
                "sha256": bundle_hashes(bundle)}
    with (bundle / "prepared.yaml").open("x") as stream:
        stream.write(yaml.safe_dump(prepared, sort_keys=False))
    (bundle / "prepared.yaml").chmod(0o600)
    return bundle


def write_document(path, value):
    with path.open("x") as stream:
        yaml.safe_dump(value, stream, sort_keys=False)
    path.chmod(0o600)


def bundle_hashes(bundle):
    return {str(p.relative_to(bundle)): gate.digest(p) for p in sorted(bundle.rglob("*"))
            if p.is_file() and p.name != "prepared.yaml"}


def verify_sources(root, arch, site, bundle):
    plan = gate.read_yaml(bundle / "plan.yml")["fleet_native_disk_clean_plan"]
    gate.require(plan.get("schema_version") == 3 and plan.get("operation") == state.OPERATION
                 and plan.get("scope") == state.SCOPE
                 and plan.get("policy", {}).get("operator_disk_exclusions") == []
                 and plan.get("clean_steps") == [{"interface": "deploy", "step": "erase_devices_metadata", "args": {}}],
                 "Require fresh all-disk native preparation; installation-volume plans cannot execute")
    runtime = gate.read_yaml(bundle / "runtime-plan.yml")["fleet_wipe_runtime_plan"]
    builder = load_module(Path(runtime["collection_root"]) / "plugins/filter/fleet_native_disk_clean_plan.py",
                          "wipe_profile_verifier")
    oem = declared_oem(root, arch, site)
    gate.require(plan.get("oem") == oem and runtime.get("oem") == oem
                 and plan.get("native_profile") == builder.fleet_native_disk_clean_profile(oem)
                 and runtime.get("native_profile") == plan["native_profile"], "Native OEM profile binding changed")
    _, _, receipt = gate.validate_handoff(root, arch, site, plan["inspection_run_id"])
    gate.require(plan["manifest_sha256"] == receipt["manifest_sha256"]
                 and sorted(n["name"] for n in plan["authorized_nodes"]) == sorted(receipt["node_names"]), "Inspection binding changed")
    original = root / "output" / arch / site / "ocp/inspection/fleet" / plan["inspection_run_id"]
    expected = {"inspection_receipt": gate.digest(root / "output" / arch / site / "reports/inspection" /
                                                  plan["inspection_run_id"] / "eligibility.yaml"),
                "inspection_prepared": gate.digest(original / "prepared.yaml"),
                "plan_builder": gate.digest(Path(runtime["collection_root"]) / "plugins/filter/fleet_native_disk_clean_plan.py")}
    gate.require(plan["source_sha256"] == expected, "Native preparation source changed")
    for filename, wanted in runtime["source_sha256"].items():
        gate.require(gate.digest(Path(filename)) == wanted, "Source changed after preparation")
    gate.require(collection_hashes(Path(runtime["collection_root"])) == runtime["collection_sha256"], "Executor changed")
    original_nodes = gate.read_yaml(original / "plan.yml")["inspect_fleet_plan"]["authorized_nodes"]
    gate.require(runtime["authorized_nodes"] == original_nodes and runtime["run_id"] == plan["run_id"]
                 and runtime["artifact_root"] == plan["artifact_root"], "Runtime node binding changed")
    return plan, runtime


def seal(root, arch, site, bundle):
    plan, runtime = verify_sources(root, arch, site, bundle)
    preflight = gate.read_yaml(bundle / "preflight-vars.yaml")
    gate.require(preflight["inspection_preflight_provenance"] == runtime["original_provenance"], "Release/network provenance changed")
    routes = gate.read_yaml(bundle / "callback-routes.yaml")
    names = sorted(n["name"] for n in plan["authorized_nodes"])
    gate.require(sorted(routes["nodes"]) == names and len(routes.get("routes", [])) == len(names),
                 "Missing fresh callback routes")
    for route in routes["routes"]:
        node = route["item"]
        observed = json.loads(route["stdout"])
        gate.require(len(observed) == 1 and node in runtime["authorized_nodes"], "Wrong callback route node")
        route_data = observed[0]
        gate.require(route_data.get("dst") == node["devices"]["bond_ip"]
                     and route_data.get("dev") == preflight["ironic_deploy_provisioning_interface"]
                     and route_data.get("prefsrc", route_data.get("src")) == preflight["ironic_deploy_provisioning_ip"],
                     "Callback route differs from fresh preflight")
    prepared = gate.read_yaml(bundle / "prepared.yaml")
    gate.require(prepared.get("runtime_sealed") is False, "Preparation already sealed")
    for name, wanted in prepared["sha256"].items():
        gate.require(gate.digest(bundle / name) == wanted, "Seed preparation changed")
    prepared.update(runtime_sealed=True, sha256=bundle_hashes(bundle))
    state.write(bundle / "prepared.yaml", prepared)


def verify(root, arch, site, bundle):
    plan, runtime = verify_sources(root, arch, site, bundle)
    gate.require(bundle == root / "output" / arch / site / "ocp/wipe/fleet" / plan["run_id"], "Wrong preparation path")
    prepared = gate.read_yaml(bundle / "prepared.yaml")
    gate.require(prepared.get("schema_version") == 3 and prepared.get("operation") == state.OPERATION
                 and prepared.get("scope") == state.SCOPE
                 and prepared.get("runtime_sealed") is True and prepared.get("execution_ready") is False
                 and prepared["run_id"] == plan["run_id"] and prepared["inspection_run_id"] == plan["inspection_run_id"]
                 and prepared["sha256"] == bundle_hashes(bundle), "Incomplete or altered preparation")
    gate.require(gate.read_yaml(bundle / "preflight-vars.yaml")["inspection_preflight_provenance"]
                 == runtime["original_provenance"], "Wrong runtime provenance")
    return plan, runtime


def authorize(root, arch, site, bundle, run, names, start=False):
    plan, runtime = verify(root, arch, site, bundle)
    gate.require(run == plan["run_id"] and sorted(names) == sorted(n["name"] for n in plan["authorized_nodes"])
                 and len(names) == len(set(names)), "Exact wipe run and fleet authorization required")
    if start:
        state.begin(state.site_root(root, arch, site), plan, gate.digest(bundle / "prepared.yaml"))
    return {"fleet_wipe_authorization": {"confirmed": True, "action": "erase_devices_metadata",
            "run_id": run, "node_names": sorted(names), "preparation_sha256": state.fingerprint(plan),
            "prepared_sha256": gate.digest(bundle / "prepared.yaml")}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", default="prepare", choices=("prepare", "seal", "verify", "begin"))
    parser.add_argument("--root", type=Path, default=ROOT)
    for name in ("arch", "site"):
        parser.add_argument("--" + name, required=True)
    for name in ("inspection-run-id", "wipe-run-id", "nodes"):
        parser.add_argument("--" + name, default="")
    parser.add_argument("--collection-root", type=Path)
    parser.add_argument("--bundle", type=Path)
    args = parser.parse_args()
    try:
        gate.require(args.collection_root is not None if args.command == "prepare" else args.bundle is not None,
                     "Missing collection root or preparation bundle")
        adapter = load_module(ROOT / "scripts/prepare-fleet-inspection.py", "wipe_cli_adapter")
        if args.command == "prepare":
            names = adapter.selection(args.nodes)
            bundle = prepare(args.root, args.arch, args.site, args.inspection_run_id, args.wipe_run_id,
                             names, args.collection_root)
            print(f"Wipe preparation seeded: {bundle}; no physical authority")
        elif args.command == "seal":
            seal(args.root.absolute(), args.arch, args.site, args.bundle.absolute())
            print("Native runtime preparation sealed; no physical authority")
        elif args.command == "verify":
            verify(args.root.absolute(), args.arch, args.site, args.bundle.absolute())
        else:
            print(json.dumps(authorize(args.root.absolute(), args.arch, args.site, args.bundle.absolute(),
                                      args.wipe_run_id, adapter.selection(args.nodes), start=True)))
    except (ValueError, KeyError, IndexError, OSError, TypeError, ImportError, yaml.YAMLError):
        parser.exit(1, "ERROR: Wipe preparation rejected; review physical eligibility, unchanged inputs, "
                    "exact membership, all-disk scope, collection capability and fresh output paths.\n")


if __name__ == "__main__":
    main()
