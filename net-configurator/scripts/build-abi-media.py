#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build isolated ABI media from an unchanged eligible inspection handoff."""

import argparse
import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess

import yaml

ROOT = Path(__file__).resolve().parent.parent


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = load("validate-abi-handoff")
media = load("abi-media-settings")


def digest(path):
    gate.cleaning_state.safe_path(path)
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


DEFAULT_COLLECTION = ROOT.parent.parent / "rhvp.baremetal_ocp"
COREOS_IMAGE = "quay.io/coreos/coreos-installer@sha256:db8c1dc7aa5955613243c5cd73d2be58ae7b839873563dc44e0bf0a15e7fed71"


def verifier(collection):
    path = gate.cleaning_state.safe_path(Path(collection) / "plugins/module_utils/abi_media.py")
    spec = importlib.util.spec_from_file_location("abi_media_shared", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def command(image, stage, tool, *args):
    return ["podman", "run", "--rm", "--userns=keep-id", "-v", f"{stage}:{stage}:Z",
            "--entrypoint", tool, image, *map(str, args)]


def build(root, arch, site, run_id, build_id="", ee_image="", installer="", run=subprocess.run,
          collection_root=DEFAULT_COLLECTION, coreos_image=COREOS_IMAGE, lsinitrd="/usr/bin/lsinitrd"):
    state = gate.cleaning_state
    base = state.site_root(root, arch, site)
    state.check(base)
    policy = media.content(media.site(root, arch, site))
    gate.require(policy["mode"] == "full" or build_id, "Nondefault media requires a fresh ABI_MEDIA_BUILD_ID")
    gate.require(not installer or policy["mode"] == "full", "Variant builds require the pinned installer EE")
    image, candidates, eligibility = gate.validate_handoff(root, arch, site, run_id, ee_image, installer)
    stage = state.safe_path(gate.staging_path(base, run_id, build_id))
    gate.require(not stage.exists(), "ABI media destination exists; use a fresh build identity")
    stage.mkdir(parents=True, mode=0o700)
    for name, content in candidates.items():
        (stage / name).write_bytes(content)
        (stage / name).chmod(0o600)
    source_hashes = {name: hashlib.sha256(content).hexdigest() for name, content in candidates.items()}
    agent = yaml.safe_load(candidates["agent-config.yaml"])
    derived = media.derive(agent, policy)
    if derived != agent:
        (stage / "agent-config.yaml").write_text(yaml.safe_dump(derived, sort_keys=False))
    (stage / "media-agent-config.yaml").write_bytes((stage / "agent-config.yaml").read_bytes())
    (stage / "media-agent-config.yaml").chmod(0o600)
    derived_hash = digest(stage / "media-agent-config.yaml")
    state.write(stage / "eligibility.yaml", eligibility)
    provenance = gate.read_yaml(base / "ocp/inspection/fleet" / run_id / "preflight-vars.yaml")["inspection_preflight_provenance"]
    create = ([installer, "--dir", str(stage), "agent", "create", "image"] if installer
              else command(image, stage, "openshift-install", "--dir", stage, "agent", "create", "image"))
    run(create, check=True, capture_output=True, timeout=1800)
    iso = stage / "agent.x86_64.iso"
    gate.require(iso.is_file(), "Installer did not produce the expected ABI ISO")
    kargs, validation = "", None
    if policy["mode"] in ("full-ram", "minimal"):
        shared = verifier(collection_root)
        validation = shared.verify_media(stage, policy["mode"], image,
            base_url=policy["boot_artifacts_base_url"], coreos_image=coreos_image,
            lsinitrd=lsinitrd, expected_release=provenance["release_image"], execute=run)
        kargs = validation["kernel_arguments"]
    artifacts = [{"name": iso.name, "source": iso.name, "sha256": digest(iso), "size": iso.stat().st_size}]
    if policy["mode"] == "minimal":
        rootfs = stage / "boot-artifacts/agent.x86_64-rootfs.img"
        gate.require(rootfs.is_file(), "Installer did not retain the matching minimal rootfs")
        artifacts.append({"name": rootfs.name, "source": str(rootfs.relative_to(stage)),
                          "sha256": digest(rootfs), "size": rootfs.stat().st_size})
    gate.require(all(item["size"] > 0 for item in artifacts), "Empty generated media")
    gate.require(gate.validate_handoff(root, arch, site, run_id, ee_image, installer)[1] == candidates,
                 "Inspected inputs changed during media construction")
    state.write(stage / "media-build.yaml", {
        "schema_version": 2 if validation else 1, "inspection_run_id": run_id, "build_id": build_id, "policy": policy,
        "installer_ee": image, "release_image": provenance["release_image"],
        "source_manifest_sha256": source_hashes, "derived_agent_sha256": derived_hash,
        "kernel_arguments": kargs, "artifacts": artifacts,
        **({"validation": {"source": "media-validation/validation.json",
                            "sha256": digest(stage / "media-validation/validation.json")},
            "verifier_sha256": validation["verifier_sha256"]} if validation else {})})
    return stage


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    for name in ("arch", "site", "run-id"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--build-id", default=os.environ.get("ABI_MEDIA_BUILD_ID", ""))
    parser.add_argument("--ee-image", default="")
    parser.add_argument("--installer", default="")
    parser.add_argument("--collection-root", type=Path, required=True)
    parser.add_argument("--coreos-installer-image", default=COREOS_IMAGE)
    parser.add_argument("--lsinitrd", default="/usr/bin/lsinitrd")
    args = parser.parse_args()
    try:
        root = args.root.absolute()
        with gate.cleaning_state.Lease(gate.cleaning_state.site_root(root, args.arch, args.site)):
            stage = build(root, args.arch, args.site, args.run_id, args.build_id, args.ee_image, args.installer,
                          collection_root=args.collection_root.absolute(),
                          coreos_image=args.coreos_installer_image, lsinitrd=args.lsinitrd)
        print(f"ABI media built: {stage}; no services or nodes changed.")
    except (ValueError, KeyError, TypeError, OSError, yaml.YAMLError, subprocess.SubprocessError):
        parser.exit(1, "ERROR: ABI media build rejected; retain private build evidence and use a fresh build identity.\n")


if __name__ == "__main__":
    main()
