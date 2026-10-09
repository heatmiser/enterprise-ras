#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""Validate a completed physical fleet handoff before staging ABI ISO inputs."""

import argparse
import hashlib
import importlib.util
from pathlib import Path
import re
import subprocess
import sys

import yaml

ROOT = Path(__file__).resolve().parent.parent
NODE_FILES = ("inventory.yaml", "inventory-raw.json", "ports-raw.json",
              "validation.yaml", "cleanup.yaml", "outcome.yaml")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_yaml(path):
    require(path.is_file() and not any(p.is_symlink() for p in (path, *path.parents)),
            f"Require regular evidence/input without symlink parents: {path}")
    value = yaml.safe_load(path.read_text())
    require(isinstance(value, dict), f"Require mapping: {path}")
    return value


def digest(path):
    require(path.is_file() and not any(p.is_symlink() for p in (path, *path.parents)),
            f"Require regular evidence/input without symlink parents: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_handoff(root, arch, site, run_id, ee_image="", installer=""):
    spec = importlib.util.spec_from_file_location("fleet_adapter", ROOT / "scripts/prepare-fleet-inspection.py")
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    adapter.validate_run(run_id)
    require(all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", v) and v not in (".", "..")
                for v in (arch, site)), "Invalid architecture/site")
    base = root / "output" / arch / site
    bundle = base / "ocp/inspection/fleet" / run_id
    evidence = base / "reports/inspection" / run_id
    document = read_yaml(bundle / "plan.yml")
    plan = document["inspect_fleet_plan"]
    prepared = read_yaml(bundle / "prepared.yaml")
    require(plan["run_id"] == prepared["run_id"] == run_id
            and plan["artifact_root"] == str(evidence), "Prepared run/evidence identity mismatch")
    require(prepared["sha256"] == adapter.handoff_files(bundle, document), "Prepared inputs changed")
    nodes = {n["name"]: n for n in plan["authorized_nodes"]}
    require(len(nodes) == len(plan["authorized_nodes"]) == plan["expected_node_count"], "Invalid node membership")
    binding = plan.get("abi_handoff", {})
    require(binding.get("schema_version") == 1 and sorted(binding.get("initial_node_names", []))
            == sorted(nodes) == sorted(prepared["node_names"]), "ABI binding missing or initial node membership mismatch")
    receipt = read_yaml(evidence / "eligibility.yaml")
    require(receipt.get("schema_version") == 1 and receipt.get("run_id") == run_id
            and sorted(receipt.get("node_names", [])) == sorted(nodes)
            and receipt.get("manifest_sha256") == binding["manifest_sha256"], "Eligibility receipt identity mismatch")
    expected_files = {"plan.yaml", "summary.yaml", "index.yaml", "runtime.yaml"}
    expected_files.update(f"{name}/{filename}" for name in nodes for filename in NODE_FILES)
    require(set(receipt.get("evidence_sha256", {})) == expected_files, "Incomplete eligibility evidence coverage")
    for filename, expected in receipt["evidence_sha256"].items():
        require(digest(evidence / filename) == expected, f"Inspection evidence changed: {filename}")
    public = read_yaml(evidence / "plan.yaml")
    require(public["run_id"] == run_id and public["wiremap"] == plan["wiremap"], "Inspection plan differs from prepared plan")
    public_nodes = {n["name"]: n for n in public["authorized_nodes"]}
    require(set(public_nodes) == set(nodes), "Inspection plan node mismatch")
    for name, node in nodes.items():
        require(all(public_nodes[name].get(k) == node.get(k) for k in
                    ("role", "bmc", "interfaces", "rootDeviceHints", "storagePolicy"))
                and public_nodes[name]["devices"]["bond_ip"] == node["devices"]["bond_ip"],
                f"Inspection policy differs from prepared node: {name}")
    summary = read_yaml(evidence / "summary.yaml")
    index = read_yaml(evidence / "index.yaml")
    runtime = read_yaml(evidence / "runtime.yaml")
    require(summary.get("run_id") == index.get("run_id") == runtime.get("run_id") == run_id,
            "Result run identity mismatch")
    require(summary.get("inspection_mode") == index.get("inspection_mode") == "physical"
            and summary.get("eligible") is True and summary.get("validation_passed") is True
            and summary.get("eligibility_reason") == "physical_inspection_passed"
            and summary.get("validated_nodes") == summary.get("total_nodes") == len(nodes)
            and summary.get("failed_nodes") == 0 and index.get("status") == "COMPLETED",
            "Physical fleet inspection is not eligible")
    require(all(summary.get(k) is True for k in ("wiremap_passed", "physical_attachments_verified",
                "disk_match_verified", "host_policy_passed", "storage_policy_passed")), "Required inspection checks did not pass")
    require(summary.get("cleanup_status") == runtime.get("cleanup_status") == "complete"
            and runtime.get("status") == "removed" and runtime.get("runtime_retained") is False
            and runtime.get("lock_owned") is False and runtime.get("ownership_confirmed") is True
            and runtime.get("artifact_root") == str(evidence), "Shared runtime cleanup is incomplete")
    require(set(index.get("nodes", {})) == set(index.get("validation", {})) == set(nodes), "Result node membership mismatch")
    for name, node in nodes.items():
        outcome = index["nodes"][name]
        cleanup = read_yaml(evidence / name / "cleanup.yaml")
        validation = read_yaml(evidence / name / "validation.yaml")
        inventory = read_yaml(evidence / name / "inventory.yaml")
        require(outcome == read_yaml(evidence / name / "outcome.yaml")
                and outcome.get("status") == "COMPLETED" and outcome.get("run_id") == run_id
                and outcome.get("node") == name and outcome.get("inspection_mode") == "physical",
                f"Incomplete physical outcome: {name}")
        require(cleanup == outcome.get("cleanup") and cleanup.get("run_id") == run_id
                and cleanup.get("node") == name and cleanup.get("power_state") == "Off"
                and cleanup.get("cleanup_status") == "complete"
                and all(cleanup.get(k) is True for k in ("cleanup_verified", "media_detached", "evidence_persisted")),
                f"Terminal node cleanup incomplete: {name}")
        require(validation.get("node") == name and validation.get("inspection_mode") == "physical"
                and validation.get("validation") == index["validation"][name]
                and validation["validation"].get("validation_passed") is True
                and validation.get("disk_validation", {}).get("run_id") == run_id
                and validation["disk_validation"].get("rootDeviceHints") == node["rootDeviceHints"],
                f"Node validation or disk identity mismatch: {name}")
        require(inventory.get("schema_version") == 3 and inventory.get("inspection_mode") == "physical"
                and inventory.get("session", {}).get("run_id") == run_id
                and inventory["session"].get("candidate_names") == [name], f"Wrong physical inventory: {name}")
    candidates = {}
    require({"agent-config.yaml", "install-config.yaml"} <= set(binding["manifest_sha256"])
            <= {"agent-config.yaml", "install-config.yaml", "endpoint-map.yaml"}, "Invalid candidate binding")
    for filename, expected in binding["manifest_sha256"].items():
        path = base / "ocp" / filename
        require(digest(path) == expected, f"ABI candidate changed: {filename}")
        candidates[filename] = path.read_bytes()
        require(hashlib.sha256(candidates[filename]).hexdigest() == expected, "Candidate changed while reading")
    agent = read_yaml(base / "ocp/agent-config.yaml")
    mapping = binding["hostname_to_physical"]
    hosts = agent.get("hosts", [])
    require(len(hosts) == len(nodes) and {h["hostname"] for h in hosts} == set(mapping)
            and sorted(mapping.values()) == sorted(nodes), "Manifest hostname membership mismatch")
    for host in hosts:
        node = nodes[mapping[host["hostname"]]]
        require(host["role"] == node["role"] and host["rootDeviceHints"] == node["rootDeviceHints"],
                "Manifest role or disk differs from inspected policy")
        macs = {i["name"]: i["macAddress"].lower() for i in host["interfaces"]}
        require(all(macs.get(i["name"]) == i["macAddress"].lower() for i in node["interfaces"]),
                "Manifest CPU MAC differs from inspected policy")
    provenance = read_yaml(bundle / "preflight-vars.yaml")["inspection_preflight_provenance"]
    image = provenance.get("ee_image", "")
    require(bool(re.fullmatch(r".+@sha256:[a-f0-9]{64}", image)), "Missing immutable prepared installer image")
    if ee_image:
        cluster = adapter.read_yaml(Path(document["fleet_adapter_collection_vars"]))
        require(ee_image in (image, cluster["ocp_ee_image"]), "Installer image differs from prepared release")
    if installer:
        result = subprocess.run([installer, "version"], capture_output=True, text=True, check=True)
        require(f"openshift-install {provenance['installer_version']}\n" in result.stdout
                and f"release image {provenance['release_image']}\n" in result.stdout,
                "Local installer differs from prepared version/release")
    return image, candidates, receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    for key in ("arch", "site", "run-id", "stage-dir"):
        parser.add_argument(f"--{key}", required=True)
    parser.add_argument("--ee-image", default="")
    parser.add_argument("--installer", default="")
    args = parser.parse_args()
    try:
        root = args.root.absolute()
        image, candidates, receipt = validate_handoff(root, args.arch, args.site, args.run_id,
                                                       args.ee_image, args.installer)
        stage = Path(args.stage_dir).absolute()
        require(stage == root / "output" / args.arch / args.site / "ocp-iso" / args.run_id,
                "Require site/run-specific ISO staging path")
        require(not any(p.is_symlink() for p in (stage, *stage.parents)), "Symlink ISO staging path")
        stage.mkdir(parents=True, exist_ok=False, mode=0o700)
        for filename, content in candidates.items():
            (stage / filename).write_bytes(content)
            (stage / filename).chmod(0o600)
        (stage / "eligibility.yaml").write_text(yaml.safe_dump(receipt))
        print(image)
    except (ValueError, KeyError, IndexError, OSError, TypeError, yaml.YAMLError, subprocess.SubprocessError):
        # Never echo parsed candidate data: install-config contains a pull secret.
        parser.exit(1, "ERROR: ABI handoff rejected; inspect the run evidence, unchanged prepared inputs, membership, and installer provenance.\n")


if __name__ == "__main__":
    main()
