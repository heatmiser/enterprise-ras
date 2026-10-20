#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""Validate sealed successful physical inspection independently of ABI membership."""

import hashlib
import importlib.util
from pathlib import Path
import re

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


def validate_handoff(root, arch, site, run_id, receipt_name=None):
    """Require complete physical evidence; never infer readiness from ABI eligible:false.

    Legacy ABI receipts remain usable only with their original binding and success
    flags. A present physical receipt is authoritative; invalid data never falls
    back to the legacy receipt. Source inspection artifacts are never rewritten.
    """
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
    require(nodes and sorted(prepared["node_names"]) == sorted(nodes), "Prepared node membership mismatch")
    if receipt_name is None:
        physical = evidence / "physical-readiness.yaml"
        receipt_name = "physical-readiness.yaml" if physical.exists() or physical.is_symlink() else "eligibility.yaml"
    require(receipt_name in ("physical-readiness.yaml", "eligibility.yaml"), "Invalid inspection receipt")
    receipt = read_yaml(evidence / receipt_name)
    require(receipt.get("schema_version") == 1 and receipt.get("run_id") == run_id
            and sorted(receipt.get("node_names", [])) == sorted(nodes), "Inspection receipt identity mismatch")
    if receipt_name == "physical-readiness.yaml":
        require(receipt.get("operation") == "physical_node_inspection", "Wrong physical readiness operation")
    else:
        binding = plan.get("abi_handoff", {})
        require(binding.get("schema_version") == 1
                and sorted(binding.get("initial_node_names", [])) == sorted(nodes)
                and receipt.get("manifest_sha256") == binding.get("manifest_sha256"),
                "Legacy ABI receipt binding mismatch")
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
            and summary.get("validation_passed") is True
            and summary.get("validated_nodes") == summary.get("total_nodes") == len(nodes)
            and summary.get("failed_nodes") == 0 and index.get("status") == "COMPLETED",
            "Physical node inspection did not pass")
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
    if receipt_name == "eligibility.yaml":
        require(summary.get("eligible") is True
                and summary.get("eligibility_reason") == "physical_inspection_passed",
                "Legacy ABI inspection is not eligible")
    provenance = read_yaml(bundle / "preflight-vars.yaml")["inspection_preflight_provenance"]
    require(bool(re.fullmatch(r".+@sha256:[a-f0-9]{64}", provenance.get("ee_image", ""))),
            "Missing immutable prepared installer image")
    return document, receipt, receipt_name
