#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build isolated ABI media from an unchanged eligible inspection handoff."""

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
from pathlib import Path
import re
import subprocess
import uuid

import yaml

ROOT = Path(__file__).resolve().parent.parent


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = load("validate-abi-handoff")
media = load("abi-media-settings")
CONTEXT = load("_era_context")


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
            "--env", f"HOME={stage / 'installer-home'}",
            "--env", f"XDG_CACHE_HOME={stage / 'installer-cache'}",
            "--entrypoint", tool, image, *map(str, args)]


def custom_manifests(directory):
    """Read optional flat YAML inputs and describe their Kubernetes identities.

    This is structural validation, not API admission or dependency validation.
    The same reader verifies retained build snapshots after installer consumption.
    """
    state = gate.cleaning_state
    state.safe_path(directory)
    if not directory.exists():
        return {}, []
    gate.require(directory.is_dir(), "Custom manifest path must be a directory")
    contents, records, identities = {}, [], set()
    for path in sorted(directory.iterdir()):
        state.safe_path(path)
        gate.require(path.is_file() and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.(yaml|yml)", path.name),
                     "Custom manifests require regular YAML files in a flat directory")
        gate.require(0 < path.stat().st_size <= 16 * 1024 * 1024, "Empty or oversized custom manifest")
        content = path.read_bytes()
        resources = []
        for document in yaml.safe_load_all(content):
            gate.require(isinstance(document, dict), "Each custom manifest document must be an object")
            metadata = document.get("metadata")
            gate.require(isinstance(metadata, dict), "Custom manifest requires metadata")
            version, kind, name = document.get("apiVersion"), document.get("kind"), metadata.get("name")
            namespace = metadata.get("namespace", "")
            gate.require(all(isinstance(value, str) and value and not any(c.isspace() for c in value)
                             for value in (version, kind, name)), "Custom manifest requires apiVersion, kind and name")
            gate.require(isinstance(namespace, str) and not any(c.isspace() for c in namespace),
                         "Invalid custom manifest namespace")
            identity = (version.split('/')[0] if '/' in version else '', kind, namespace, name)
            gate.require(identity not in identities, "Duplicate custom Kubernetes resource identity")
            identities.add(identity)
            labels = metadata.get("labels", {})
            gate.require(isinstance(labels, dict), "Custom manifest labels must be a mapping")
            role = labels.get("machineconfiguration.openshift.io/role", "")
            gate.require(isinstance(role, str) and not any(c.isspace() for c in role),
                         "Invalid MachineConfig role")
            if kind == "MachineConfig":
                spec = document.get("spec")
                gate.require(version == "machineconfiguration.openshift.io/v1" and bool(role)
                             and not namespace and isinstance(spec, dict), "Invalid MachineConfig identity or scope")
                if "kernelArguments" in spec:
                    gate.require(isinstance(spec["kernelArguments"], list)
                                 and all(isinstance(arg, str) and arg for arg in spec["kernelArguments"]),
                                 "MachineConfig kernelArguments must be a list of nonempty strings")
            resources.append({"apiVersion": version, "kind": kind, "name": name,
                              "namespace": namespace, "role": role})
        gate.require(bool(resources), "Custom manifest contains no resources")
        contents[path.name] = content
        records.append({"filename": path.name, "source": "custom-manifests/" + path.name,
                        "sha256": hashlib.sha256(content).hexdigest(), "resources": resources})
    return contents, records


def build(root, arch, site, run_id, build_id="", ee_image="", installer="", run=subprocess.run,
          collection_root=DEFAULT_COLLECTION, coreos_image=COREOS_IMAGE, lsinitrd="/usr/bin/lsinitrd"):
    state = gate.cleaning_state
    base = state.site_root(root, arch, site)
    state.check(base)
    policy = media.content(media.site(root, arch, site))
    gate.require(policy["mode"] == "full" or build_id, "Nondefault media requires a fresh ABI_MEDIA_BUILD_ID")
    gate.require(not installer or policy["mode"] == "full", "Variant builds require the pinned installer EE")
    image, candidates, eligibility = gate.validate_handoff(root, arch, site, run_id, ee_image, installer)
    custom_dir = root / "input" / arch / site / "openshift"
    custom, custom_records = custom_manifests(custom_dir)
    stage = state.safe_path(gate.staging_path(base, run_id, build_id))
    gate.require(not stage.exists(), "ABI media destination exists; use a fresh build identity")
    stage.mkdir(parents=True, mode=0o700)
    for name, content in candidates.items():
        (stage / name).write_bytes(content)
        (stage / name).chmod(0o600)
    if custom:
        for folder in ("custom-manifests", "openshift"):
            (stage / folder).mkdir(mode=0o700)
            for name, content in custom.items():
                target = stage / folder / name
                target.write_bytes(content)
                target.chmod(0o600)
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
    if not installer:
        for name in ("installer-home", "installer-cache"):
            (stage / name).mkdir(mode=0o700)
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
            lsinitrd=lsinitrd, expected_release=provenance["release_image"], execute=run, agent_config=derived)
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
    gate.require(custom_manifests(custom_dir) == (custom, custom_records),
                 "Custom manifests changed during media construction")
    gate.require(custom_manifests(stage / "custom-manifests") == (custom, custom_records),
                 "Retained custom manifest sources changed during media construction")
    state.write(stage / "media-build.yaml", {
        "schema_version": 2 if validation else 1, "inspection_run_id": run_id, "build_id": build_id, "policy": policy,
        "installer_ee": image, "release_image": provenance["release_image"],
        "source_manifest_sha256": source_hashes, "derived_agent_sha256": derived_hash,
        "custom_manifests": custom_records,
        "kernel_arguments": kargs, "artifacts": artifacts,
        **({"validation": {"source": "media-validation/validation.json",
                            "sha256": digest(stage / "media-validation/validation.json")},
            "verifier_sha256": validation["verifier_sha256"]} if validation else {})})
    return stage


def build_and_select(root, arch, site, run_id, build_id=None, **kwargs):
    """Build and select verified media under the existing site lease."""
    state = gate.cleaning_state
    base = state.site_root(root, arch, site)
    with state.Lease(base):
        path = state.safe_path(root / '.era-context')
        previous_hash = digest(path)
        context = CONTEXT.read_context(path)
        gate.require((context['arch'], context['site'], context.get('fleet_run_id')) == (arch, site, run_id),
                     'Media construction requires the selected deployment and inspection run')
        if build_id is None:
            mode = media.site(root, arch, site)['mode']
            build_id = f"{mode}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:12]}"
        gate.require(bool(build_id), 'Explicit ABI media build identity must not be empty')
        stage = build(root, arch, site, run_id, build_id, **kwargs)
        image, candidates, _ = gate.validate_handoff(
            root, arch, site, run_id, kwargs.get('ee_image', ''), kwargs.get('installer', ''))
        release = gate.read_yaml(base / 'ocp/inspection/fleet' / run_id / 'preflight-vars.yaml')[
            'inspection_preflight_provenance']['release_image']
        install = load('prepare-abi-install')
        install.media_build(stage, run_id, build_id, image, release, candidates,
                            kwargs.get('collection_root', DEFAULT_COLLECTION))
        gate.require(digest(path) == previous_hash,
                     'Deployment context changed during media construction; build retained but not selected')
        context.update(abi_media_build_id=build_id, abi_media_run_id=run_id,
                       abi_media_receipt_sha256=digest(stage / 'media-build.yaml'))
        state.write(path, context)
    return stage


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    for name in ("arch", "site", "run-id"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--build-id", default=None)
    parser.add_argument("--ee-image", default="")
    parser.add_argument("--installer", default="")
    parser.add_argument("--collection-root", type=Path, required=True)
    parser.add_argument("--coreos-installer-image", default=COREOS_IMAGE)
    parser.add_argument("--lsinitrd", default="/usr/bin/lsinitrd")
    args = parser.parse_args()
    try:
        root = args.root.absolute()
        stage = build_and_select(root, args.arch, args.site, args.run_id, args.build_id,
                                 ee_image=args.ee_image, installer=args.installer,
                                 collection_root=args.collection_root.absolute(),
                                 coreos_image=args.coreos_installer_image, lsinitrd=args.lsinitrd)
        print(f"ABI media built: {stage}; no services or nodes changed.")
    except (ValueError, KeyError, TypeError, OSError, yaml.YAMLError, subprocess.SubprocessError):
        parser.exit(1, "ERROR: ABI media build rejected; retain private build evidence and use a fresh build identity.\n")


if __name__ == "__main__":
    main()
