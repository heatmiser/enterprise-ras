# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""Unit tests for --server-os flag in generate-node-instructions.py."""

import base64
import importlib.util
import json
import sys
from pathlib import Path

import yaml

scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))

spec = importlib.util.spec_from_file_location("generate_node_instructions", scripts_dir / "generate-node-instructions.py")
gni = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gni)


def sample_network_config():
    return {
        "interfaces": [
            {
                "name": "bond0",
                "type": "bond",
                "state": "up",
                "ipv4": {
                    "enabled": True,
                    "dhcp": False,
                    "address": [{"ip": "10.78.221.10", "prefix-length": 24}],
                },
                "link-aggregation": {"mode": "active-backup", "port": ["eth1", "eth2"]},
            }
        ],
        "routes": {
            "config": [
                {
                    "destination": "0.0.0.0/0",
                    "next-hop-address": "10.78.221.1",
                    "next-hop-interface": "bond0",
                }
            ]
        },
    }


def test_generate_rhcos_ignition_payload():
    node_name = "test-node-1"
    net_cfg = sample_network_config()

    payload_str = gni.generate_rhcos_ignition_payload(node_name, net_cfg)
    payload = json.loads(payload_str)

    assert payload["ignition"]["version"] == "3.4.0"

    files = {f["path"]: f for f in payload["storage"]["files"]}
    assert "/etc/hostname" in files
    assert "/etc/nmstate/network-config.yml" in files

    # Verify hostname content
    hostname_src = files["/etc/hostname"]["contents"]["source"]
    assert hostname_src.startswith("data:text/plain;charset=utf-8;base64,")
    hostname_b64 = hostname_src.split("base64,")[1]
    assert base64.b64decode(hostname_b64).decode() == "test-node-1\n"

    # Verify nmstate content
    nmstate_src = files["/etc/nmstate/network-config.yml"]["contents"]["source"]
    nmstate_b64 = nmstate_src.split("base64,")[1]
    decoded_nmstate = base64.b64decode(nmstate_b64).decode()
    assert yaml.safe_load(decoded_nmstate) == net_cfg

    # Verify era-nmstate.service unit
    units = {u["name"]: u for u in payload["systemd"]["units"]}
    assert "era-nmstate.service" in units
    unit = units["era-nmstate.service"]
    assert unit["enabled"] is True
    assert "ExecStart=/usr/bin/nmstatectl apply /etc/nmstate/network-config.yml" in unit["contents"]
