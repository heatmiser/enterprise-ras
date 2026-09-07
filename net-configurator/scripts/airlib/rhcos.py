# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""Shared RHCOS helpers used by both air-deploy.py and generate-node-instructions.py."""

import base64
import json

import yaml


def build_rhcos_nmstate_config(
    eth0_ip: str,
    prefix_len: int = 25,
    gateway: str = "",
    mac: str = "",
) -> dict:
    """Return NMState network config dict for a single static-IP OOB interface.

    When mac is provided, NMState 2.x matches the physical interface by MAC address
    (identifier: mac-address) so the config applies regardless of kernel NIC naming
    (ens8 in Air, varies on real hardware). The connection profile is named 'oob0'.
    """
    iface: dict = {
        "name": "oob0",
        "type": "ethernet",
        "state": "up",
        "ipv4": {
            "enabled": True,
            "address": [{"ip": eth0_ip, "prefix-length": prefix_len}],
            "dhcp": False,
        },
    }
    if mac:
        iface["identifier"] = "mac-address"
        iface["mac-address"] = mac

    return {
        "interfaces": [iface],
        "routes": {
            "config": [
                {
                    "destination": "0.0.0.0/0",
                    "next-hop-address": gateway,
                    "next-hop-interface": "oob0",
                }
            ]
        },
    }


def generate_rhcos_ignition_payload(
    node_name: str,
    network_config: dict,
    ssh_key: str | None = None,
) -> str:
    """Generate Ignition v3.4.0 JSON payload for RHCOS OpenStack qcow2 images.

    Configures:
      1. /etc/hostname
      2. /etc/nmstate/network-config.yml  (NMState applied by era-nmstate.service)
      3. era-nmstate.service (oneshot, applies NMState on first boot)
      4. serial-getty@ttyS0.service (serial console in NVIDIA Air GUI)
      5. passwd.users core sshAuthorizedKeys (if ssh_key provided)
    """
    nmstate_yaml = yaml.dump(network_config, default_flow_style=False, sort_keys=False)

    def _data_url(content: str) -> str:
        b64 = base64.b64encode(content.encode("utf-8")).decode("utf-8")
        return f"data:text/plain;charset=utf-8;base64,{b64}"

    systemd_unit = (
        "[Unit]\n"
        "Description=Apply ERA Stage 1 NMState Configuration\n"
        "After=NetworkManager.service\n"
        "Requires=NetworkManager.service\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        "ExecStart=/usr/bin/nmstatectl apply /etc/nmstate/network-config.yml\n"
        "RemainAfterExit=yes\n\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )

    ignition_cfg: dict = {
        "ignition": {"version": "3.4.0"},
        "kernelArguments": {"shouldExist": ["console=ttyS0,115200n8"]},
        "storage": {
            "files": [
                {
                    "overwrite": True,
                    "path": "/etc/hostname",
                    "mode": 420,
                    "contents": {"source": _data_url(f"{node_name}\n")},
                },
                {
                    "overwrite": True,
                    "path": "/etc/nmstate/network-config.yml",
                    "mode": 420,
                    "contents": {"source": _data_url(nmstate_yaml)},
                },
            ]
        },
        "systemd": {
            "units": [
                {"name": "era-nmstate.service", "enabled": True, "contents": systemd_unit},
                {"name": "serial-getty@ttyS0.service", "enabled": True},
            ]
        },
    }

    if ssh_key:
        ignition_cfg["passwd"] = {
            "users": [{"name": "core", "sshAuthorizedKeys": [ssh_key.strip()]}]
        }

    return json.dumps(ignition_cfg, indent=2) + "\n"
