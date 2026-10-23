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
STATE_SPEC = importlib.util.spec_from_file_location("abi_cleaning_state", ROOT / "scripts/fleet-cleaning-state.py")
cleaning_state = importlib.util.module_from_spec(STATE_SPEC)
STATE_SPEC.loader.exec_module(cleaning_state)
PHYSICAL_SPEC = importlib.util.spec_from_file_location(
    "abi_physical_inspection", ROOT / "scripts/validate-physical-inspection.py")
physical = importlib.util.module_from_spec(PHYSICAL_SPEC)
PHYSICAL_SPEC.loader.exec_module(physical)
NODE_FILES = physical.NODE_FILES
require = physical.require
read_yaml = physical.read_yaml
digest = physical.digest


def validate_handoff(root, arch, site, run_id, ee_image="", installer=""):
    document, receipt, _ = physical.validate_handoff(root, arch, site, run_id, "eligibility.yaml")
    spec = importlib.util.spec_from_file_location("abi_fleet_adapter", ROOT / "scripts/prepare-fleet-inspection.py")
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    base = root / "output" / arch / site
    bundle = base / "ocp/inspection/fleet" / run_id
    plan = document["inspect_fleet_plan"]
    nodes = {n["name"]: n for n in plan["authorized_nodes"]}
    binding = plan.get("abi_handoff", {})
    require(binding.get("schema_version") == 1 and sorted(binding.get("initial_node_names", [])) == sorted(nodes)
            and receipt.get("manifest_sha256") == binding.get("manifest_sha256"),
            "ABI binding missing or initial node membership mismatch")
    summary = read_yaml(base / "reports/inspection" / run_id / "summary.yaml")
    require(summary.get("eligible") is True and summary.get("eligibility_reason") == "physical_inspection_passed",
            "Physical fleet inspection is not eligible")
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


def staging_path(base, run_id, build_id=""):
    spec = importlib.util.spec_from_file_location("abi_handoff_media", ROOT / "scripts/abi-media-settings.py")
    media = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(media)
    return media.build_path(base, run_id, build_id)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    for key in ("arch", "site", "run-id", "stage-dir"):
        parser.add_argument(f"--{key}", required=True)
    parser.add_argument("--build-id", default="")
    parser.add_argument("--ee-image", default="")
    parser.add_argument("--installer", default="")
    args = parser.parse_args()
    try:
        root = args.root.absolute()
        base = cleaning_state.site_root(root, args.arch, args.site)
        with cleaning_state.Lease(base):
            cleaning_state.check(base)
            image, candidates, receipt = validate_handoff(root, args.arch, args.site, args.run_id,
                                                           args.ee_image, args.installer)
            stage = Path(args.stage_dir).absolute()
            require(stage == staging_path(base, args.run_id, args.build_id), "Require site/run-specific ISO staging path")
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
