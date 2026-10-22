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


gate = load_module(ROOT / "scripts/validate-physical-inspection.py", "wipe_inspection_gate")
state = load_module(ROOT / "scripts/fleet-cleaning-state.py", "wipe_state")
HTTPD = load_module(ROOT / "scripts/ironic-httpd-settings.py", "wipe_httpd")
REDFISH = load_module(ROOT / "scripts/ironic-redfish-settings.py", "wipe_redfish")


def collection_hashes(collection):
    paths = list((collection / "roles/fleet_native_disk_clean_driver").rglob("*.yml"))
    required = {"defaults/main.yml", "meta/argument_specs.yml", "meta/main.yml"} | {
        "tasks/" + name + ".yml" for name in (
            "main", "validate", "runtime", "prepare_node", "enroll", "clean_node", "clean_batch",
            "manage_node", "submit_clean", "observe_clean", "cleanup_node", "cleanup_runtime", "finalize", "worker")}
    role = collection / "roles/fleet_native_disk_clean_driver"
    gate.require(required <= {str(path.relative_to(role)) for path in paths},
                 "Native fleet executor is incomplete")
    paths += [collection / relative for relative in (
        "playbooks/wipe_fleet_install_disks.yml", "playbooks/_fleet_native_disk_clean_worker.yml",
        "plugins/action/fleet_native_disk_clean_coordinator.py",
        "plugins/modules/fleet_native_disk_clean_coordinator.py",
        "plugins/modules/redfish_read.py",
        "plugins/module_utils/fleet_native_disk_clean_scheduler.py", "plugins/filter/fleet_native_disk_clean_result.py",
        "plugins/filter/fleet_native_disk_clean_plan.py",
        "plugins/filter/runtime_diagnostics.py",
        "roles/ironic_deploy/tasks/main.yml", "roles/ironic_deploy/tasks/deploy_ephemeral.yml",
        "roles/ironic_deploy/defaults/main.yml", "roles/ironic_deploy/meta/argument_specs.yml",
        "plugins/module_utils/redfish_retry.py", "plugins/module_utils/redfish_deadline.py",
        "plugins/modules/fleet_native_disk_clean_deadline.py",
        "roles/ironic_deploy/tasks/prepare_redfish_retry.yml",
        "roles/ironic_deploy/files/redfish_retry/rhvp_ironic_retry_launcher.py",
        "roles/ironic_deploy/files/native_disk_clean/rhvp_redfish_retry.py",
        "roles/ironic_deploy/tasks/prepare_httpd_tuning.yml",
        "roles/ironic_deploy/templates/ironic-tuning.conf.j2")]
    for name in ("ironic_node_power_off", "ironic_vmedia_detach"):
        paths += list((collection / "roles" / name).rglob("*.yml"))
        gate.require((collection / "roles" / name / "tasks/main.yml") in paths
                     and (collection / "roles" / name / "meta/argument_specs.yml") in paths,
                     "Native cleanup executor is incomplete")
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


def prepare(root, arch, site, inspection_run_id, wipe_run_id, names, collection_root, concurrency_limit=1):
    root = root.absolute()
    httpd = HTTPD.site(root, arch, site)
    # Physical readiness is independent of initial ABI membership. Legacy ABI
    # receipts remain valid sources under their original sealed input contract.
    document, receipt, receipt_name = gate.validate_handoff(root, arch, site, inspection_run_id)
    adapter = load_module(ROOT / "scripts/prepare-fleet-inspection.py", "wipe_inspection_adapter")
    adapter.validate_run(wipe_run_id)
    gate.require(names and len(names) == len(set(names))
                 and set(names) <= set(receipt["node_names"]), "Require explicit unique inspected nodes")
    base = root / "output" / arch / site
    source = base / "reports/inspection" / inspection_run_id
    original = base / "ocp/inspection/fleet" / inspection_run_id
    bundle = base / "ocp/wipe/fleet" / wipe_run_id
    evidence_root = base / "reports/wipe" / wipe_run_id
    redfish = REDFISH.site(root, arch, site, collection_root.absolute(), evidence_root / "redfish-retry")
    plugin_path = collection_root.absolute() / "plugins/filter/fleet_native_disk_clean_plan.py"
    plugin_hash = gate.digest(plugin_path)
    builder = load_module(plugin_path, "wipe_plan_builder")
    oem = declared_oem(root, arch, site)
    profile = builder.fleet_native_disk_clean_profile(oem)
    code_hashes = collection_hashes(collection_root.absolute())
    selected = [node for node in document["inspect_fleet_plan"]["authorized_nodes"] if node["name"] in names]
    gate.require(all(node["bmc"]["driver"] in ("idrac", "redfish") if oem == "dell"
                     else node["bmc"]["driver"] == "redfish"
                     for node in selected),
                 "Reviewed BMC inputs do not match the OEM credential contract")
    deadline_builder = load_module(collection_root.absolute() / "plugins/module_utils/redfish_deadline.py", "wipe_deadlines")
    deadline_contract = deadline_builder.contract(selected, redfish[REDFISH.ENABLED])
    provenance = gate.read_yaml(original / "preflight-vars.yaml")["inspection_preflight_provenance"]
    node_evidence = {}
    for name in names:
        raw = json.loads((source / name / "inventory-raw.json").read_text())
        node_evidence[name] = {
            "inventory": raw["inventory"],
            "disk_validation": gate.read_yaml(source / name / "validation.yaml")["disk_validation"],
        }
    source_hashes = {
        "inspection_receipt": gate.digest(source / receipt_name),
        "inspection_prepared": gate.digest(original / "prepared.yaml"),
        "plan_builder": plugin_hash,
    }
    plan = builder.fleet_native_disk_clean_plan(inspection_run_id, wipe_run_id,
        selected, node_evidence, source_hashes, oem, concurrency_limit)
    plan.update({"artifact_root": str(evidence_root), "inspection_artifact_root": str(source),
                 "manifest_sha256": receipt.get("manifest_sha256", {}), "redfish_retry": redfish, "redfish_deadlines": deadline_contract})
    # Revalidate after projecting evidence so altered files cannot be sealed into
    # a plan during preparation. No credential-bearing candidate data is written.
    current_document, current_receipt, current_name = gate.validate_handoff(root, arch, site, inspection_run_id)
    gate.require(current_document == document and current_name == receipt_name and current_receipt == receipt and source_hashes == {
        "inspection_receipt": gate.digest(source / receipt_name),
        "inspection_prepared": gate.digest(original / "prepared.yaml"),
        "plan_builder": gate.digest(plugin_path)}, "Preparation sources changed while reading")
    for path in (bundle, evidence_root):
        gate.require(not any(parent.is_symlink() for parent in (path, *path.parents)), "Symlink wipe path")
        gate.require(not path.exists(), "Wipe preparation or evidence run already exists")
    bundle.parent.mkdir(parents=True, exist_ok=True)
    bundle.mkdir(mode=0o700)
    runtime = {"redfish_retry": redfish, "redfish_deadlines": deadline_contract,
               "preparation_code_sha256": {str(path): gate.digest(path) for path in (
                   ROOT / "scripts/prepare-fleet-wipe.py", ROOT / "scripts/ironic-redfish-settings.py")},
               "concurrency_limit": plan["concurrency_limit"], "httpd_tuning": httpd, "run_id": wipe_run_id,
               "artifact_root": str(evidence_root), "oem": oem, "native_profile": profile,
               "authorized_nodes": selected, "inspection_receipt_name": receipt_name,
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
              f"Concurrency: {plan['concurrency_limit']}; independent node lifecycles; stop queued nodes after failure; observe in-flight nodes", ""]
    review.append(HTTPD.review(httpd))
    review.append(REDFISH.review(redfish))
    review.append("Conductor read deadlines: phase/run bounded when adaptive reads are enabled; expiry grants no cleanup authority")
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
    gate.require(runtime.get("redfish_retry") == plan.get("redfish_retry")
                 and isinstance(runtime.get("redfish_retry"), dict)
                 and REDFISH.normalize(runtime["redfish_retry"], Path(runtime["collection_root"]),
                                       runtime["redfish_retry"].get(REDFISH.STAGING)) == runtime["redfish_retry"],
                 "Redfish retry policy binding changed")
    preparation_paths = (ROOT / "scripts/prepare-fleet-wipe.py", ROOT / "scripts/ironic-redfish-settings.py")
    gate.require(runtime.get("preparation_code_sha256") == {str(path): gate.digest(path) for path in preparation_paths},
                 "Retry preparation code changed")
    builder = load_module(Path(runtime["collection_root"]) / "plugins/filter/fleet_native_disk_clean_plan.py",
                          "wipe_profile_verifier")
    limit = plan.get("concurrency_limit")
    gate.require(type(limit) is int and 1 <= limit <= len(plan["authorized_nodes"])
                 and runtime.get("concurrency_limit", 1) == limit,
                 "Native concurrency binding changed")
    oem = declared_oem(root, arch, site)
    gate.require(plan.get("oem") == oem and runtime.get("oem") == oem
                 and plan.get("native_profile") == builder.fleet_native_disk_clean_profile(oem)
                 and runtime.get("native_profile") == plan["native_profile"], "Native OEM profile binding changed")
    document, receipt, receipt_name = gate.validate_handoff(root, arch, site, plan["inspection_run_id"])
    names = [n["name"] for n in plan["authorized_nodes"]]
    gate.require(names and len(names) == len(set(names)) and set(names) <= set(receipt["node_names"])
                 and plan["manifest_sha256"] == receipt.get("manifest_sha256", {})
                 and runtime.get("inspection_receipt_name", "eligibility.yaml") == receipt_name,
                 "Inspection binding changed")
    original = root / "output" / arch / site / "ocp/inspection/fleet" / plan["inspection_run_id"]
    expected = {"inspection_receipt": gate.digest(root / "output" / arch / site / "reports/inspection" /
                                                  plan["inspection_run_id"] / receipt_name),
                "inspection_prepared": gate.digest(original / "prepared.yaml"),
                "plan_builder": gate.digest(Path(runtime["collection_root"]) / "plugins/filter/fleet_native_disk_clean_plan.py")}
    gate.require(plan["source_sha256"] == expected, "Native preparation source changed")
    for filename, wanted in runtime["source_sha256"].items():
        gate.require(gate.digest(Path(filename)) == wanted, "Source changed after preparation")
    gate.require(collection_hashes(Path(runtime["collection_root"])) == runtime["collection_sha256"], "Executor changed")
    original_nodes = [node for node in document["inspect_fleet_plan"]["authorized_nodes"] if node["name"] in names]
    gate.require(runtime["authorized_nodes"] == original_nodes and runtime["run_id"] == plan["run_id"]
                 and runtime["artifact_root"] == plan["artifact_root"], "Runtime node binding changed")
    deadlines = load_module(Path(runtime["collection_root"]) / "plugins/module_utils/redfish_deadline.py", "verify_wipe_deadlines")
    gate.require(runtime.get("redfish_deadlines") == plan.get("redfish_deadlines")
                 == deadlines.contract(runtime["authorized_nodes"], runtime["redfish_retry"][REDFISH.ENABLED]),
                 "Redfish deadline selection binding changed")
    return plan, runtime


def validate_preflight_provenance(preflight, runtime):
    HTTPD.verify_preflight(preflight, runtime.get("httpd_tuning", {}))
    """Retain the inspected release while binding fresh preflight to the selection."""
    expected = {
        **runtime["original_provenance"],
        "callback_target_ip": runtime["authorized_nodes"][0]["devices"]["bond_ip"],
    }
    gate.require(preflight["inspection_preflight_provenance"] == expected,
                 "Release provenance or selected callback changed")


def seal(root, arch, site, bundle):
    plan, runtime = verify_sources(root, arch, site, bundle)
    preflight = gate.read_yaml(bundle / "preflight-vars.yaml")
    validate_preflight_provenance(preflight, runtime)
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
    validate_preflight_provenance(gate.read_yaml(bundle / "preflight-vars.yaml"), runtime)
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
    parser.add_argument("--concurrency", type=int)
    parser.add_argument("--collection-root", type=Path)
    parser.add_argument("--bundle", type=Path)
    args = parser.parse_args()
    try:
        gate.require(args.collection_root is not None if args.command == "prepare" else args.bundle is not None,
                     "Missing collection root or preparation bundle")
        gate.require(args.command == "prepare" or args.concurrency is None,
                     "Concurrency may only be selected during preparation")
        adapter = load_module(ROOT / "scripts/prepare-fleet-inspection.py", "wipe_cli_adapter")
        if args.command == "prepare":
            names = adapter.selection(args.nodes)
            bundle = prepare(args.root, args.arch, args.site, args.inspection_run_id, args.wipe_run_id,
                             names, args.collection_root, 1 if args.concurrency is None else args.concurrency)
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
                    "explicit inspected selection, all-disk scope, collection capability and fresh output paths.\n")


if __name__ == "__main__":
    main()
