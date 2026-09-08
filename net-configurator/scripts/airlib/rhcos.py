# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""Shared RHCOS helpers used by both air-deploy.py and generate-node-instructions.py."""

import base64
import json

import yaml


def _parse_cidr(cidr: str) -> tuple[str, int]:
    """Split 'x.x.x.x/n' → (ip_str, prefix_int). Returns (cidr, 24) on bad input."""
    if not cidr or "/" not in cidr:
        return cidr or "", 24
    ip, pfx = cidr.rsplit("/", 1)
    try:
        return ip, int(pfx)
    except ValueError:
        return ip, 24


def build_rhcos_nmstate_config(
    eth0_ip: str,
    prefix_len: int = 25,
    gateway: str = "",
    mac: str = "",
    bond_ip: str = "",
    bond_gateway: str = "",
    bond_mac1: str = "",
    bond_mac2: str = "",
    bond_mode: str = "active-backup",
    bond_vlan: int | None = None,
    gpu_ifaces: list | None = None,
    gpu_macs: dict | None = None,
) -> dict:
    """Return NMState network config dict for a full ERA fabric node.

    OOB is always configured. CPU/support bond and GPU rails are added when
    the corresponding parameters are provided. MAC-based matching
    (identifier: mac-address) is used throughout so the config is independent
    of kernel NIC naming (predictable PCI names vs eth*). Profile names
    (oob0, cpu-bond-0/1, bond0, rail1–rail4) are NM connection names; the OS
    maps them to kernel devices by MAC.

    Args:
        eth0_ip:      OOB static IP (bare, no CIDR; prefix_len carries the mask)
        prefix_len:   OOB subnet prefix length
        gateway:      OOB default gateway
        mac:          OOB NIC MAC for identifier matching
        bond_ip:      Bond IP with CIDR, e.g. '10.78.221.201/24'
        bond_gateway: Default-route gateway for bond (optional)
        bond_mac1/2:  MACs of the two bond member NICs (from topology links)
        bond_mode:    NM bond mode; 'active-backup' for Air, '802.3ad' for HW
        bond_vlan:    When set, bond0 carries no IP; a bond0.<bond_vlan> VLAN
                      sub-interface is created instead (needed when the switch
                      port native VLAN differs from the target network VLAN,
                      e.g. k8s support nodes on VLAN 400 with native VLAN 300)
        gpu_ifaces:   List of {iface, plane, ip, gateway, table} dicts from inventory
        gpu_macs:     {topo_iface_name → mac} for MAC-based GPU rail matching
    """
    interfaces: list[dict] = []
    routes_cfg: list[dict] = []
    rules_cfg: list[dict] = []
    has_bond = bool(bond_ip and bond_mac1)

    # ── OOB ──────────────────────────────────────────────────────────────────
    oob: dict = {
        "name": "oob0",
        "type": "ethernet",
        "state": "up",
        "ipv4": {
            "enabled": True,
            "dhcp": False,
            "address": [{"ip": eth0_ip, "prefix-length": prefix_len}],
        },
    }
    if mac:
        oob["identifier"] = "mac-address"
        oob["mac-address"] = mac
    interfaces.append(oob)

    if gateway:
        oob_route: dict = {
            "destination": "0.0.0.0/0",
            "next-hop-address": gateway,
            "next-hop-interface": "oob0",
        }
        if has_bond:
            oob_route["metric"] = 200  # bond is preferred at metric 100
        routes_cfg.append(oob_route)

    # ── CPU / support bond ────────────────────────────────────────────────────
    if has_bond:
        bond_ip_addr, bond_prefix = _parse_cidr(bond_ip)
        vlan_iface = f"bond0.{bond_vlan}" if bond_vlan else None

        for idx, bmac in enumerate([bond_mac1, bond_mac2]):
            if not bmac:
                continue
            interfaces.append({
                "name": f"cpu-bond-{idx}",
                "type": "ethernet",
                "state": "up",
                "identifier": "mac-address",
                "mac-address": bmac,
                "ipv4": {"enabled": False, "dhcp": False},
            })

        bond_members = ["cpu-bond-0"] + (["cpu-bond-1"] if bond_mac2 else [])
        # When bond_vlan is set the IP lives on the VLAN sub-interface; bond0 carries no address.
        interfaces.append({
            "name": "bond0",
            "type": "bond",
            "state": "up",
            "link-aggregation": {"mode": bond_mode, "port": bond_members},
            "ipv4": {"enabled": False, "dhcp": False} if vlan_iface else {
                "enabled": True,
                "dhcp": False,
                "address": [{"ip": bond_ip_addr, "prefix-length": bond_prefix}],
            },
        })

        if vlan_iface:
            interfaces.append({
                "name": vlan_iface,
                "type": "vlan",
                "state": "up",
                "vlan": {"base-iface": "bond0", "id": bond_vlan},
                "ipv4": {
                    "enabled": True,
                    "dhcp": False,
                    "address": [{"ip": bond_ip_addr, "prefix-length": bond_prefix}],
                },
            })

        route_iface = vlan_iface or "bond0"
        if bond_gateway:
            routes_cfg.append({
                "destination": "0.0.0.0/0",
                "next-hop-address": bond_gateway,
                "next-hop-interface": route_iface,
                "metric": 100,
            })

    # ── GPU rails (per-rail PBR) ──────────────────────────────────────────────
    if gpu_ifaces:
        _gpu_macs = gpu_macs or {}
        for i, gi in enumerate(gpu_ifaces):
            topo_iface = gi.get("iface", "")
            rail_mac = _gpu_macs.get(topo_iface, "")
            # Use the 'plane' field (e.g. 'rail1') as the NM connection profile name
            profile = gi.get("plane", f"gpu-rail-{i + 1}")
            rail_ip, rail_prefix = _parse_cidr(gi["ip"])
            table = gi.get("table", 900 + i + 1)

            rail: dict = {
                "name": profile,
                "type": "ethernet",
                "state": "up",
                "ipv4": {
                    "enabled": True,
                    "dhcp": False,
                    "address": [{"ip": rail_ip, "prefix-length": rail_prefix}],
                },
            }
            if rail_mac:
                rail["identifier"] = "mac-address"
                rail["mac-address"] = rail_mac
            interfaces.append(rail)

            gw = gi.get("gateway", "")
            if gw:
                routes_cfg.append({
                    "destination": "0.0.0.0/0",
                    "next-hop-address": gw,
                    "next-hop-interface": profile,
                    "table-id": table,
                })
            rules_cfg.append({
                "ip-from": f"{rail_ip}/32",
                "route-table": table,
                "priority": 100 + i,
            })

    # ── Assemble ──────────────────────────────────────────────────────────────
    config: dict = {"interfaces": interfaces}
    if routes_cfg:
        config["routes"] = {"config": routes_cfg}
    if rules_cfg:
        config["route-rules"] = {"config": rules_cfg}
    return config


def _nm_keyfile_stubs(network_config: dict) -> list[tuple[str, str]]:
    """Generate NM keyfile stubs from NMState config for initrd DHCP prevention.

    Written via ignition to /etc/NetworkManager/system-connections/ so that
    initrd NetworkManager owns non-OOB interfaces immediately (no DHCP attempts),
    letting nm-wait-online-initrd finish in seconds instead of ~28 minutes.

    era-nmstate.service overwrites these stubs with the full config post-pivot.

    Returns list of (filepath, keyfile_content) pairs.
    """
    results: list[tuple[str, str]] = []
    bond_name: str = ""
    bond_mode: str = "active-backup"

    for iface in network_config.get("interfaces", []):
        if iface.get("type") == "bond":
            bond_name = iface["name"]
            bond_mode = iface.get("link-aggregation", {}).get("mode", "active-backup")
            break

    for iface in network_config.get("interfaces", []):
        name = iface["name"]
        itype = iface.get("type", "")
        mac = iface.get("mac-address", "")

        if name == "oob0":
            continue

        if itype == "ethernet":
            ipv4 = iface.get("ipv4", {})
            is_bond_member = not ipv4.get("enabled", True)
            lines = ["[connection]", f"id={name}", "type=ethernet"]
            if is_bond_member and bond_name:
                lines += [f"slave-type=bond", f"master={bond_name}"]
            if mac:
                lines += ["", "[ethernet]", f"mac-address={mac}"]
            lines += ["", "[ipv4]", "method=disabled", "", "[ipv6]", "method=disabled"]

        elif itype == "bond":
            ipv4 = iface.get("ipv4", {})
            addrs = ipv4.get("address", [])
            lines = ["[connection]", f"id={name}", "type=bond", "", "[bond]", f"mode={bond_mode}"]
            if addrs:
                a = addrs[0]
                lines += ["", "[ipv4]", "method=manual", f"address1={a['ip']}/{a['prefix-length']}"]
            else:
                lines += ["", "[ipv4]", "method=disabled"]
            lines += ["", "[ipv6]", "method=disabled"]

        elif itype == "vlan":
            ipv4 = iface.get("ipv4", {})
            addrs = ipv4.get("address", [])
            vlan_cfg = iface.get("vlan", {})
            vlan_id = vlan_cfg.get("id", 0)
            parent = vlan_cfg.get("base-iface", "bond0")
            lines = [
                "[connection]", f"id={name}", "type=vlan",
                "", "[vlan]", f"id={vlan_id}", f"parent={parent}",
            ]
            if addrs:
                a = addrs[0]
                lines += ["", "[ipv4]", "method=manual",
                          f"address1={a['ip']}/{a['prefix-length']}"]
            else:
                lines += ["", "[ipv4]", "method=disabled"]
            lines += ["", "[ipv6]", "method=disabled"]

        else:
            continue

        content = "\n".join(lines) + "\n"
        filepath = f"/etc/NetworkManager/system-connections/{name}.nmconnection"
        results.append((filepath, content))

    return results


def generate_rhcos_ignition_payload(
    node_name: str,
    network_config: dict,
    ssh_key: str | None = None,
) -> str:
    """Generate Ignition v3.4.0 JSON payload for RHCOS OpenStack qcow2 images.

    Configures:
      1. /etc/hostname
      2. /etc/nmstate/network-config.yml  (NMState applied by era-nmstate.service)
      3. NM keyfile stubs for non-OOB interfaces (prevent initrd DHCP wait)
      4. era-nmstate.service (oneshot, applies NMState on first boot)
      5. serial-getty@ttyS0.service (serial console in NVIDIA Air GUI)
      6. passwd.users core sshAuthorizedKeys (if ssh_key provided)
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

    storage_files: list[dict] = [
        {
            "overwrite": True,
            "path": "/etc/hostname",
            "mode": 420,  # 0o644
            "contents": {"source": _data_url(f"{node_name}\n")},
        },
        {
            "overwrite": True,
            "path": "/etc/nmstate/network-config.yml",
            "mode": 420,  # 0o644
            "contents": {"source": _data_url(nmstate_yaml)},
        },
    ]

    # NM keyfile stubs: written to real root by ignition so initrd NM owns every
    # non-OOB interface immediately, preventing ~28-min DHCP timeout on bond
    # members and GPU rail interfaces. era-nmstate.service overwrites post-pivot.
    # NM requires 0o600 (mode 384) on .nmconnection files.
    for nm_path, nm_content in _nm_keyfile_stubs(network_config):
        storage_files.append({
            "overwrite": True,
            "path": nm_path,
            "mode": 384,  # 0o600 — required by NetworkManager
            "contents": {"source": _data_url(nm_content)},
        })

    ignition_cfg: dict = {
        "ignition": {"version": "3.4.0"},
        "kernelArguments": {"shouldExist": ["console=ttyS0,115200n8"]},
        "storage": {"files": storage_files},
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
