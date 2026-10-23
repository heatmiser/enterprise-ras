#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Validate named ABI media policy without generating or publishing artifacts."""

from pathlib import Path
import re
from urllib.parse import urlsplit

import yaml

DEFAULTS = {"mode": "full", "boot_artifacts_base_url": ""}
MODES = ("full", "full-ram", "minimal", "manual")


def normalize(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise ValueError("ABI media policy requires only mode and boot_artifacts_base_url")
    result = {**DEFAULTS, **value}
    if result["mode"] not in MODES:
        raise ValueError("ABI media mode requires full, full-ram, minimal or manual")
    url = result["boot_artifacts_base_url"]
    if not isinstance(url, str):
        raise ValueError("ABI boot artifacts base URL requires a string")
    if result["mode"] == "minimal":
        parsed = urlsplit(url)
        if (parsed.scheme != "http" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path != "/redfish"
                or parsed.port != 6180 or not re.fullmatch(r"http://[A-Za-z0-9.-]+:6180/redfish", url)):
            raise ValueError("Managed minimal media requires http://<server>:6180/redfish")
    elif url:
        raise ValueError("Boot artifacts base URL is only permitted for minimal media")
    return result


def content(policy):
    """Manual changes boot ownership, while using ordinary full ISO content."""
    return {**policy, "mode": "full" if policy["mode"] == "manual" else policy["mode"]}


def site(root, arch, name):
    path = Path(root).absolute() / "input" / arch / name / "abi-media.yml"
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("Symlink ABI media policy")
    if not path.exists():
        return normalize({})
    if not path.is_file() or path.stat().st_size > 65536:
        raise ValueError("ABI media policy requires a regular file no larger than 64 KiB")
    return normalize(yaml.safe_load(path.read_text()))


def build_path(base, run_id, build_id=""):
    stage = base / "ocp-iso" / run_id
    if build_id:
        if not isinstance(build_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", build_id):
            raise ValueError("Invalid ABI media build identity")
        stage = stage / "builds" / build_id
    return stage


def derive(agent, policy):
    """Only the two named media fields may differ from inspected input."""
    result = dict(agent)
    result.pop("minimalISO", None)
    result.pop("bootArtifactsBaseURL", None)
    if policy["mode"] == "minimal":
        result.update(minimalISO=True, bootArtifactsBaseURL=policy["boot_artifacts_base_url"])
    return result
