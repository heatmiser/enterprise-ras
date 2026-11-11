#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Run oc in the selected successful installation's immutable installer EE."""

import importlib.util
import os
from pathlib import Path
import re
import subprocess
import sys

import yaml

ROOT = Path(__file__).resolve().parent.parent


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CONTEXT = load(ROOT / "scripts/_era_context.py", "oc_ee_context")
INSTALL = load(ROOT / "scripts/prepare-abi-install.py", "oc_ee_install")


def selected_access(root):
    """Resolve existing context/attempt contracts without changing any evidence."""
    state = INSTALL.state
    require = INSTALL.require
    context = CONTEXT.read_context(state.safe_path(root / ".era-context"))
    arch, site, run = context["arch"], context["site"], context.get("fleet_run_id", "")
    base = state.site_root(root, arch, site)
    bundle, attempt = INSTALL.selected_installation(base, run)
    _, reports = INSTALL.installation_paths(base, run, attempt)
    identity = attempt or run
    serving = state.read(bundle / "plan.yml")["abi_install_plan"]
    require(state.fingerprint(serving) == state.read(bundle / "prepared.yaml")["plan_sha256"],
            "Selected serving plan changed")
    require(serving["inspection_run_id"] == run
            and serving.get("installation_attempt_id", run) == identity
            and serving["reports_dir"] == str(reports / "serving"), "Serving identity mismatch")
    boot = state.read(bundle / "boot/plan.yml")["abi_boot_plan"]
    require(state.fingerprint(boot) == state.read(bundle / "boot/prepared.yaml")["plan_sha256"],
            "Selected boot plan changed")
    require(boot["serving_plan"] == serving and boot["inspection_run_id"] == run
            and boot.get("installation_attempt_id", run) == identity
            and boot["artifact_root"] == str(reports / "boot"), "Boot identity mismatch")
    outcome = state.read(reports / "boot/runtime.yaml")
    require(outcome.get("status") == "SUCCEEDED"
            and all(outcome.get(k) is True for k in (
                "bootstrap_complete", "installation_complete", "fleet_ready"))
            and outcome.get("cleanup_status") == "complete"
            and outcome.get("runtime_retained") is False,
            "Selected installation is incomplete; oc access requires successful completion")
    require(outcome.get("inspection_run_id") == run
            and outcome.get("installation_attempt_id", run) == identity
            and outcome.get("node_names") == serving["node_names"]
            and outcome.get("iso_sha256") == serving["iso_sha256"], "Completion identity mismatch")
    image = serving["installer_ee"]
    require(isinstance(image, str) and re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", image),
            "Installer EE must use an immutable digest")
    kubeconfig = state.safe_path(reports / "boot/installer/auth/kubeconfig")
    require(kubeconfig.is_file() and os.access(kubeconfig, os.R_OK), "Selected kubeconfig is missing or unreadable")
    require(state.digest(kubeconfig) == serving["source_sha256"]["auth/kubeconfig"],
            "Selected kubeconfig differs from sealed installer credentials")
    return image, kubeconfig


def command(image, kubeconfig, args):
    argv = ["podman", "run", "--rm", "-i"]
    if sys.stdin.isatty() and sys.stdout.isatty():
        argv.append("-t")
    return argv + ["--userns=keep-id", "--network=host", "-v",
                   f"{kubeconfig}:/work/kubeconfig:ro,z", "--entrypoint=oc", image,
                   "--kubeconfig=/work/kubeconfig", *args]


def main():
    try:
        image, kubeconfig = selected_access(ROOT)
        return subprocess.run(command(image, kubeconfig, sys.argv[1:]), check=False).returncode
    except (ValueError, KeyError, TypeError, IndexError, OSError, yaml.YAMLError):
        # Parsed installer inputs and subprocess arguments can contain credentials.
        print("oc_ee: cannot use the selected installation; check context, completed installation "
              "receipts, private kubeconfig and Podman availability. No older attempt was selected.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
