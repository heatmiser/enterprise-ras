#!/usr/bin/env python3
"""Serialize site operations and reject unresolved optional cleaning evidence."""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

import yaml


LEGACY_OPERATION = "native_installation_volume_metadata_cleaning"
OPERATION = "native_all_disks_metadata_cleaning"
SCOPE = "all_native_eligible_disks"


def require(value, reason):
    if not value:
        raise ValueError(reason)


def safe_path(path):
    require(not any(p.is_symlink() for p in (path, *path.parents)), "Symlink operation path")
    return path


def site_root(root, arch, site):
    require(all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value) and value not in (".", "..")
                for value in (arch, site)), "Invalid site identity")
    return safe_path(root.absolute() / "output" / arch / site)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def digest(path):
    require(safe_path(path).is_file(), "Missing regular evidence")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path):
    require(safe_path(path).is_file() and path.stat().st_size <= 16 * 1024 * 1024, "Missing or oversized evidence")
    value = yaml.safe_load(path.read_text())
    require(isinstance(value, dict), "Require evidence mapping")
    return value


def write(path, value):
    safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".cleaning-")
    try:
        with os.fdopen(descriptor, "w") as stream:
            yaml.safe_dump(value, stream, sort_keys=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Lease:
    """Hold an exclusive kernel lease through the child command, including ISO creation."""

    def __init__(self, base):
        self.base = safe_path(base)
        self.fd = None
        self.borrowed = False
        self.previous = {}

    def __enter__(self):
        path = safe_path(self.base / "ocp/.operation.lock")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        inherited = os.environ.get("RHV_NATIVE_OPERATION_LOCK_FD")
        if inherited:
            require(os.environ.get("RHV_NATIVE_OPERATION_SITE") == str(self.base), "Wrong inherited site lease")
            self.fd = int(inherited)
            observed, expected = os.fstat(self.fd), path.stat()
            require((observed.st_dev, observed.st_ino) == (expected.st_dev, expected.st_ino), "Wrong inherited lease file")
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.borrowed = True
        else:
            self.fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except Exception:
                os.close(self.fd)
                self.fd = None
                raise
            for key, value in {"RHV_NATIVE_OPERATION_LOCK_FD": str(self.fd),
                               "RHV_NATIVE_OPERATION_SITE": str(self.base)}.items():
                self.previous[key] = os.environ.get(key)
                os.environ[key] = value
        return self

    def __exit__(self, *args):
        if not self.borrowed:
            for key, value in self.previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            os.close(self.fd)


def registry(base):
    path = base / "ocp/cleaning-registry.yaml"
    result = read(path) if path.exists() or path.is_symlink() else {"schema_version": 1, "runs": {}}
    require(result.get("schema_version") == 1 and isinstance(result.get("runs"), dict), "Invalid cleaning registry")
    return result


def evidence_root(base, run):
    require(isinstance(run, str) and re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", run), "Invalid cleaning run")
    return safe_path(base / "reports/wipe" / run)


def begin(base, plan, prepared_sha):
    require(os.environ.get("RHV_NATIVE_OPERATION_SITE") == str(base), "Execution requires a site lease")
    with Lease(base):
        check(base)
        run = plan["run_id"]
        destination = evidence_root(base, run)
        require(plan.get("artifact_root") == str(destination) and not destination.exists(), "Existing or wrong execution destination")
        require(plan.get("schema_version") == 3 and plan.get("operation") == OPERATION
                and plan.get("scope") == SCOPE and plan.get("policy", {}).get("operator_disk_exclusions") == []
                and plan.get("execution_ready") is False, "Require fresh all-disk native preparation")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination.mkdir(mode=0o700)
        intent = {"schema_version": 1, "operation": OPERATION, "run_id": run,
                  "inspection_run_id": plan["inspection_run_id"], "node_names": sorted(n["name"] for n in plan["authorized_nodes"]),
                  "preparation_sha256": fingerprint(plan), "prepared_sha256": prepared_sha,
                  "manifest_sha256": plan["manifest_sha256"], "scope": SCOPE, "status": "STARTED"}
        write(destination / "execution-intent.yaml", intent)
        state = registry(base)
        require(run not in state["runs"], "Cleaning run already registered")
        state["runs"][run] = {"intent_sha256": digest(destination / "execution-intent.yaml")}
        write(base / "ocp/cleaning-registry.yaml", state)
        return intent


def validate_receipt(base, run, intent):
    folder = evidence_root(base, run)
    receipt = read(folder / "receipt.yaml")
    require(all(receipt.get(k) == intent.get(k) for k in
                ("schema_version", "operation", "run_id", "inspection_run_id", "node_names", "preparation_sha256", "manifest_sha256")),
            "Cleaning receipt binding changed")
    bundle = safe_path(base / "ocp/wipe/fleet" / run)
    require(digest(bundle / "prepared.yaml") == intent.get("prepared_sha256"), "Preparation seal changed")
    prepared = read(bundle / "prepared.yaml")
    require(prepared.get("runtime_sealed") is True and prepared.get("execution_ready") is False,
            "Missing sealed runtime preparation")
    for path, wanted in prepared["sha256"].items():
        require(digest(bundle / path) == wanted, "Sealed preparation changed")
    plan = read(bundle / "plan.yml")["fleet_native_disk_clean_plan"]
    require(fingerprint(plan) == intent.get("preparation_sha256"), "Prepared native plan changed")
    names = intent["node_names"]
    require(names and names == sorted(n["name"] for n in plan["authorized_nodes"]), "Wrong prepared fleet")
    expected = {"summary.yaml", "runtime.yaml", "execution-intent.yaml"}
    expected.update(f"{name}/outcome.yaml" for name in names)
    require(set(receipt.get("evidence_sha256", {})) == expected, "Incomplete cleaning receipt coverage")
    for name, wanted in receipt["evidence_sha256"].items():
        require(digest(folder / name) == wanted, "Cleaning evidence changed")
    summary, runtime = read(folder / "summary.yaml"), read(folder / "runtime.yaml")
    require(all(summary.get(k) == receipt.get(k) for k in receipt if k != "evidence_sha256"), "Receipt differs from summary")
    require(runtime.get("run_id") == run and runtime.get("status") == "removed"
            and runtime.get("cleanup_status") == "complete" and runtime.get("runtime_retained") is False
            and runtime.get("lock_owned") is False and receipt.get("cleanup_verified") is True, "Cleaning runtime not safely removed")
    require(plan.get("operation") == intent.get("operation"), "Cleaning operation changed")
    if intent["operation"] == OPERATION:
        require(plan.get("schema_version") == 3 and plan.get("scope") == SCOPE
                and intent.get("scope") == receipt.get("scope") == SCOPE
                and plan.get("policy", {}).get("operator_disk_exclusions") == []
                and plan.get("clean_steps") in ([{"interface": "deploy", "step": "erase_devices_metadata", "args": {}}], [{"interface": "deploy", "step": "clean_uefi_nvram", "args": {}}, {"interface": "deploy", "step": "erase_devices_metadata", "args": {}}]),
                "Wrong all-disk cleaning scope")
        expected_request = {"preparation": plan, "authorization": {
            "action": "erase_devices_metadata", "scope": SCOPE, "run_id": run, "node_names": names,
            "preparation_sha256": intent["preparation_sha256"], "node_uuids": runtime.get("uuids", {})}}
        request_sha = fingerprint(expected_request)
        successes = []
        for name in names:
            node = read(folder / name / "outcome.yaml")
            require(node.get("run_id") == run and node.get("node") == name, "Wrong cleaning node")
            if node.get("status") == "SKIPPED":
                require(node.get("submitted") is False, "Skipped node may have started cleaning")
            else:
                cleanup = node.get("cleanup", {})
                require(node.get("cleanup_safe") is True and cleanup.get("verified") is True
                        and cleanup.get("power_state") == "Off" and cleanup.get("media_detached") is True,
                        "Node cleanup not verified")
                require(node.get("scope") == SCOPE and node.get("request_sha256") == request_sha
                        and node.get("uuid") == runtime.get("uuids", {}).get(name), "Native request identity changed")
                if node.get("submitted") is True:
                    require(node.get("submission") == {"attempts": 1, "accepted": True, "http_status": 202}
                            and node.get("native_step") == plan["clean_steps"][0]
                            and node.get("clean_step_present") is False
                            and node.get("final_state") in ("manageable", "clean failed"), "Missing native terminal result")
            success = node.get("status") == "SUCCEEDED" and node.get("native_success") is True
            if success:
                require(node.get("submitted") is True and node.get("final_state") == "manageable"
                        and not node.get("last_error") and node.get("maintenance") is False
                        and node.get("target_provision_state") in (None, "manageable"), "Missing native completion")
            successes.append(success)
    else:
        require(intent["operation"] == LEGACY_OPERATION, "Unknown cleaning operation")
        expected_request = {"preparation": plan, "authorization": {
            "action": "erase_devices_metadata", "run_id": run, "node_names": names,
            "preparation_sha256": intent["preparation_sha256"], "node_uuids": runtime.get("uuids", {}),
            "agent_endpoints": runtime.get("agent_endpoints", {}), "agent_version": runtime.get("agent_metadata", {}).get("version"),
            "conductor_sha256": runtime.get("native_extension", {}).get("source_sha256", {}).get("rhvp_ironic_native_disk_clean.py"),
            "scope_validator_sha256": runtime.get("native_extension", {}).get("source_sha256", {}).get("fleet_native_disk_clean_scope.py"),
            "redactor_sha256": runtime.get("native_extension", {}).get("source_sha256", {}).get("runtime_diagnostics.py")}}
        request_sha = fingerprint(expected_request)
        successes = []
        for name in names:
            node = read(folder / name / "outcome.yaml")
            require(node.get("run_id") == run and node.get("node") == name, "Wrong cleaning node")
            if node.get("status") == "SKIPPED":
                require(node.get("submitted") is False, "Skipped node may have started cleaning")
            else:
                cleanup = node.get("cleanup", {})
                require(node.get("cleanup_safe") is True and cleanup.get("verified") is True
                        and cleanup.get("power_state") == "Off" and cleanup.get("media_detached") is True,
                        "Node cleanup not verified")
            if node.get("submitted") is True:
                require(node.get("request_sha256") == request_sha and node.get("uuid") == runtime["uuids"].get(name),
                        "Native request identity changed")
                boot = node.get("boot", {})
                require(boot.get("node") == name and boot.get("uuid") == node["uuid"]
                        and boot.get("phase") == "BOOT_AUTHORIZED" and boot.get("mutation_attempted") is False
                        and boot.get("request_sha256") == request_sha
                        and boot.get("preparation_sha256") == intent["preparation_sha256"], "Missing native boot binding")
                if node.get("mutation_attempted") is True:
                    target = next(n for n in plan["authorized_nodes"] if n["name"] == name)
                    scope, command = node.get("scope", {}), node.get("command", {})
                    require(scope.get("node_plan_sha256") == fingerprint(target)
                            and scope.get("host_identity") == target["host_identity"]
                            and scope.get("status") == "SNAPSHOT_VERIFIED" and scope.get("target_devices")
                            and scope.get("skip_block_devices") == [{"name": d} for d in scope.get("protected_devices", [])]
                            and not set(scope["target_devices"]) & set(scope["protected_devices"])
                            and command.get("request_sha256") == request_sha
                            and command.get("preparation_sha256") == intent["preparation_sha256"]
                            and command.get("terminal_confirmed") is True and command.get("dispatch_count") == 1
                            and command.get("command_id") == node.get("command_outcome", {}).get("id")
                            and node.get("command_outcome", {}).get("clean_step") == plan["clean_steps"][-1],
                            "Missing terminal scoped native command")
            success = node.get("status") == "SUCCEEDED" and node.get("native_success") is True
            if success:
                require(node.get("final_state") == "manageable" and node.get("mutation_attempted") is True
                        and node.get("scope", {}).get("target_devices") and node.get("command", {}).get("dispatch_count") == 1
                        and node.get("command", {}).get("terminal_confirmed") is True
                        and node.get("command_outcome", {}).get("command_name") == "execute_clean_step"
                        and node["command_outcome"].get("command_status") == "SUCCEEDED", "Missing native completion")
            successes.append(success)
    if receipt.get("status") == "SUCCEEDED":
        require(all(successes) and receipt.get("native_completion_verified") is True, "False successful receipt")
    else:
        require(receipt.get("status") == "FAILED" and receipt.get("native_completion_verified") is False,
                "Nonterminal cleaning receipt")
    return receipt


def check(base):
    state = registry(base)
    reports = safe_path(base / "reports/wipe")
    observed = {p.name for p in reports.iterdir()} if reports.exists() else set()
    require(observed == set(state["runs"]), "Unregistered or missing cleaning execution evidence")
    for run, entry in state["runs"].items():
        folder = evidence_root(base, run)
        require(digest(folder / "execution-intent.yaml") == entry.get("intent_sha256"), "Cleaning intent changed")
        intent = read(folder / "execution-intent.yaml")
        require(intent.get("run_id") == run and intent.get("operation") in (OPERATION, LEGACY_OPERATION), "Wrong cleaning intent")
        receipt = validate_receipt(base, run, intent)
        if receipt["status"] == "FAILED":
            disposition = read(folder / "disposition.yaml")
            require(disposition.get("action") == "proceed_without_successful_cleaning"
                    and disposition.get("run_id") == run and disposition.get("node_names") == intent["node_names"]
                    and disposition.get("receipt_sha256") == digest(folder / "receipt.yaml")
                    and isinstance(disposition.get("reason"), str) and disposition["reason"].strip(),
                    "Failed cleaning requires recorded operator disposition")


def disposition(base, run, names, reason):
    require(isinstance(reason, str) and 0 < len(reason.strip()) <= 2048, "Disposition requires a bounded reason")
    with Lease(base):
        folder = evidence_root(base, run)
        state = registry(base)
        intent = read(folder / "execution-intent.yaml")
        require(state["runs"].get(run, {}).get("intent_sha256") == digest(folder / "execution-intent.yaml"), "Unregistered cleaning")
        receipt = validate_receipt(base, run, intent)
        require(receipt["status"] == "FAILED" and sorted(names) == intent["node_names"]
                and len(names) == len(set(names)), "Disposition requires exact failed terminal fleet")
        require(not (folder / "disposition.yaml").exists(), "Preserve prior disposition")
        write(folder / "disposition.yaml", {"schema_version": 1, "action": "proceed_without_successful_cleaning",
              "run_id": run, "node_names": sorted(names), "receipt_sha256": digest(folder / "receipt.yaml"),
              "operator_uid": os.getuid(), "reason": reason.strip()})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("lease", "check", "disposition"))
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--arch", required=True)
    parser.add_argument("--site", required=True)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--nodes", default="")
    arguments = sys.argv[1:]
    split = arguments.index("--") if "--" in arguments else len(arguments)
    child = arguments[split + 1:]
    args = parser.parse_args(arguments[:split])
    try:
        base = site_root(args.root, args.arch, args.site)
        if args.command == "lease":
            command = child
            require(command, "Missing leased command")
            with Lease(base) as lease:
                result = subprocess.run(command, pass_fds=(lease.fd,), check=False)
            return result.returncode
        if args.command == "check":
            with Lease(base):
                check(base)
        else:
            disposition(base, args.run_id, args.nodes.split(","), os.environ.get("FLEET_WIPE_DISPOSITION_REASON", ""))
    except (ValueError, KeyError, TypeError, OSError, yaml.YAMLError):
        parser.exit(1, "ERROR: Site operation rejected; review lease, started cleaning evidence, native completion and cleanup.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
