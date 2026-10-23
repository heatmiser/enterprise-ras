#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Prepare existing ABI media and its separately authorized boot handoff."""

import argparse
from datetime import datetime, timezone
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
import uuid

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
MEDIA = load(ROOT / "scripts/abi-media-settings.py", "install_media")
HTTPD = load(ROOT / "scripts/ironic-httpd-settings.py", "install_httpd")


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
    paths.append(collection / "roles/ironic_deploy/templates/ironic-tuning.conf.j2")
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


def inputs(root, arch, site, run_id, build_id="", collection=None):
    state.site_root(root, arch, site)
    image, candidates, receipt = gate.validate_handoff(root, arch, site, run_id)
    base = root / "output" / arch / site
    original = base / "ocp/inspection/fleet" / run_id
    inspection = gate.read_yaml(original / "plan.yml")["inspect_fleet_plan"]
    provenance = gate.read_yaml(original / "preflight-vars.yaml")["inspection_preflight_provenance"]
    stage = MEDIA.build_path(base, run_id, build_id)
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
    if build_id or (stage / "media-build.yaml").exists():
        build = media_build(stage, run_id, build_id, image, release, candidates, collection)
        source_hashes["media-build.yaml"] = digest(stage / "media-build.yaml")
        source_hashes["media-agent-config.yaml"] = digest(stage / "media-agent-config.yaml")
        if build.get("schema_version") == 2:
            validation = json.loads((stage / build["validation"]["source"]).read_text())
            source_hashes[build["validation"]["source"]] = build["validation"]["sha256"]
            for evidence in validation["evidence"]:
                source_hashes[evidence["path"]] = evidence["sha256"]
        for artifact in build["artifacts"]:
            source_hashes[artifact["source"]] = artifact["sha256"]
    return base, iso, source_hashes, image, release, inspection["authorized_nodes"], rendezvous


def media_build(stage, run_id, build_id, image, release, candidates, collection=None):
    build = state.read(stage / "media-build.yaml")
    require(build.get("schema_version") in (1, 2) and build["inspection_run_id"] == run_id
            and build["build_id"] == build_id and build["installer_ee"] == image
            and build["release_image"] == release, "Media build identity/provenance changed")
    policy = MEDIA.normalize(build["policy"])
    require(build["source_manifest_sha256"] == {
        name: hashlib.sha256(content).hexdigest() for name, content in candidates.items()},
        "Media source manifest binding changed")
    require(digest(stage / "media-agent-config.yaml") == build["derived_agent_sha256"]
            and state.read(stage / "media-agent-config.yaml") == MEDIA.derive(
                yaml.safe_load(candidates["agent-config.yaml"]), policy), "Derived media configuration changed")
    expected = [("agent.x86_64.iso", "agent.x86_64.iso")]
    if policy["mode"] == "minimal":
        expected.append(("agent.x86_64-rootfs.img", "boot-artifacts/agent.x86_64-rootfs.img"))
    require([(item["name"], item["source"]) for item in build["artifacts"]] == expected,
            "Media artifact membership differs from selected mode")
    for artifact in build["artifacts"]:
        path = stage / artifact["source"]
        require(type(artifact["size"]) is int and artifact["size"] > 0
                and digest(path) == artifact["sha256"] and path.stat().st_size == artifact["size"],
                "Media artifact identity changed")
    if policy["mode"] in ("full-ram", "minimal"):
        require(build.get("schema_version") == 2, "Obsolete variant validation; generate a fresh build")
        builder = load(ROOT / "scripts/build-abi-media.py", "install_media_builder")
        shared = builder.verifier(collection if collection is not None else builder.DEFAULT_COLLECTION)
        require(build.get("verifier_sha256") == shared.verifier_digest(), "Media verifier identity changed")
        reference = build["validation"]
        require(reference["source"] == "media-validation/validation.json", "Validation evidence reference changed")
        path = stage / reference["source"]
        require(digest(path) == reference["sha256"], "Validation receipt changed")
        record = json.loads(path.read_text())
        shared.validate_record(stage, record, policy["mode"], image, release,
                               build["kernel_arguments"], policy["boot_artifacts_base_url"])
    return build


def media_contract(iso, build_id=""):
    if not (iso.parent / "media-build.yaml").exists():
        require(not build_id, "Missing variant build receipt")
        return {"mode": "full", "build_id": "", "boot_artifacts_base_url": "", "artifacts": [
            {"name": iso.name, "source": iso.name, "sha256": digest(iso), "size": iso.stat().st_size}]}
    build = state.read(iso.parent / "media-build.yaml")
    return {**build["policy"], "build_id": build_id, "artifacts": build["artifacts"]}


def firewall_sources(contract, bmc_sources, nodes):
    sources = set(bmc_sources)
    if contract["mode"] == "minimal":
        sources.update(node["callback_ip"] for node in nodes)
    return [{"source": source, "port": 6180} for source in sorted(sources)]


def installation_paths(base, run_id, attempt_id=""):
    require(isinstance(run_id, str) and bool(re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", run_id)), "Invalid inspection run identity")
    bundle = state.safe_path(base / "ocp/install/fleet" / run_id)
    reports = state.safe_path(base / "reports/installation" / run_id)
    if attempt_id:
        require(isinstance(attempt_id, str) and bool(re.fullmatch(r"[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}", attempt_id)),
                "Invalid installation attempt identity")
        bundle = state.safe_path(bundle / "attempts" / attempt_id)
        reports = state.safe_path(reports / "attempts" / attempt_id)
    return bundle, reports


def selected_installation(base, run_id):
    original, _ = installation_paths(base, run_id)
    marker = state.safe_path(original / "current-attempt.yaml")
    if not marker.exists():
        return original, ""
    current = state.read(marker)
    require(current.get("schema_version") == 1 and current.get("inspection_run_id") == run_id,
            "Current installation reference changed")
    attempt_id = current["installation_attempt_id"]
    require(isinstance(attempt_id, str) and bool(attempt_id), "Current installation attempt identity is empty")
    bundle, _ = installation_paths(base, run_id, attempt_id)
    require(current["plan_sha256"] == state.read(bundle / "prepared.yaml")["plan_sha256"],
            "Current installation reference differs from prepared plan")
    return bundle, attempt_id


def require_previous_cleanup(base, run_id, lock):
    require(not state.safe_path(lock).exists(), "Serving runtime is retained; resolve it before preparing another attempt")
    _, reports = installation_paths(base, run_id)
    completed = set()
    retirement = load(ROOT / "scripts/prepare-abi-retirement.py", "install_retirement")
    histories = [(run_id, reports)]
    histories.extend((path.name, path) for path in reports.glob("attempts/*") if path.is_dir())
    retired = set()
    for attempt_id, _ in histories:
        receipt = retirement.completed(base, run_id, attempt_id)
        if receipt is not None:
            retired.add(receipt["serving_attempt"])
    completed.update(retired)
    # A successful boot cleanup consumes its serving owner without rewriting the
    # original SERVING receipt. Match that exact owner instead of treating every
    # historical SERVING receipt as a still-active service.
    for boot_dir in list(reports.glob("boot")) + list(reports.glob("attempts/*/boot")):
        previous = state.read(boot_dir / "runtime.yaml")
        require(previous.get("serving_attempt") in retired or
                (previous.get("runtime_retained") is False and previous.get("cleanup_status") == "complete"),
                "Previous boot runtime cleanup is unresolved")
        owner = state.safe_path(Path(previous["serving_attempt"]))
        require(owner.parent == boot_dir.parent / "serving" and owner.name.startswith("attempt-"),
                "Previous boot cleanup owner differs from its serving reports")
        completed.add(str(owner))
    for attempt in list(reports.glob("serving/attempt-*")) + list(reports.glob("attempts/*/serving/attempt-*")):
        previous = state.read(attempt / "runtime.yaml")
        require(str(attempt) in completed or (previous.get("runtime_retained") is False
                and previous.get("cleanup_status") in ("complete", "not_needed")),
                "Previous serving runtime cleanup is unresolved")


def prepare_repeat(root, arch, site, run_id, collection, lock=None):
    base = state.site_root(root, arch, site)
    require_previous_cleanup(base, run_id, RUNTIME_LOCK if lock is None else lock)
    attempt_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    bundle = prepare(root, arch, site, run_id, collection, attempt_id=attempt_id)
    prepared = state.read(bundle / "prepared.yaml")
    original, _ = installation_paths(base, run_id)
    # CLI callers hold the site lease. Publish the reference only after all
    # preparation writes succeed, leaving earlier attempts available for review.
    state.write(original / "current-attempt.yaml", {
        "schema_version": 1, "inspection_run_id": run_id,
        "installation_attempt_id": attempt_id, "plan_sha256": prepared["plan_sha256"]})
    return bundle


def prepare(root, arch, site, run_id, collection, attempt_id=""):
    httpd = HTTPD.site(root, arch, site)
    build_id = os.environ.get("ABI_MEDIA_BUILD_ID", "")
    base, iso, hashes, ee, release, nodes, rendezvous = inputs(root, arch, site, run_id, build_id, collection)
    state.check(base)
    contract = media_contract(iso, build_id)
    policy = MEDIA.site(root, arch, site)
    require(MEDIA.content(policy) == {key: contract[key] for key in MEDIA.DEFAULTS},
            "Site media policy differs from selected build; generate/select matching media")
    selected, routes, bmc_sources = network(nodes)
    if contract["mode"] == "minimal":
        require(contract["boot_artifacts_base_url"] == f"http://{routes[0]['source']}:6180/redfish",
                "Minimal rootfs base URL differs from the reviewed serving endpoint")
    reader = load(ROOT / "scripts/init-ocp-settings.py", "install_settings")
    workbook_oem = reader.read_ocp_settings(root / "input" / arch / site / f"{arch}.xlsx").get("ocp_oem", "")
    oem = gate.read_yaml(root / "input" / arch / site / "ocp-settings.yml").get("oem", "")
    require(isinstance(oem, str) and oem.lower() in ("dell", "redfish")
            and workbook_oem.lower() == oem.lower(), "Workbook/settings must agree on Dell or native Redfish OEM")
    bundle, reports = installation_paths(base, run_id, attempt_id)
    require(not bundle.exists(), "Installation preparation exists; use verify/serve instead of overwriting it")
    require(not reports.exists(), "Installation reports already exist; require a fresh attempt")
    plan = {"boot_method": "manual" if policy["mode"] == "manual" else "automatic",
            "media": contract, "httpd_tuning": httpd, "schema_version": 1, "inspection_run_id": run_id, "operation": "serve_existing_abi_iso",
            "iso_path": str(iso), "iso_sha256": hashes["agent.x86_64.iso"], "iso_size": iso.stat().st_size,
            "source_sha256": hashes, "installer_ee": ee, "release_image": release,
            "rendezvous_ip": rendezvous, "node_names": sorted(node["name"] for node in nodes),
            "nodes": selected, "routes": routes, "oem": oem.lower(),
            "publisher_driver": "idrac" if oem.lower() == "dell" else "redfish",
            "provisioning_ip": routes[0]["source"], "provisioning_interface": routes[0]["interface"],
            "http_port": 6180, "api_port": 6385, "firewall_zone": "public",
            "firewall_rules": firewall_sources(contract, bmc_sources, selected),
            "bundle": str(bundle), "media_dir": str(bundle / "media"),
            "reports_dir": str(reports / "serving"),
            "collection_root": str(collection), "collection_sha256": collection_hashes(collection),
            "physical_authority": False}
    if attempt_id:
        plan["installation_attempt_id"] = attempt_id
    require(hashes == inputs(root, arch, site, run_id, build_id, collection)[2], "ISO inputs changed during preparation")
    bundle.mkdir(parents=True, mode=0o700)
    state.write(bundle / "plan.yml", {"abi_install_plan": plan})
    state.write(bundle / "prepared.yaml", {"plan_sha256": state.fingerprint(plan)})
    review = [f"ABI serving preparation: {attempt_id or run_id}", f"Inspection source: {run_id}",
              f"ISO: {iso}", f"SHA256: {plan['iso_sha256']}",
              f"Rendezvous: {rendezvous}", "Selected initial nodes: " + ", ".join(plan["node_names"]),
              f"Release: {release}", f"Installer EE: {ee}",
              "Scope: start Ironic/HTTPD and publish the existing ISO; NO node enrollment, boot or cleaning.",
              f"Media mode: {contract['mode']}; build: {build_id or 'legacy'}; boot: {plan['boot_method']}",
              "Only sealed boot artifacts enter the serving directory; installer state/auth remain private."]
    review.append(HTTPD.review(httpd))
    (bundle / "review.txt").write_text("\n".join(review) + "\n")
    (bundle / "review.txt").chmod(0o600)
    return bundle


def verify(root, arch, site, run_id, collection):
    base = state.site_root(root, arch, site)
    state.check(base)
    bundle, attempt_id = selected_installation(base, run_id)
    plan = gate.read_yaml(bundle / "plan.yml")["abi_install_plan"]
    build_id = plan.get("media", {}).get("build_id", "")
    _, iso, hashes, ee, release, nodes, rendezvous = inputs(root, arch, site, run_id, build_id, collection)
    contract = media_contract(iso, build_id)
    require(plan.get("media", contract) == contract, "Prepared media contract changed")
    require(plan.get("boot_method", "automatic") in ("automatic", "manual")
            and (plan.get("boot_method") != "manual" or contract["mode"] == "full"),
            "Invalid media boot method")
    HTTPD.normalize(plan.get("httpd_tuning", {}))
    require(gate.read_yaml(bundle / "prepared.yaml")["plan_sha256"] == state.fingerprint(plan), "Prepared plan changed")
    _, reports = installation_paths(base, run_id, attempt_id)
    require(plan["inspection_run_id"] == run_id and plan.get("installation_attempt_id", run_id) == (attempt_id or run_id)
            and plan["bundle"] == str(bundle) and plan["media_dir"] == str(bundle / "media")
            and plan["reports_dir"] == str(reports / "serving"), "Installation attempt paths or identity changed")
    require(plan["source_sha256"] == hashes and plan["installer_ee"] == ee
            and plan["release_image"] == release and plan["rendezvous_ip"] == rendezvous,
            "ISO or prepared provenance changed")
    require(plan["node_names"] == sorted(node["name"] for node in nodes), "Initial membership changed")
    require(plan["collection_root"] == str(collection) and plan["collection_sha256"] == collection_hashes(collection),
            "Collection serving implementation changed; review a fresh preparation")
    selected, routes, sources = network(nodes)
    require(plan["nodes"] == selected and plan["routes"] == routes
            and plan["firewall_rules"] == firewall_sources(contract, sources, selected),
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


# Boot preparation is separate so an already running serving implementation and
# its sealed hashes remain valid when boot orchestration is added.
RUNTIME_LOCK = Path("/tmp/rhvp-fleet-inspection-runtime.lock")
OEM_BOOT_PROFILES = {
    "dell": {"method": "dell_virtual_media_once", "media_module": "dellemc.openmanage.idrac_virtual_media",
             "boot_module": "dellemc.openmanage.idrac_boot", "power_module": "dellemc.openmanage.redfish_powerstate",
             "mode": "uefi", "target": "cd", "enabled": "once", "reset_type": "none", "power_action": "On"},
}


def boot_hashes(collection):
    paths = [collection / "playbooks/install_abi.yml"]
    paths.extend(sorted((collection / "roles/abi_install_driver").rglob("*.yml")))
    required = {"main.yml", "adopt.yml", "boot.yml", "monitor.yml", "complete.yml", "manual_handoff.yml"}
    require(required <= {path.name for path in (collection / "roles/abi_install_driver/tasks").glob("*.yml")}
            and (collection / "roles/abi_install_driver/defaults/main.yml").is_file()
            and (collection / "roles/abi_install_driver/meta/argument_specs.yml").is_file()
            and paths[0].is_file(), "Incomplete ABI installation driver packaging")
    return {str(path.relative_to(collection)): digest(path) for path in paths}


def serving_handoff(plan, lock):
    marker = state.safe_path(lock / "owner")
    require(marker.is_file(), "Missing serving runtime owner")
    attempt = state.safe_path(Path(marker.read_text().strip()))
    require(attempt.is_absolute() and attempt.parent == Path(plan["reports_dir"])
            and bool(re.fullmatch(r"attempt-[A-Za-z0-9_-]+", attempt.name)), "Unexpected serving owner")
    require(gate.read_yaml(attempt / "plan.yml")["abi_install_plan"] == plan, "Serving plan changed")
    receipt = state.read(attempt / "runtime.yaml")
    require(receipt["schema_version"] == 1 and receipt["status"] == "SERVING"
            and receipt["attempt"] == str(attempt)
            and receipt["inspection_run_id"] == plan["inspection_run_id"]
            and receipt.get("installation_attempt_id", receipt["inspection_run_id"])
                == plan.get("installation_attempt_id", plan["inspection_run_id"])
            and receipt["node_names"] == plan["node_names"]
            and receipt["iso_sha256"] == plan["iso_sha256"]
            and receipt["runtime_retained"] is True and receipt["ownership_confirmed"] is True
            and receipt["lock_owned"] is True and receipt["physical_execution"] is False,
            "Serving receipt is not eligible for adoption")
    if "media" in plan:
        require(receipt.get("media") == plan["media"], "Serving media manifest changed")
    require(receipt["url"] == f"http://{plan['provisioning_ip']}:{plan['http_port']}/redfish/agent.x86_64.iso",
            "Unexpected served ISO URL")
    require(bool(re.fullmatch(r"[^\s]+@sha256:[a-f0-9]{64}", receipt["ironic_image"])),
            "Serving image is not immutable")
    return attempt, receipt


def prepare_boot(root, arch, site, run_id, collection, lock=RUNTIME_LOCK):
    bundle, serving = verify(root, arch, site, run_id, collection)
    manual = serving.get("boot_method") == "manual"
    require(manual or serving["oem"] in OEM_BOOT_PROFILES, "ABI boot implementation unavailable for workbook OEM")
    attempt, receipt = serving_handoff(serving, lock)
    _, _, _, _, _, inspected, _ = inputs(root, arch, site, run_id, serving.get("media", {}).get("build_id", ""), collection)
    nodes = []
    for node in inspected:
        if manual:
            nodes.append({"name": node["name"]})
            continue
        bmc = node["bmc"]
        system_id = bmc.get("system_id") or ("System.Embedded.1" if serving["oem"] == "dell" else "1")
        system_id = system_id.removeprefix("/redfish/v1/Systems/").rstrip("/")
        require(bool(re.fullmatch(r"[A-Za-z0-9_.:-]+", system_id)), "Invalid BMC system ID")
        require(system_id == "System.Embedded.1", "Dell OpenManage virtual media requires System.Embedded.1")
        nodes.append({"name": node["name"], "bmc_host": bmc["host"], "system_id": system_id,
                      "verify_ca": bool(bmc.get("verify_ca", False))})
    profile = {"method": "operator_manual", "media_owner": "operator"} if manual else dict(OEM_BOOT_PROFILES[serving["oem"]])
    expected_nodes = expected_cluster_nodes(serving)
    plan = {"schema_version": 1, "operation": "boot_existing_abi_iso", "inspection_run_id": run_id,
            "serving_plan": serving, "serving_attempt": str(attempt), "serving_receipt": receipt,
            "serving_sha256": digest(attempt / "runtime.yaml"), "runtime_lock": str(lock),
            "node_names": serving["node_names"], "nodes": sorted(nodes, key=lambda node: node["name"]),
            "expected_cluster_nodes": expected_nodes,
            "profile": profile, "automated_clean": False, "physical_authority": False,
            "workflow_ready": True, "collection_sha256": boot_hashes(collection),
            "installer_dir": str(Path(serving["iso_path"]).parent),
            "artifact_root": str(Path(serving["reports_dir"]).parent / "boot")}
    if "installation_attempt_id" in serving:
        plan["installation_attempt_id"] = serving["installation_attempt_id"]
    target = state.safe_path(bundle / "boot")
    require(not target.exists(), "Boot preparation exists; verify it instead of overwriting")
    target.mkdir(mode=0o700)
    state.write(target / "plan.yml", {"abi_boot_plan": plan})
    state.write(target / "prepared.yaml", {"plan_sha256": state.fingerprint(plan)})
    identity = plan.get("installation_attempt_id", run_id)
    review = [f"ABI boot preparation: {identity}", f"Inspection source: {run_id}",
              f"Serving owner: {attempt}", f"ISO URL: {receipt['url']}",
              f"ISO SHA256: {serving['iso_sha256']}", "Nodes: " + ", ".join(plan["node_names"]),
              "Workbook OEM: " + serving["oem"], "Boot profile: " + json.dumps(profile, sort_keys=True),
              ("Operator inserts reviewed media and boots selected nodes; interactive Continue is required. "
               "Operator media is never ejected by automation." if manual else
               "Virtual media boot ONCE before power-on; persistent BIOS boot order is unchanged."),
              "No Ironic enrollment, provisioning transitions, inspection or cleaning.",
              "Boot authority: none. No hardware was contacted by preparation.",
              "Expected Ready Kubernetes nodes: " + ", ".join(expected_nodes),
              "Native installer completion AND every expected node Ready are required before cleanup.",
              "Complete private service logs are retained automatically; no additional CLI variables.",
              "Failures retain runtime/evidence; resume observation never repeats node boot.",
              f"After review: make boot-ocp-nodes FLEET_AUTHORIZE_BOOT_RUN={identity}"]
    (target / "review.txt").write_text("\n".join(review) + "\n")
    (target / "review.txt").chmod(0o600)
    return target


def expected_cluster_nodes(serving):
    """Resolve API node names from the sealed ISO's physical endpoint map."""
    mapping = gate.read_yaml(Path(serving["iso_path"]).parent / "endpoint-map.yaml")["rhcos_to_physical"]
    selected = set(serving["node_names"])
    expected = sorted(hostname for hostname, physical in mapping.items() if physical in selected)
    require(len(expected) == len(selected)
            and {mapping[hostname] for hostname in expected} == selected,
            "Endpoint map must identify every selected Kubernetes node exactly once")
    return expected


def verify_boot(root, arch, site, run_id, collection, lock=RUNTIME_LOCK):
    bundle, serving = verify(root, arch, site, run_id, collection)
    plan = state.read(bundle / "boot/plan.yml")["abi_boot_plan"]
    require(state.read(bundle / "boot/prepared.yaml")["plan_sha256"] == state.fingerprint(plan),
            "Boot plan changed")
    attempt, receipt = serving_handoff(serving, lock)
    require(plan["serving_plan"] == serving and plan["serving_attempt"] == str(attempt)
            and plan["runtime_lock"] == str(lock) and plan["serving_receipt"] == receipt
            and plan["serving_sha256"] == digest(attempt / "runtime.yaml"), "Serving evidence changed")
    require(plan["inspection_run_id"] == run_id
            and plan.get("installation_attempt_id", run_id) == serving.get("installation_attempt_id", run_id)
            and plan["installer_dir"] == str(Path(serving["iso_path"]).parent)
            and plan["artifact_root"] == str(Path(serving["reports_dir"]).parent / "boot"),
            "Boot installation attempt identity or paths changed")
    require(plan["collection_sha256"] == boot_hashes(collection), "Boot implementation changed")
    profile = ({"method": "operator_manual", "media_owner": "operator"} if serving.get("boot_method") == "manual"
               else OEM_BOOT_PROFILES.get(serving["oem"]))
    require(plan["profile"] == profile, "Workbook OEM boot profile changed")
    require(plan["node_names"] == serving["node_names"]
            and [node["name"] for node in plan["nodes"]] == plan["node_names"]
            and plan["automated_clean"] is False and plan["physical_authority"] is False,
            "Boot membership or cleaning contract changed")
    require(plan.get("expected_cluster_nodes") == expected_cluster_nodes(serving),
            "Expected Kubernetes membership changed; prepare a fresh boot review")
    return bundle / "boot", plan


def manual_binding(plan):
    return {"schema_version": 1, "operation": "operator_manual_boot_attestation",
            "installation_attempt_id": plan.get("installation_attempt_id", plan["inspection_run_id"]),
            "plan_sha256": state.fingerprint(plan), "node_names": plan["node_names"],
            "iso_sha256": plan["serving_plan"]["iso_sha256"], "iso_url": plan["serving_receipt"]["url"]}


def manual_handoff(plan, continuing=False):
    """A durable operator attestation; never a claim of BMC or OS observation."""
    artifact = state.safe_path(Path(plan["artifact_root"]))
    path = artifact / "operator-handoff.yaml"
    binding = manual_binding(plan)
    if path.exists():
        record = state.read(path)
        require({key: record.get(key) for key in binding} == binding, "Manual handoff binding changed")
        require(record.get("status") in ("WAITING_FOR_OPERATOR", "OPERATOR_CONFIRMED"), "Invalid manual handoff state")
        if record["status"] == "OPERATOR_CONFIRMED":
            require(isinstance(record.get("confirmed_at"), str), "Missing manual confirmation time")
            confirmed = datetime.fromisoformat(record["confirmed_at"])
            require(confirmed.tzinfo is not None and confirmed <= datetime.now(timezone.utc), "Invalid manual confirmation time")
            return
        require(continuing, "Manual handoff is waiting; use continue-ocp-install")
    else:
        require(not continuing, "No manual gate to continue; run boot-ocp-nodes first")
        state.write(path, {**binding, "status": "WAITING_FOR_OPERATOR"})
    print("WAITING_FOR_OPERATOR: insert the reviewed media and boot every selected node.", flush=True)
    print("ISO: " + binding["iso_url"] + "\nSHA256: " + binding["iso_sha256"], flush=True)
    print("Nodes: " + ", ".join(binding["node_names"]), flush=True)
    require(sys.stdin.isatty(), "Manual Continue requires an interactive terminal; waiting record retained")
    try:
        response = input("Press Enter to attest that media was inserted and selected nodes were booted; Ctrl-C to leave waiting: ")
    except (EOFError, KeyboardInterrupt) as error:
        raise ValueError("Manual handoff remains waiting; use continue-ocp-install") from error
    require(response == "", "Continue requires Enter only; waiting record retained")
    state.write(path, {**binding, "status": "OPERATOR_CONFIRMED",
                       "confirmed_at": datetime.now(timezone.utc).isoformat()})


def boot(root, arch, site, run_id, collection, lease, authorize_run, resume=False, continuing=False):
    bundle, plan = verify_boot(root, arch, site, run_id, collection)
    require(authorize_run == plan.get("installation_attempt_id", run_id),
            "Explicit FLEET_AUTHORIZE_BOOT_RUN must match the reviewed installation attempt")
    require(plan["workflow_ready"] is True,
            "Boot execution disabled until installer monitoring and completion cleanup are implemented")
    manual = plan["profile"]["method"] == "operator_manual"
    require(not continuing or manual, "Continue is only available for a manual boot handoff")
    secrets = state.safe_path(collection / "inventories" / site / "secrets.yaml")
    if not manual:
        require(secrets.is_file(), "Missing selected site BMC vault")
    artifact = state.safe_path(Path(plan["artifact_root"]))
    if resume or continuing:
        require(state.read(artifact / "plan.yml") == {"abi_boot_plan": plan}, "Executed boot plan changed")
    else:
        require(not artifact.exists(), "Boot already attempted; use resume-ocp-install for observation")
        artifact.mkdir(parents=True, mode=0o700)
        state.write(artifact / "plan.yml", {"abi_boot_plan": plan})
    if manual:
        if resume:
            require(state.read(artifact / "operator-handoff.yaml").get("status") == "OPERATOR_CONFIRMED",
                    "Manual handoff is waiting; use continue-ocp-install")
        manual_handoff(plan, continuing=continuing)
        # A confirmed handoff may have been interrupted before Ansible started.
        resume = (artifact / "runtime.yaml").exists()
        require(resume or not (artifact / "execution.claim").exists(), "Incomplete execution claim; retain evidence for recovery")
    if resume:
        previous = state.read(artifact / "runtime.yaml")
        require((manual or previous.get("boot_complete") is True)
                and previous.get("runtime_retained") is True and not previous.get("cleanup_started", False),
                "Resume requires completed boot and retained runtime before shared cleanup")
    command = ["ansible-playbook", str(ROOT / "playbooks/install-ocp.yml"), "-i", "localhost,",
               "-e", "@" + str(bundle / "plan.yml"),
               "-e", "abi_install_driver_authorize_run=" + authorize_run,
               "-e", "abi_install_driver_action=" + ("resume" if resume else "install")]
    if not manual:
        command += ["-e", "@" + str(secrets)]
        vault = root / ".era-secrets/vault-pass"
        command += ["--vault-password-file", str(vault)] if vault.is_file() else ["--ask-vault-pass"]
    print(f"Boot evidence: {artifact}", flush=True)
    result = subprocess.run(command, cwd=root, pass_fds=(lease.fd,))
    require(result.returncode == 0, f"Boot incomplete; retain runtime and review {artifact}/runtime.yaml")
    return artifact


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "prepare-repeat", "verify", "serve", "prepare-boot", "verify-boot", "boot", "resume", "continue"))
    parser.add_argument("--root", type=Path, default=ROOT)
    for name in ("arch", "site", "run-id"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--collection-root", type=Path, required=True)
    parser.add_argument("--authorize-run", default="")
    args = parser.parse_args()
    try:
        root, collection = args.root.absolute(), args.collection_root.absolute()
        with state.Lease(state.site_root(root, args.arch, args.site)) as lease:
            if args.action == "prepare":
                bundle = prepare(root, args.arch, args.site, args.run_id, collection)
                print(f"Prepared: {bundle}; review review.txt. No services or hardware touched.")
            elif args.action == "prepare-repeat":
                bundle = prepare_repeat(root, args.arch, args.site, args.run_id, collection)
                print(f"Prepared repeat: {bundle}; review review.txt. No services or hardware touched.")
                print(f"Inspection source: {args.run_id}; installation attempt: {bundle.name}")
                print("Existing serve/prepare-boot/boot/resume commands automatically select this attempt.")
            elif args.action == "verify":
                verify(root, args.arch, args.site, args.run_id, collection)
                print("ABI serving preparation verified.")
            elif args.action == "serve":
                serve(root, args.arch, args.site, args.run_id, collection, lease)
            elif args.action == "prepare-boot":
                target = prepare_boot(root, args.arch, args.site, args.run_id, collection)
                print(f"Prepared installation review: {target}; no physical authority. No hardware contacted.")
            elif args.action == "verify-boot":
                verify_boot(root, args.arch, args.site, args.run_id, collection)
                print("ABI boot preparation verified; no boot authorized.")
            else:
                boot(root, args.arch, args.site, args.run_id, collection, lease, args.authorize_run,
                     resume=args.action == "resume", continuing=args.action == "continue")
    except (ValueError, KeyError, TypeError, IndexError, OSError, yaml.YAMLError, subprocess.SubprocessError) as error:
        # Errors describe paths/checks, never credential-bearing candidate data.
        reason = str(error) if isinstance(error, ValueError) else type(error).__name__
        parser.exit(1, f"ERROR: ABI serving rejected: {reason}. Review preparation and retained runtime evidence.\n")


if __name__ == "__main__":
    main()
