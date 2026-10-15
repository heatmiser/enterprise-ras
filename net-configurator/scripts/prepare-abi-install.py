#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Prepare and serve an existing ABI ISO; never enroll, boot or clean nodes."""

import argparse
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile

import yaml

ROOT = Path(__file__).resolve().parent.parent


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = load(ROOT / "scripts/validate-abi-handoff.py", "install_gate")
state = gate.cleaning_state
require = gate.require


def digest(path):
    state.safe_path(path)
    require(path.is_file(), f"Missing regular file: {path}")
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def collection_hashes(collection):
    paths = [collection / "playbooks" / name for name in ("serve_abi_iso.yml", "_abi_iso_runtime.yml")]
    for role in ("ironic_deploy", "ironic_cleanup", "ironic_image_resolve"):
        directory = collection / "roles" / role
        require(directory.is_dir(), f"Missing collection role: {role}")
        paths.extend(sorted(directory.rglob("*.yml")))
    paths.append(collection / "plugins/filter/runtime_diagnostics.py")
    return {str(path.relative_to(collection)): digest(path) for path in paths}


def network(nodes):
    """Read local routes and resolve reviewed BMC names; no BMC connection."""
    selected, routes, sources = [], [], set()
    for node in nodes:
        callback = str(ipaddress.IPv4Address(node["devices"]["bond_ip"]))
        result = subprocess.run(["ip", "-j", "route", "get", callback], check=True,
                                capture_output=True, text=True)
        route = json.loads(result.stdout)[0]
        address = str(ipaddress.IPv4Address(route.get("prefsrc", route.get("src"))))
        interface = route["dev"]
        require(bool(re.fullmatch(r"[A-Za-z0-9_.:-]+", interface)), "Invalid route interface")
        routes.append({"node": node["name"], "source": address, "interface": interface, "target": callback})
        host = node["bmc"]["host"]
        bmc_ip = str(ipaddress.IPv4Address(node["devices"]["bmc_ip"]))
        observed = {item[4][0] for item in socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)}
        require(observed == {bmc_ip}, f"Reviewed BMC address changed: {node['name']}")
        sources.add(bmc_ip)
        selected.append({"name": node["name"], "role": node["role"], "callback_ip": callback,
                         "bmc_host": host, "bmc_ip": bmc_ip,
                         "system_id": node["bmc"].get("system_id", "")})
    require(len({(route["source"], route["interface"]) for route in routes}) == 1,
            "Selected nodes require one shared bastion source and interface")
    return selected, routes, sorted(sources)


def inputs(root, arch, site, run_id):
    state.site_root(root, arch, site)
    image, _, receipt = gate.validate_handoff(root, arch, site, run_id)
    base = root / "output" / arch / site
    original = base / "ocp/inspection/fleet" / run_id
    inspection = gate.read_yaml(original / "plan.yml")["inspect_fleet_plan"]
    provenance = gate.read_yaml(original / "preflight-vars.yaml")["inspection_preflight_provenance"]
    stage = base / "ocp-iso" / run_id
    require(gate.read_yaml(stage / "eligibility.yaml") == receipt, "ISO staging eligibility differs from inspection")
    iso = stage / "agent.x86_64.iso"
    source_hashes = {name: digest(stage / name) for name in (
        "agent.x86_64.iso", "eligibility.yaml", "endpoint-map.yaml", "rendezvousIP",
        ".openshift_install_state.json", "auth/kubeconfig", "auth/kubeadmin-password")}
    require(source_hashes["endpoint-map.yaml"] == receipt["manifest_sha256"]["endpoint-map.yaml"],
            "Staged endpoint map changed")
    rendezvous = str(ipaddress.IPv4Address((stage / "rendezvousIP").read_text().strip()))
    require(rendezvous in {node["devices"]["bond_ip"] for node in inspection["authorized_nodes"]
                          if node["role"] == "master"}, "Rendezvous is not an inspected control-plane node")
    release = provenance["release_image"]
    require(bool(re.fullmatch(r"[^\s]+@sha256:[a-f0-9]{64}", release)), "Release must be immutable")
    return base, iso, source_hashes, image, release, inspection["authorized_nodes"], rendezvous


def prepare(root, arch, site, run_id, collection):
    base, iso, hashes, ee, release, nodes, rendezvous = inputs(root, arch, site, run_id)
    state.check(base)
    selected, routes, bmc_sources = network(nodes)
    reader = load(ROOT / "scripts/init-ocp-settings.py", "install_settings")
    workbook_oem = reader.read_ocp_settings(root / "input" / arch / site / f"{arch}.xlsx").get("ocp_oem", "")
    oem = gate.read_yaml(root / "input" / arch / site / "ocp-settings.yml").get("oem", "")
    require(isinstance(oem, str) and oem.lower() in ("dell", "redfish")
            and workbook_oem.lower() == oem.lower(), "Workbook/settings must agree on Dell or native Redfish OEM")
    bundle = state.safe_path(base / "ocp/install/fleet" / run_id)
    require(not bundle.exists(), "Installation preparation exists; use verify/serve instead of overwriting it")
    plan = {"schema_version": 1, "inspection_run_id": run_id, "operation": "serve_existing_abi_iso",
            "iso_path": str(iso), "iso_sha256": hashes["agent.x86_64.iso"], "iso_size": iso.stat().st_size,
            "source_sha256": hashes, "installer_ee": ee, "release_image": release,
            "rendezvous_ip": rendezvous, "node_names": sorted(node["name"] for node in nodes),
            "nodes": selected, "routes": routes, "oem": oem.lower(),
            "publisher_driver": "idrac" if oem.lower() == "dell" else "redfish",
            "provisioning_ip": routes[0]["source"], "provisioning_interface": routes[0]["interface"],
            "http_port": 6180, "api_port": 6385, "firewall_zone": "public",
            "firewall_rules": [{"source": source, "port": 6180} for source in bmc_sources],
            "bundle": str(bundle), "media_dir": str(bundle / "media"),
            "reports_dir": str(base / "reports/installation" / run_id / "serving"),
            "collection_root": str(collection), "collection_sha256": collection_hashes(collection),
            "physical_authority": False}
    require(hashes == inputs(root, arch, site, run_id)[2], "ISO inputs changed during preparation")
    bundle.mkdir(parents=True, mode=0o700)
    state.write(bundle / "plan.yml", {"abi_install_plan": plan})
    state.write(bundle / "prepared.yaml", {"plan_sha256": state.fingerprint(plan)})
    review = [f"ABI serving preparation: {run_id}", f"ISO: {iso}", f"SHA256: {plan['iso_sha256']}",
              f"Rendezvous: {rendezvous}", "Selected initial nodes: " + ", ".join(plan["node_names"]),
              f"Release: {release}", f"Installer EE: {ee}",
              "Scope: start Ironic/HTTPD and publish the existing ISO; NO node enrollment, boot or cleaning.",
              "Only the ISO enters the serving directory; original installer state/auth remain private."]
    (bundle / "review.txt").write_text("\n".join(review) + "\n")
    (bundle / "review.txt").chmod(0o600)
    return bundle


def verify(root, arch, site, run_id, collection):
    base, _, hashes, ee, release, nodes, rendezvous = inputs(root, arch, site, run_id)
    state.check(base)
    bundle = base / "ocp/install/fleet" / run_id
    plan = gate.read_yaml(bundle / "plan.yml")["abi_install_plan"]
    require(gate.read_yaml(bundle / "prepared.yaml")["plan_sha256"] == state.fingerprint(plan), "Prepared plan changed")
    require(plan["source_sha256"] == hashes and plan["installer_ee"] == ee
            and plan["release_image"] == release and plan["rendezvous_ip"] == rendezvous,
            "ISO or prepared provenance changed")
    require(plan["node_names"] == sorted(node["name"] for node in nodes), "Initial membership changed")
    require(plan["collection_root"] == str(collection) and plan["collection_sha256"] == collection_hashes(collection),
            "Collection serving implementation changed; review a fresh preparation")
    selected, routes, sources = network(nodes)
    require(plan["nodes"] == selected and plan["routes"] == routes
            and plan["firewall_rules"] == [{"source": source, "port": 6180} for source in sources],
            "Routes or BMC identities changed")
    return bundle, plan


def serve(root, arch, site, run_id, collection, lease):
    bundle, plan = verify(root, arch, site, run_id, collection)
    reports = state.safe_path(Path(plan["reports_dir"]))
    reports.mkdir(parents=True, exist_ok=True, mode=0o700)
    attempt = Path(tempfile.mkdtemp(prefix="attempt-", dir=reports))
    state.write(attempt / "plan.yml", {"abi_install_plan": plan})
    command = ["ansible-playbook", str(ROOT / "playbooks/serve-ocp-iso.yml"), "-i", "localhost,",
               "-e", "@" + str(bundle / "plan.yml"), "-e", "abi_install_attempt=" + str(attempt)]
    vault = root / ".era-secrets/vault-pass"
    command += ["--vault-password-file", str(vault)] if vault.is_file() else ["--ask-vault-pass"]
    print(f"Serving evidence: {attempt}", flush=True)
    result = subprocess.run(command, cwd=root, pass_fds=(lease.fd,))
    require(result.returncode == 0, f"Serving failed; review {attempt}/runtime.yaml")
    return attempt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "verify", "serve"))
    parser.add_argument("--root", type=Path, default=ROOT)
    for name in ("arch", "site", "run-id"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--collection-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        root, collection = args.root.absolute(), args.collection_root.absolute()
        with state.Lease(state.site_root(root, args.arch, args.site)) as lease:
            if args.action == "prepare":
                bundle = prepare(root, args.arch, args.site, args.run_id, collection)
                print(f"Prepared: {bundle}; review review.txt. No services or hardware touched.")
            elif args.action == "verify":
                verify(root, args.arch, args.site, args.run_id, collection)
                print("ABI serving preparation verified.")
            else:
                serve(root, args.arch, args.site, args.run_id, collection, lease)
    except (ValueError, KeyError, TypeError, IndexError, OSError, yaml.YAMLError, subprocess.SubprocessError) as error:
        # Errors describe paths/checks, never credential-bearing candidate data.
        reason = str(error) if isinstance(error, ValueError) else type(error).__name__
        parser.exit(1, f"ERROR: ABI serving rejected: {reason}. Review preparation and retained runtime evidence.\n")


if __name__ == "__main__":
    main()
