#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Read preparation-only HTTPD policy; never deploy services or apply sysctls."""

import argparse
import json
from pathlib import Path
import re

import yaml


DEFAULTS = {
    "ironic_deploy_httpd_tuning": False,
    "ironic_deploy_httpd_tuning_path": "/var/lib/ironic/httpd-tuning",
    "ironic_deploy_httpd_manage_sysctl": False,
}


def normalize(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise ValueError("HTTPD settings require a mapping containing only the three supported role inputs")
    result = {**DEFAULTS, **value}
    for name in ("ironic_deploy_httpd_tuning", "ironic_deploy_httpd_manage_sysctl"):
        if type(result[name]) is not bool:
            raise ValueError(f"HTTPD setting {name} requires a YAML boolean")
    path = result["ironic_deploy_httpd_tuning_path"]
    if (not isinstance(path, str) or not re.fullmatch(r"(/[A-Za-z0-9_.-]+)+", path)
            or any(part in (".", "..") for part in path.split("/"))):
        raise ValueError("HTTPD staging path requires a canonical absolute directory")
    if result["ironic_deploy_httpd_manage_sysctl"] and not result["ironic_deploy_httpd_tuning"]:
        raise ValueError("HTTPD host sysctl management requires the Apache profile")
    return result


def read(path):
    path = Path(path)
    if not path.is_absolute() or any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError("HTTPD settings require an absolute path without symlink ancestors")
    if not path.exists():
        return normalize({})
    if not path.is_file() or path.stat().st_size > 64 * 1024:
        raise ValueError("HTTPD settings require a regular file no larger than 64 KiB")
    return normalize(yaml.safe_load(path.read_text()))


def site(root, arch, name):
    return read(Path(root).absolute() / "input" / arch / name / "ironic-httpd.yml")


def verify_preflight(preflight, policy):
    expected = normalize(policy)
    actual = normalize({key: preflight[key] for key in DEFAULTS if key in preflight})
    if actual != expected:
        raise ValueError("HTTPD preflight settings differ from the reviewed preparation")


def review(policy):
    value = normalize(policy)
    return (f"HTTPD profile: {value['ironic_deploy_httpd_tuning']}; "
            f"host sysctls: {value['ironic_deploy_httpd_manage_sysctl']}; "
            f"staging directory: {value['ironic_deploy_httpd_tuning_path']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(read(args.path)))
    except (ValueError, OSError, yaml.YAMLError) as error:
        parser.exit(1, f"ERROR: {error}\n")
