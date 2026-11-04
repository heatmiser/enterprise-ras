#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""Validate `.era-context` and print shell-safe ARCH / SITE assignments.

Usage (from a Makefile recipe or shell):

    eval "$(python3 scripts/_era_context.py)"
    # sets: _ARCH and _SITE in the calling shell

Prints two lines of the form:

    _ARCH='2-8-5-200'
    _SITE='default'

Both values are passed through ``shlex.quote`` so they are safe to
``eval``. Exits non-zero with a diagnostic on stderr if ``.era-context``
is missing, malformed, or contains values that don't pass input
validation (e.g. an arch name not in the known set, or a site name
with characters that could traverse paths / inject shell metacharacters).

This script exists specifically to replace a 200-plus-char inline
Python one-liner in the Makefile that interpolated ``.era-context``
values directly into a Python string literal — a path-traversal /
command-injection hazard if the context file ever contained a
malicious ARCH or SITE. Centralising read + validation here means
every Makefile target that consumes the context gets the same
protection.
"""

import re
import shlex
import sys
from datetime import datetime
from pathlib import Path

VALID_ARCHS = {"2-4-3-200", "2-4-5-800", "2-8-5-200", "2-8-9-400", "2-8-9-800", "2-8-9-400-SP"}
VALID_SERVER_OS = {"ubuntu", "rhcos"}
# Site names end up in filesystem paths (input/<arch>/<site>/...), so
# require a conservative allowlist — no slashes, no dots-only, no spaces.
SITE_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]*$")

CONTEXT_PATH = Path(".era-context")


def die(message: str) -> None:
    sys.stderr.write(f"era-context error: {message}\n")
    sys.exit(1)


def read_context(path: Path = CONTEXT_PATH) -> dict:
    """Read validated deployment context for Make and existing lifecycle adapters."""
    if not path.exists():
        raise ValueError(
            "no .era-context in the current directory — "
            "run `make use ARCH=<type> [SITE=<name>]` to create one"
        )

    arch = None
    site = None
    server_os = None
    fleet_run_id = None
    fleet_wipe_run_id = None
    media_selection = {}
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition(':')
        if key in ('abi_media_build_id', 'abi_media_run_id', 'abi_media_receipt_sha256'):
            if key in media_selection:
                raise ValueError(f'duplicate {key} in .era-context')
            media_selection[key] = value.strip()
            continue
        m = re.match(r"^arch:\s*(\S+)\s*$", line)
        if m:
            arch = m.group(1)
            continue
        m = re.match(r"^site:\s*(\S+)\s*$", line)
        if m:
            site = m.group(1)
            continue
        m = re.match(r"^server_os:\s*(\S+)\s*$", line)
        if m:
            server_os = m.group(1)
            continue
        if line.startswith('fleet_run_id:'):
            if fleet_run_id is not None:
                raise ValueError('duplicate fleet_run_id in .era-context')
            fleet_run_id = line.split(':', 1)[1].strip()
        if line.startswith('fleet_wipe_run_id:'):
            if fleet_wipe_run_id is not None:
                raise ValueError('duplicate fleet_wipe_run_id in .era-context')
            fleet_wipe_run_id = line.split(':', 1)[1].strip()

    if arch is None:
        raise ValueError(".era-context is missing an 'arch:' line")
    if arch not in VALID_ARCHS:
        raise ValueError(
            f"invalid arch {arch!r} in .era-context — must be one of: "
            + ", ".join(sorted(VALID_ARCHS))
        )
    if site is None:
        site = "default"
    if not SITE_RE.match(site):
        raise ValueError(
            f"invalid site {site!r} in .era-context — must match "
            "[A-Za-z0-9_-][A-Za-z0-9._-]* (no slashes, no leading dot)"
        )
    # Extra belt-and-braces against path traversal via sneaky site names.
    if ".." in site or site in ("", ".", ".."):
        raise ValueError(f"invalid site {site!r} in .era-context — disallowed value")

    if server_os is not None and server_os not in VALID_SERVER_OS:
        raise ValueError(
            f"invalid server_os {server_os!r} in .era-context — must be one of: "
            + ", ".join(sorted(VALID_SERVER_OS))
        )

    context = {'arch': arch, 'site': site}
    if server_os is not None:
        context['server_os'] = server_os
    for key, value in [('fleet_run_id', fleet_run_id), ('fleet_wipe_run_id', fleet_wipe_run_id)]:
        if value is not None:
            if not re.fullmatch(r'[0-9]{8}T[0-9]{6}Z', value):
                raise ValueError(f'invalid {key}; require UTC YYYYMMDDTHHMMSSZ')
            datetime.strptime(value, '%Y%m%dT%H%M%SZ')
            context[key] = value
    if media_selection:
        if set(media_selection) != {'abi_media_build_id', 'abi_media_run_id', 'abi_media_receipt_sha256'}:
            raise ValueError('incomplete ABI media selection in .era-context')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', media_selection['abi_media_build_id']):
            raise ValueError('invalid ABI media build identity in .era-context')
        if media_selection['abi_media_run_id'] != fleet_run_id:
            raise ValueError('ABI media selection belongs to another inspection run')
        if not re.fullmatch(r'[0-9a-f]{64}', media_selection['abi_media_receipt_sha256']):
            raise ValueError('invalid ABI media receipt digest in .era-context')
        context.update(media_selection)
    return context


def main() -> int:
    try:
        context = read_context()
    except (ValueError, OSError) as exc:
        die(str(exc))
    for key, value in context.items():
        print(f"_{key.upper()}={shlex.quote(value)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
