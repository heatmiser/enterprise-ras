#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""Seal optional native metadata-cleaning preparation; never boot nodes or write disks."""

import argparse
import importlib.util
import json
from pathlib import Path
import sys

import yaml


ROOT = Path(__file__).resolve().parent.parent


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = load_module(ROOT / "scripts/validate-abi-handoff.py", "wipe_inspection_gate")


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
    document = gate.read_yaml(original / "plan.yml")
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
        document["inspect_fleet_plan"]["authorized_nodes"], node_evidence, source_hashes)
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
    review = [f"Native metadata-cleaning preparation: {wipe_run_id}", f"Inspection source: {inspection_run_id}",
              "Status: PREPARED; execution ready: false; no physical authority",
              "Optional step: inspection-only ABI generation remains supported",
              "Manual clean step: deploy.erase_devices_metadata only",
              "Completion: nonempty target scope, SUCCEEDED, manageable, durable outcome, verified cleanup",
              "Concurrency: 1; stop subsequent nodes after failure",
              "Live disk and partition exclusions UNVERIFIED; proposals grant no execution authority", ""]
    for node in plan["authorized_nodes"]:
        disk = node["installation_volume"]
        review.extend([node["name"], f"  Chassis serial: {node['host_identity']['serial_number']}",
                       f"  Volume: {disk['by_path']}", f"  Serial: {disk['serial']}; WWN: {disk['wwn']}",
                       f"  Model: {disk['model']}; bytes: {disk['size']}",
                       f"  Observed name only: {node['observed_device_name']}",
                       f"  Observed whole disks: {len(node['observed_disks'])}; "
                       f"protected whole disks: {len(node['protected_disks'])}"])
        for disk in node["protected_disks"]:
            review.append(f"  Protect: {disk['by_path']}; serial: {disk['serial']}; "
                          f"WWN: {disk['wwn']}; model: {disk['model']}; bytes: {disk['size']}")
        for hint in node["proposed_skip_block_devices"]:
            review.append(f"  Proposed exclusion: serial={hint['serial']}; WWN={hint['wwn']}")
    for filename, text in {
        "plan.yml": yaml.safe_dump({"fleet_native_disk_clean_plan": plan}, sort_keys=False),
        "review.txt": "\n".join(review) + "\n",
    }.items():
        with (bundle / filename).open("x") as stream:
            stream.write(text)
        (bundle / filename).chmod(0o600)
    prepared = {"schema_version": 2, "operation": plan["operation"], "run_id": wipe_run_id,
                "inspection_run_id": inspection_run_id,
                "execution_ready": False, "node_names": sorted(names), "source_sha256": source_hashes,
                "sha256": {name: gate.digest(bundle / name) for name in ("plan.yml", "review.txt")}}
    with (bundle / "prepared.yaml").open("x") as stream:
        stream.write(yaml.safe_dump(prepared, sort_keys=False))
    (bundle / "prepared.yaml").chmod(0o600)
    return bundle


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    for name in ("arch", "site", "inspection-run-id", "wipe-run-id", "nodes"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--collection-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        adapter = load_module(ROOT / "scripts/prepare-fleet-inspection.py", "wipe_cli_adapter")
        names = adapter.selection(args.nodes)
        bundle = prepare(args.root, args.arch, args.site, args.inspection_run_id, args.wipe_run_id,
                         names, args.collection_root)
        print(f"Wipe preparation sealed: {bundle}")
        print("Review plan.yml and review.txt; no physical authority or executable wipe was created.")
    except (ValueError, KeyError, IndexError, OSError, TypeError, ImportError, yaml.YAMLError):
        parser.exit(1, "ERROR: Wipe preparation rejected; review physical eligibility, unchanged inputs, "
                    "exact membership, host/volume identity, collection capability and fresh output paths.\n")


if __name__ == "__main__":
    main()
