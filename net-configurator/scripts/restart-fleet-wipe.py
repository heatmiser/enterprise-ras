#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""Prepare a fresh wipe from recorded scope; never execute cleaning or remove evidence."""

import argparse
from datetime import datetime, timezone
import importlib.util
import os
from pathlib import Path
import shlex
import site as python_site
import subprocess
import sys
import sysconfig

import yaml


ROOT = Path(__file__).resolve().parent.parent
# The existing workbook reader imports sibling helpers when loaded by the adapter.
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("restart_wipe_adapter", ROOT / "scripts/prepare-fleet-wipe.py")
wipe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wipe)
state = wipe.state
PROBE_SOURCES = {
    "rhvp_ironic_retry_launcher.py": "roles/ironic_deploy/files/redfish_retry/rhvp_ironic_retry_launcher.py",
    "rhvp_redfish_retry.py": "roles/ironic_deploy/files/native_disk_clean/rhvp_redfish_retry.py",
    "rhvp_redfish_deadline.py": "plugins/module_utils/redfish_deadline.py",
    "rhvp_redfish_retry_policy.py": "plugins/module_utils/redfish_retry.py",
}


def inspect_site(base):
    """Validate every terminal attempt before offering disposition for missing records."""
    registry = state.registry(base)
    reports = state.safe_path(base / "reports/wipe")
    observed = {p.name for p in reports.iterdir()} if reports.exists() else set()
    state.require(observed == set(registry["runs"]), "Unregistered or missing cleaning execution evidence")
    attempts, pending = {}, []
    for run, entry in sorted(registry["runs"].items()):
        folder = state.evidence_root(base, run)
        intent = state.read(folder / "execution-intent.yaml")
        state.require(state.digest(folder / "execution-intent.yaml") == entry.get("intent_sha256"),
                      f"Registered cleaning intent hash differs for {run}")
        state.require(intent.get("run_id") == run
                      and intent.get("operation") in (state.OPERATION, state.LEGACY_OPERATION),
                      f"Unsupported registered cleaning intent for {run}")
        receipt = state.validate_receipt(base, run, intent)
        attempts[run] = (intent, receipt)
        disposition = folder / "disposition.yaml"
        if receipt["status"] == "FAILED":
            if not disposition.exists() and not disposition.is_symlink():
                pending.append(run)
            else:
                value = state.read(disposition)
                state.require(value.get("action") == "proceed_without_successful_cleaning"
                              and value.get("run_id") == run and value.get("node_names") == intent["node_names"]
                              and value.get("receipt_sha256") == state.digest(folder / "receipt.yaml")
                              and isinstance(value.get("reason"), str) and bool(value["reason"].strip()),
                              "Existing failed-run disposition differs; preserve and review it")
    return attempts, pending


def run_command(root, argv, collections):
    """Keep the site lease and loader roots; discard inherited Make/wipe authorization."""
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("FLEET_") and key not in ("MAKEFLAGS", "MFLAGS", "MAKEOVERRIDES")}
    paths = [*collections.split(os.pathsep), *python_site.getsitepackages(),
             sysconfig.get_path("purelib"), sysconfig.get_path("platlib"),
             str(Path.home() / ".ansible/collections"), "/usr/share/ansible/collections"]
    paths = os.pathsep.join(dict.fromkeys(path for path in paths if path))
    env.update(ANSIBLE_COLLECTIONS_PATH=paths, ANSIBLE_COLLECTIONS_SCAN_SYS_PATH="false")
    env.pop("ANSIBLE_COLLECTIONS_PATHS", None)
    descriptor = os.environ.get("RHV_NATIVE_OPERATION_LOCK_FD")
    result = subprocess.run(argv, cwd=root, env=env, pass_fds=(int(descriptor),) if descriptor else ())
    state.require(result.returncode == 0, "Preparation command failed; rerun the same restart command after review")


def probe(root, arch, site, collection, collections, source_runtime, work):
    """Reuse the role's networkless, service-free probe, including the native main boundary."""
    image = source_runtime["native_runtime"]["ironic_image"]
    context = source_runtime["deadline_context"]
    state.require(bool(context) and context.get("run_id") == source_runtime["run_id"],
                  "Missing recorded native deadline context")
    retry = wipe.REDFISH.site(root, arch, site, collection, work / "native-probe")
    hashes = {name: state.digest(collection / relative) for name, relative in PROBE_SOURCES.items()}
    provenance = work / "native-provenance.json"
    role_vars = {
        "ironic_deploy_mode": "ephemeral", "ironic_deploy_image": image,
        "ironic_deploy_redfish_retry_enabled": True,
        "ironic_deploy_redfish_retry_path": str(work / "native-probe"),
        "ironic_deploy_redfish_retry_policy": retry[wipe.REDFISH.POLICY],
        "ironic_deploy_redfish_retry_deadline_context": context,
        "ironic_deploy_native_disk_clean": False, "ironic_deploy_physical_lldp_hook": False,
        "ironic_deploy_pull_secret_path": str(Path.home() / ".era-secrets/pull-secret.json"),
    }
    tasks = [
        {"name": "Verify installed retry activation without services or hardware",
         "ansible.builtin.include_role": {"name": "rhvp.baremetal_ocp.ironic_deploy",
                                          "tasks_from": "prepare_redfish_retry"}, "vars": role_vars},
        {"name": "Require verified native bootstrap and exact installed sources",
         "ansible.builtin.assert": {"that": [
             "ironic_deploy_redfish_retry_provenance.bootstrap_verified is sameas true",
             "ironic_deploy_redfish_retry_provenance.compatibility_verified is sameas true",
             "ironic_deploy_redfish_retry_provenance.physical_execution is sameas false",
             "ironic_deploy_redfish_retry_provenance.force_power_state_during_sync is sameas false",
             "ironic_deploy_redfish_retry_provenance.source_sha256 == restart_expected_sources"]}},
        {"name": "Preserve isolated probe provenance", "ansible.builtin.copy": {
            "dest": str(provenance), "mode": "0600",
            "content": "{{ ironic_deploy_redfish_retry_provenance | to_nice_json }}"}},
    ]
    playbook = work / "verify-native.yml"
    state.write(playbook, [{"name": "Verify fresh wipe bootstrap", "hosts": "localhost", "connection": "local",
                            "gather_facts": False, "vars": {"restart_expected_sources": hashes}, "tasks": tasks}])
    run_command(root, ["ansible-playbook", "-i", "localhost,", "--connection", "local", "--forks", "1",
                       str(playbook)], collections)
    value = state.read(provenance)
    state.require(value.get("bootstrap_verified") is True and value.get("compatibility_verified") is True
                  and value.get("physical_execution") is False and value.get("force_power_state_during_sync") is False
                  and value.get("source_sha256") == hashes and value.get("image") == image,
                  "Installed collection needs verified native-bootstrap support; update it before retrying")


def new_run_id():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def check_scope(plan, source, run, concurrency):
    state.require(plan["run_id"] == run and plan["inspection_run_id"] == source["inspection_run_id"]
                  and sorted(n["name"] for n in plan["authorized_nodes"]) == source["node_names"]
                  and plan["concurrency_limit"] == concurrency and plan["clean_steps"] == source["clean_steps"],
                  "Restart scope differs from the reviewed source or concurrency")


def execute_command(root, arch, site, collection, collections, run, names):
    return shlex.join(["make", "wipe-fleet-disks", "ARCH=" + arch, "SITE=" + site,
                       "RHV_BAREMETAL_OCP_ROOT=" + str(collection),
                       "RHV_BAREMETAL_OCP_COLLECTIONS_PATH=" + collections,
                       "FLEET_WIPE_RUN_ID=" + run, "FLEET_AUTHORIZE_WIPE_RUN=" + run,
                       "FLEET_AUTHORIZE_WIPE_NODES=" + ",".join(names)])


def restart(root, arch, site, source_run, concurrency, collection, collections, confirm=input):
    root, collection = root.absolute(), collection.absolute()
    # Prefer the selected installed collection over inherited loader roots.
    namespace = collection.parents[2]
    if collection == namespace / "ansible_collections/rhvp/baremetal_ocp":
        collections = os.pathsep.join([str(namespace), collections])
    base = state.site_root(root, arch, site)
    source_folder = state.evidence_root(base, source_run)
    record = state.safe_path(base / "ocp/wipe/restarts" / (source_run + ".yaml"))
    with state.Lease(base):
        attempts, pending = inspect_site(base)
        state.require(source_run in attempts, "Source run is not registered in the selected architecture/site")
        intent, _ = attempts[source_run]
        state.require(intent["operation"] == state.OPERATION, "Restart source must be an all-disk cleaning run")
        source_plan = state.read(base / "ocp/wipe/fleet" / source_run / "plan.yml")["fleet_native_disk_clean_plan"]
        source = {"inspection_run_id": intent["inspection_run_id"], "node_names": intent["node_names"],
                  "clean_steps": source_plan["clean_steps"]}
        state.require(type(concurrency) is int and 1 <= concurrency <= len(source["node_names"]),
                      "Concurrency must be between one and the recorded node count")
        wipe.gate.validate_handoff(root, arch, site, source["inspection_run_id"])
        identity = {"schema_version": 1, "from_run": source_run, "arch": arch, "site": site,
                    "source_receipt_sha256": state.digest(source_folder / "receipt.yaml"),
                    "concurrency": concurrency, **source}
        progress = state.read(record) if record.exists() else None
        if progress:
            state.require(all(progress.get(key) == value for key, value in identity.items()),
                          "Existing restart selection differs; preserve it for review")
            run = progress["run_id"]
            state.require(not state.evidence_root(base, run).exists(),
                          "Fresh run already executed or started; review its retained evidence")
        else:
            run = new_run_id()
        bundle = state.safe_path(base / "ocp/wipe/fleet" / run)
        work = state.safe_path(base / "ocp/wipe/restarts" / run)
        state.require(run != source_run and (progress is not None or not bundle.exists())
                      and not state.evidence_root(base, run).exists() and run not in state.registry(base)["runs"],
                      "Fresh run ID already exists; no evidence was replaced")
        print(f"Site: {arch}/{site}; inspection: {source['inspection_run_id']}; fresh wipe: {run}")
        print(f"Nodes ({len(source['node_names'])}), concurrency {concurrency}: " + ", ".join(source["node_names"]))
        print("Steps: " + ", ".join(step["step"] for step in source["clean_steps"]))
        if pending:
            print("Safely removed FAILED attempts requiring disposition:")
            for previous in pending:
                print(f"  {previous}: " + ", ".join(attempts[previous][0]["node_names"]))
            state.require(confirm("Record disposition to prepare this retry, preserving all prior results? [yes/no] ")
                          .strip().lower() == "yes", "Restart cancelled; no disposition or preparation written")
        if progress is None:
            progress = {**identity, "run_id": run}
            state.write(record, progress)
        print(f"Restart checkpoint: {record}", flush=True)
        if bundle.exists() and state.read(bundle / "prepared.yaml").get("runtime_sealed") is True:
            state.check(base)
            plan, runtime = wipe.verify(root, arch, site, bundle)
            check_scope(plan, source, run, concurrency)
            state.require(Path(runtime["collection_root"]) == collection, "Prepared collection location differs")
        else:
            work.mkdir(parents=True, exist_ok=True, mode=0o700)
            hashes = wipe.collection_hashes(collection)
            probe(root, arch, site, collection, collections, state.read(source_folder / "runtime.yaml"), work)
            state.require(wipe.collection_hashes(collection) == hashes, "Installed executor changed during probe")
            for previous in pending:
                state.disposition(base, previous, attempts[previous][0]["node_names"],
                                  f"Operator confirmed fresh cleaning preparation from {source_run} as {run}; "
                                  "prior terminal results preserved; no installation or cleaning success claimed.")
            state.check(base)
            if not bundle.exists():
                nvram = any(step["step"] == "clean_uefi_nvram" for step in source["clean_steps"])
                wipe.prepare(root, arch, site, source["inspection_run_id"], run, source["node_names"],
                             collection, concurrency, nvram)
            plan, runtime = wipe.verify_sources(root, arch, site, bundle)
            check_scope(plan, source, run, concurrency)
            state.require(Path(runtime["collection_root"]) == collection
                          and runtime["collection_sha256"] == hashes, "Prepared executor differs from verified probe")
            seed = state.read(bundle / "prepared.yaml")
            for name, wanted in seed["sha256"].items():
                state.require(state.digest(bundle / name) == wanted, "Seed preparation changed")
            vault = root / ".era-secrets/vault-pass"
            vault_args = ["--vault-password-file", str(vault)] if vault.is_file() else ["--ask-vault-pass"]
            run_command(root, ["ansible-playbook", str(root / "playbooks/prepare-fleet-wipe.yml"),
                               "-i", str(bundle / "hosts.yml"), "-e", "@" + str(bundle / "runtime-plan.yml"),
                               *vault_args], collections)
            plan, _ = wipe.verify(root, arch, site, bundle)
            check_scope(plan, source, run, concurrency)
        print((bundle / "review.txt").read_text())
        print("Preparation verified. No nodes booted; no cleaning submitted. Execute the reviewed plan with:")
        print(execute_command(root, arch, site, collection, collections, run, source["node_names"]))
        return bundle


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--arch", required=True)
    parser.add_argument("--site", required=True)
    parser.add_argument("--from-run", required=True)
    parser.add_argument("--concurrency", type=int, required=True)
    parser.add_argument("--collection-root", type=Path, required=True)
    parser.add_argument("--collections-path", required=True)
    args = parser.parse_args()
    try:
        restart(args.root, args.arch, args.site, args.from_run, args.concurrency,
                args.collection_root, args.collections_path)
    except (ValueError, KeyError, TypeError, OSError, ImportError, EOFError, yaml.YAMLError) as error:
        parser.exit(1, f"ERROR: {error}\nRestart stopped; preserve evidence and rerun the same command after review.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
