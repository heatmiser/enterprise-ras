#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Read wipe-only Redfish policy; never start services or contact hardware."""

import argparse
import importlib.util
import json
from pathlib import Path
import re

import yaml


ENABLED = "ironic_deploy_redfish_retry_enabled"
STAGING = "ironic_deploy_redfish_retry_path"
POLICY = "ironic_deploy_redfish_retry_policy"
KEYS = {ENABLED, STAGING, POLICY}


def policy_module(collection):
    """Use the collection's single authoritative policy validator."""
    path = Path(collection) / "plugins/module_utils/redfish_retry.py"
    if not path.is_file() or any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError("Require an ordinary collection retry policy source")
    spec = importlib.util.spec_from_file_location("wipe_redfish_policy", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def normalize(value, collection, staging):
    if not isinstance(value, dict) or set(value) - KEYS:
        raise ValueError("Redfish settings require only the three supported role inputs")
    result = {ENABLED: True, STAGING: str(staging), **value}
    if type(result[ENABLED]) is not bool:
        raise ValueError("Redfish retry enablement requires a YAML boolean")
    path = result[STAGING]
    if (not isinstance(path, str) or not re.fullmatch(r"(/[A-Za-z0-9_.-]+)+", path)
            or any(part in (".", "..") for part in path.split("/"))):
        raise ValueError("Redfish staging path requires a canonical absolute directory")
    result[POLICY] = policy_module(collection).validate_policy(value.get(POLICY))
    return result


def site(root, arch, name, collection, staging):
    path = Path(root).absolute() / "input" / arch / name / "ironic-redfish.yml"
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError("Redfish settings refuse symlink ancestors")
    value = {}
    if path.exists():
        if not path.is_file() or path.stat().st_size > 64 * 1024:
            raise ValueError("Redfish settings require a regular file no larger than 64 KiB")
        value = yaml.safe_load(path.read_text())
    return normalize(value, collection, staging)


def review(value):
    return (f"Redfish adaptive reads: {value[ENABLED]}; policy: "
            + json.dumps(value[POLICY], sort_keys=True)
            + f"; Ironic connection attempts when enabled: 1; staging directory: {value[STAGING]}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--arch", required=True)
    parser.add_argument("--site", required=True)
    parser.add_argument("--collection-root", type=Path, required=True)
    parser.add_argument("--staging-path", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(site(args.root, args.arch, args.site, args.collection_root, args.staging_path)))
    except (ValueError, OSError, ImportError, yaml.YAMLError) as error:
        parser.exit(1, f"ERROR: {error}\n")
