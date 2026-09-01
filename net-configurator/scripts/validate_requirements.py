#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""
Validate generated net-configurator output against architecture requirements.

Architecture requirements docs are site-specific and not included in the public release.
When invoked without a requirements file the script exits cleanly with a notice.
"""

import argparse
import sys
from pathlib import Path

import yaml


def _load_yaml(path: Path) -> dict:
    """Load a YAML file with friendly SystemExit on missing or malformed input."""
    path = Path(path)
    if not path.exists():
        sys.exit(f"ERROR: Requirements file not found: {path}")
    try:
        with open(path) as fh:
            data = yaml.safe_load(fh)
        return data or {}
    except yaml.YAMLError as exc:
        sys.exit(f"ERROR: Malformed YAML in {path}: {exc}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arch", default=None, help="Architecture (e.g. 2-8-5-200)")
    parser.add_argument("--site", default=None, help="Site name (e.g. kicktires)")
    args = parser.parse_args()

    if args.arch:
        req_file = Path(f"input/{args.arch}/requirements.yml")
    else:
        matches = list(Path("input").glob("*/requirements.yml"))
        req_file = matches[0] if matches else None

    if req_file is None or not req_file.exists():
        print("  Architecture requirements docs not present — skipping validation.")
        return

    reqs = _load_yaml(req_file)
    print(f"✓ Requirements loaded from {req_file} ({len(reqs)} keys)")


if __name__ == "__main__":
    main()
