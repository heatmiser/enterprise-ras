#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""
Generate OCP-flavored Ansible inventory from ERA net-configurator output.

Reads the ERA inventory produced by `make generate` and writes an OCP-grouped
Ansible inventory + Agent-Based Installer manifests under output/<arch>/<site>/ocp/.

Node role resolution (priority order):
  1. ocp-settings.yml node_roles  (explicit, required for production)
  2. Auto-inferred from hostname prefix or mid-name role indicator (fallback — see warning below):
       k8s-* / *-k8s-*   -> control_plane
       su-*-node-* / gpu-* / *-gpu-* -> worker_gpu
       storage-*          -> worker_storage
       support-*          -> infra  (WARNING: support nodes may serve as control_plane;
                                     use ocp-settings.yml to assign the split explicitly)

GPU node networking — Stage 1 / Stage 2 split:
  Stage 1 (ABI): agent-config.yaml networkConfig contains bond0 (CPU/N-S) only.
                 GPU rail interfaces are excluded — RHCOS during bootstrap may lack
                 drivers for the B3140 400G NICs, and E/W misconfiguration can
                 disrupt cluster formation.
  Stage 2 (Day-2): Apply ocp/day2/nncp-*-gpu-rails.yaml after the NVIDIA Network
                   Operator is installed and healthy.  These NodeNetworkConfigurationPolicy
                   CRs configure per-rail interfaces with correct PBR routing rules
                   (one routing table per rail, ip-from rule per NIC).

Writes:
  output/<arch>/<site>/ocp/
  ├── inventory/
  │   ├── hosts.yml                     OCP-grouped YAML inventory (ansible_host per node)
  │   ├── host_vars/<node>.yml          NMState networkConfig (Stage 1 / bond0 only for GPU nodes)
  │   └── group_vars/all/ocp.yml        OCP cluster vars for Ansible roles
  ├── agent-config.yaml                 ABI per-node config (if ocp-settings.yml present)
  ├── install-config.yaml               ABI cluster manifest (if pull_secret readable)
  ├── inspection/
  │   └── nmstate/<candidate>.yaml      IPA callback-only NMState for one inspection candidate
  │   └── early-network/<candidate>.yaml CPU NIC identities for pre-rootfs dracut networking
  └── day2/
      └── nncp-<node>-gpu-rails.yaml    Stage-2 NodeNetworkConfigurationPolicy CRs
                                        (GPU rail interfaces + PBR; apply after NMState Operator)

Usage:
    python3 scripts/generate-ocp-inventory.py --arch 2-8-5-200 [--site default]
    python3 scripts/generate-ocp-inventory.py --arch 2-8-5-200 --site kicktires \\
        --ocp-settings input/2-8-5-200/kicktires/ocp-settings.yml
    python3 scripts/generate-ocp-inventory.py --arch 2-8-5-200 --site rhaifn01 \\
        --inspection-candidate ipp5-285-rh-k8s-01
"""

import argparse
import ipaddress
import os
import re
import sys
from pathlib import Path

import yaml

try:
    from airlib.env import _load_shared_air_vault
except ImportError:
    def _load_shared_air_vault(*_args, **_kwargs):
        return {}

# ---------------------------------------------------------------------------
# Architecture-level GPU boot disk defaults.
# OEM-specific control_plane/infra/storage disks come from ocp-settings.yml.
# See: era-ocp-configurator/vars/arch-oem-disk-defaults.yml for the full table.
# ---------------------------------------------------------------------------
GPU_DISK_DEFAULTS = {
    "2-4-3-200":    "/dev/nvme0n1",
    "2-4-5-800":    "/dev/nvme2n1",  # GB200 NVL72: E1.S cache drives enumerate first
    "2-8-5-200":    "/dev/nvme0n1",
    "2-8-9-400":    "/dev/nvme0n1",  # UNVERIFIED — OEM-dependent
    "2-8-9-400-SP": "/dev/nvme0n1",  # UNVERIFIED
    "2-8-9-800":    "/dev/nvme2n1",  # GB200 NVL72: same as 2-4-5-800
}

FALLBACK_DISK = "/dev/sda"

# Routing table IDs assigned per GPU rail for PBR (matches VLAN IDs 901-904).
GPU_RAIL_TABLE_BASE = 900

OCP_ROLES = ("control_plane", "infra", "worker_gpu", "worker_storage")

_KVM_PROFILE_ORDER = ("oob", "cpu", "gpu", "support", "storage")


def _kvm_offset_for_profile(nic_map, profile_key):
    """Return starting ethN index for profile_key based on canonical profile order."""
    offset = 0
    for key in _KVM_PROFILE_ORDER:
        if key == profile_key:
            break
        offset += len(nic_map.get(key, []))
    return offset


INSTALLER_ROLE = {
    "control_plane": "master",
    "infra":         "worker",
    "worker_gpu":    "worker",
    "worker_storage": "worker",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_yaml(path):
    with open(path) as f:
        return yaml.safe_load(f) or {}


def write_yaml(path, data, header=None):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        if header:
            f.write(header + "\n")
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)


def _parse_cidr(cidr):
    """Return (ip_str, prefix_int) from '172.16.178.201/24' or bare IP '10.78.220.141'."""
    if not cidr:
        return "", None
    if "/" in str(cidr):
        parts = str(cidr).split("/", 1)
        return parts[0], int(parts[1])
    return str(cidr), None


def infer_ocp_role(hostname):
    """Auto-infer OCP role from ERA hostname prefix or mid-name role indicator."""
    if hostname.startswith("k8s-") or "-k8s-" in hostname:
        return "control_plane"
    if re.match(r"su-\d+-node-", hostname) or hostname.startswith("gpu-") or "-gpu-" in hostname:
        return "worker_gpu"
    if hostname.startswith("storage-"):
        return "worker_storage"
    if hostname.startswith("support-"):
        return "infra"
    return None


def resolve_disk(hostname, ocp_role, arch, overrides):
    """Four-step disk resolution: override → gpu-default → arch-fallback → /dev/sda.

    Returns (device_path, used_fallback).  Callers should aggregate used_fallback
    and emit a single summary warning rather than one warning per node.
    """
    if hostname in overrides:
        return overrides[hostname], False
    if ocp_role == "worker_gpu":
        dev = GPU_DISK_DEFAULTS.get(arch)
        if dev:
            return dev, False
    return FALLBACK_DISK, True


# ---------------------------------------------------------------------------
# NMState network config (mirrors era-ocp-configurator build_nmstate_network_config)
# ---------------------------------------------------------------------------

def build_nmstate_network_config(device_data, site_vars, ocp_role, nic_mode="real-hw"):
    """Return nmstate networkConfig dict for a node."""
    common     = site_vars.get("common", {})
    ifaces_map = device_data.get("interfaces", {})
    nic_map    = device_data.get("nic_map", {})
    interfaces = []
    routes     = []
    rules      = []

    def _bond_members(profile_key):
        """Return bond member NIC names, handling real-hw vs kvm mode."""
        if nic_map and nic_mode == "real-hw":
            entries = nic_map.get(profile_key, [])
            if entries:
                return [e["kernel"] for e in entries]
        # KVM/Air: use topology-derived interface names (ifaces_map) directly.
        # _kvm_offset_for_profile uses real-hw nic_map counts which can differ
        # from the number of ports the topology actually wires up per profile.
        return ifaces_map.get(profile_key, [])

    def _bond(members, cidr, gateway):
        if not (cidr and members):
            return
        ip, prefix = _parse_cidr(cidr)
        interfaces.append({
            "name": "bond0",
            "type": "bond",
            "state": "up",
            "ipv4": {
                "enabled": True,
                "dhcp": False,
                "address": [{"ip": ip, "prefix-length": prefix}],
            },
            "link-aggregation": {"mode": "802.3ad", "port": list(members)},
        })
        if gateway:
            routes.append({
                "destination": "0.0.0.0/0",
                "next-hop-address": gateway,
                "next-hop-interface": "bond0",
            })

    # Add eth0 OOB management interface if eth0_ip is defined
    eth0_ip = device_data.get("eth0_ip")
    if eth0_ip:
        ip, prefix = _parse_cidr(eth0_ip)
        if not prefix:
            oob_net = common.get("oob_network", "")
            _, prefix = _parse_cidr(oob_net) if oob_net else ("", 24)
            prefix = prefix or 24
        interfaces.append({
            "name": "eth0",
            "type": "ethernet",
            "state": "up",
            "ipv4": {
                "enabled": True,
                "dhcp": False,
                "address": [{"ip": ip, "prefix-length": prefix}],
            },
        })

    if ocp_role in ("control_plane", "infra"):
        members = _bond_members("cpu") or _bond_members("support")
        cidr    = device_data.get("bond_ip") or device_data.get("bond_ip1")
        gw      = common.get("cpu_gateway") or common.get("support_gateway")
        _bond(members, cidr, gw)

    elif ocp_role == "worker_gpu":
        _bond(_bond_members("cpu"),
              device_data.get("bond_ip"),
              common.get("cpu_gateway"))
        # GPU rail interfaces are intentionally excluded from ABI networkConfig;
        # applied post-install via NNCP CRs in ocp/day2/.

    elif ocp_role == "worker_storage":
        members = _bond_members("storage")
        cidr    = device_data.get("bond_ip1") or device_data.get("bond_ip")
        gw      = common.get("storage_gateway") or common.get("cpu_gateway")
        _bond(members, cidr, gw)

    config = {}
    if interfaces:
        config["interfaces"] = interfaces
    if routes:
        config["routes"] = {"config": routes}
    if rules:
        config["route-rules"] = {"config": rules}
    return config


def build_inspection_nmstate_network_config(device_data, site_vars, nic_mode="real-hw"):
    """Return CPU-bond-only NMState for IPA callback connectivity.

    This deliberately excludes OOB, GPU rails, ABI-specific settings, and
    Day-2 network state. The static IPA callback uses the site CPU network
    through ``bond0``.
    """
    common = site_vars.get("common", {})
    ifaces_map = device_data.get("interfaces", {})
    nic_map = device_data.get("nic_map", {})

    if nic_map and nic_mode == "real-hw":
        cpu_entries = nic_map.get("cpu", [])
        members = [entry.get("kernel") for entry in cpu_entries if entry.get("kernel")]
    else:
        members = list(ifaces_map.get("cpu", []))

    cidr = device_data.get("bond_ip")
    gateway = common.get("cpu_gateway")
    dns_servers = site_vars.get("dns_servers", [])
    if not members:
        raise ValueError("candidate has no CPU bond members")
    if not cidr:
        raise ValueError("candidate has no CPU bond address")

    if not isinstance(dns_servers, list) or not dns_servers:
        raise ValueError("site dns_servers must provide at least one IPv4 resolver")
    normalized_dns_servers = []
    for dns_server in dns_servers:
        if not isinstance(dns_server, str):
            raise ValueError("site dns_servers must be an ordered list of IPv4 resolvers")
        try:
            parsed_dns_server = ipaddress.ip_address(dns_server)
        except ValueError as exc:
            raise ValueError(
                f"site dns_servers contains invalid IP address {dns_server!r}"
            ) from exc
        if parsed_dns_server.version != 4 or str(parsed_dns_server) != dns_server:
            raise ValueError("site dns_servers must contain canonical IPv4 resolvers")
        if dns_server in normalized_dns_servers:
            raise ValueError(
                f"site dns_servers contains duplicate address {dns_server!r}"
            )
        normalized_dns_servers.append(dns_server)

    ip, prefix = _parse_cidr(cidr)
    if not ip or prefix is None:
        raise ValueError("candidate CPU bond address is not a valid CIDR")

    config = {
        "interfaces": [{
            "name": "bond0",
            "type": "bond",
            "state": "up",
            "ipv4": {
                "enabled": True,
                "dhcp": False,
                "address": [{"ip": ip, "prefix-length": prefix}],
            },
            "link-aggregation": {"mode": "802.3ad", "port": members},
        }],
    }
    if gateway:
        config["routes"] = {
            "config": [{
                "destination": "0.0.0.0/0",
                "next-hop-address": gateway,
                "next-hop-interface": "bond0",
            }],
        }
    config["dns-resolver"] = {"config": {"server": normalized_dns_servers}}
    return config


def build_inspection_early_network_interfaces(device_data, inspection_nmstate, nic_mode="real-hw"):
    """Return ordered CPU NIC name/MAC identities for dracut ``ifname=``.

    The inspection NMState document supplies the desired bond member names,
    but it is applied only after the live rootfs is fetched.  Dracut must bind
    those names to physical NICs first, so retain the matching CPU MACs from
    the workbook-derived ``nic_map`` in a separate, reviewable artifact.
    """
    if nic_mode != "real-hw":
        raise ValueError("MAC-pinned inspection networking requires nic_mode=real-hw")

    interfaces = inspection_nmstate.get("interfaces", [])
    bonds = [interface for interface in interfaces if interface.get("name") == "bond0"]
    if len(bonds) != 1:
        raise ValueError("inspection NMState must contain exactly one bond0 interface")

    members = bonds[0].get("link-aggregation", {}).get("port", [])
    cpu_entries = device_data.get("nic_map", {}).get("cpu", [])
    mac_by_kernel = {
        entry.get("kernel"): (entry.get("mac") or "").lower()
        for entry in cpu_entries
        if entry.get("kernel")
    }
    mac_pattern = re.compile(r"^[0-9a-f]{2}(?::[0-9a-f]{2}){5}$")
    result = []
    for member in members:
        mac = mac_by_kernel.get(member, "")
        if not mac_pattern.fullmatch(mac):
            raise ValueError(
                f"CPU bond member {member!r} lacks a valid workbook NIC MAC"
            )
        result.append({"name": member, "mac": mac})

    if len(result) < 2 or len({entry["name"] for entry in result}) != len(result):
        raise ValueError("inspection CPU bond members must be unique and contain at least two NICs")
    if len({entry["mac"] for entry in result}) != len(result):
        raise ValueError("inspection CPU bond member MAC addresses must be unique")
    return result


# ---------------------------------------------------------------------------
# Stage-2 GPU rail config (NodeNetworkConfigurationPolicy)
# ---------------------------------------------------------------------------

def _build_gpu_rail_desiredstate(device_data, site_vars, nic_mode="real-hw"):
    """Return nmstate desiredState dict for GPU rail interfaces only.

    Handles both the structured gpu_interfaces list (explicit gateway + table per
    NIC) and the flat gpu_ips fallback (assigns routing tables 901-904 sequentially
    to match the per_rail VLAN numbering).  Returns None when no GPU data is present.
    """
    common          = site_vars.get("common", {})
    ifaces_map      = device_data.get("interfaces", {})
    nic_map         = device_data.get("nic_map", {})
    gpu_ifaces_list = device_data.get("gpu_interfaces")
    gpu_ips_list    = device_data.get("gpu_ips")
    gpu_nic_names   = ifaces_map.get("gpu", [])

    # Kernel NIC names for GPU rails (from Wire Map col K), ordered by Wire Map row.
    if nic_mode == "kvm" and nic_map:
        gpu_offset = _kvm_offset_for_profile(nic_map, "gpu")
        gpu_count = len(gpu_ifaces_list) if gpu_ifaces_list else len(nic_map.get("gpu", []))
        gpu_kernel_names = [f"eth{gpu_offset + i}" for i in range(gpu_count)]
    else:
        gpu_kernel_names = [e["kernel"] for e in nic_map.get("gpu", []) if e.get("kernel")]

    interfaces, routes, rules = [], [], []

    if gpu_ifaces_list:
        for i, gi in enumerate(gpu_ifaces_list):
            gi = dict(gi)
            if i < len(gpu_kernel_names):
                gi["iface"] = gpu_kernel_names[i]
            nic_ip, nic_prefix = _parse_cidr(gi["ip"])
            interfaces.append({
                "name": gi["iface"],
                "type": "ethernet",
                "state": "up",
                "ipv4": {
                    "enabled": True,
                    "dhcp": False,
                    "address": [{"ip": nic_ip, "prefix-length": nic_prefix}],
                },
            })
            routes.append({
                "destination": "0.0.0.0/0",
                "next-hop-address": gi["gateway"],
                "next-hop-interface": gi["iface"],
                "table-id": gi["table"],
            })
            rules.append({
                "ip-from": f"{nic_ip}/32",
                "route-table": gi["table"],
                "priority": 100,
            })

    elif gpu_ips_list and gpu_nic_names:
        gw = common.get("gpu_gateway") or common.get("cpu_gateway")
        for i, (nic_name, nic_cidr) in enumerate(zip(gpu_nic_names, gpu_ips_list)):
            table_id = GPU_RAIL_TABLE_BASE + i + 1  # 901, 902, 903, 904
            nic_ip, nic_prefix = _parse_cidr(nic_cidr)
            interfaces.append({
                "name": nic_name,
                "type": "ethernet",
                "state": "up",
                "ipv4": {
                    "enabled": True,
                    "dhcp": False,
                    "address": [{"ip": nic_ip, "prefix-length": nic_prefix}],
                },
            })
            if gw:
                routes.append({
                    "destination": "0.0.0.0/0",
                    "next-hop-address": gw,
                    "next-hop-interface": nic_name,
                    "table-id": table_id,
                })
            rules.append({
                "ip-from": f"{nic_ip}/32",
                "route-table": table_id,
                "priority": 100 + i,
            })

    else:
        return None

    state = {}
    if interfaces:
        state["interfaces"] = interfaces
    if routes:
        state["routes"] = {"config": routes}
    if rules:
        state["route-rules"] = {"config": rules}
    return state or None


def build_gpu_nncp(hostname, device_data, site_vars, nic_mode="real-hw"):
    """Return a NodeNetworkConfigurationPolicy CR dict for GPU rail interfaces.

    Returns None when the node has no GPU rail data (not a GPU node or data absent).
    """
    desired_state = _build_gpu_rail_desiredstate(device_data, site_vars, nic_mode=nic_mode)
    if desired_state is None:
        return None
    return {
        "apiVersion": "nmstate.io/v1",
        "kind":       "NodeNetworkConfigurationPolicy",
        "metadata":   {"name": f"{hostname}-gpu-rails"},
        "spec": {
            "nodeSelector": {"kubernetes.io/hostname": hostname},
            "desiredState": desired_state,
        },
    }


# ---------------------------------------------------------------------------
# Output builders
# ---------------------------------------------------------------------------

def build_role_map(ocp_settings, devices, arch, site):
    """
    Return {hostname: ocp_role} for all OCP-managed nodes.

    Uses ocp_settings['node_roles'] if present; otherwise auto-infers from
    hostname prefix.  Warns when auto-inference produces no control_plane
    nodes (common for architectures without k8s-* hosts).
    """
    if ocp_settings and "node_roles" in ocp_settings:
        role_map = {}
        for ocp_role, hostnames in ocp_settings["node_roles"].items():
            for h in hostnames:
                role_map[h] = ocp_role
        return role_map

    # Auto-inference fallback
    print("  NOTE: no ocp-settings.yml node_roles found — auto-inferring OCP roles from hostnames.")
    role_map = {}
    for hostname in sorted(devices.keys()):
        role = infer_ocp_role(hostname)
        if role:
            role_map[hostname] = role

    if not any(r == "control_plane" for r in role_map.values()):
        print(
            "\n  WARNING: no control_plane nodes found via auto-inference.\n"
            "  This architecture has no k8s-* hosts.  To designate control plane nodes,\n"
            "  create input/{arch}/{site}/ocp-settings.yml with explicit node_roles.\n"
            "  See: era-ocp-configurator/input/ocp-settings.yml for the schema.\n".format(arch=arch, site=site),
            file=sys.stderr,
        )

    return role_map


def build_hosts_yaml(role_map, era_host_vars):
    """Build OCP-grouped YAML inventory dict with ansible_host per node."""
    groups = {role: {} for role in OCP_ROLES}
    for hostname, ocp_role in sorted(role_map.items()):
        if ocp_role not in groups:
            continue
        ansible_host = era_host_vars.get(hostname, {}).get("ansible_host", "")
        groups[ocp_role][hostname] = {"ansible_host": ansible_host} if ansible_host else {}

    return {
        "all": {
            "children": {
                f"ocp_{role}": {"hosts": hosts}
                for role, hosts in groups.items()
                if hosts
            }
        }
    }


def build_ocp_group_vars(ocp_settings, site_vars, ocp_out_dir, vault=None):
    """Build group_vars/all/ocp.yml content dict."""
    common = site_vars.get("common", {})
    vault = vault or {}
    # air_ssh_key_path set by `make air-setup`; fallback matches airlib.env default
    vault_ssh_key = vault.get("air_ssh_key_path", "~/.ssh/id_ed25519")
    vault_ssh_key_pub = vault_ssh_key if vault_ssh_key.endswith(".pub") else vault_ssh_key + ".pub"

    if ocp_settings and "cluster" in ocp_settings:
        cluster = ocp_settings["cluster"]
        all_nodes = []
        for hostnames in ocp_settings.get("node_roles", {}).values():
            all_nodes.extend(hostnames)
        return {
            "ocp_cluster_name":       cluster.get("name", ""),
            "ocp_cluster_domain":     cluster.get("domain", ""),
            "ocp_api_vip":            cluster.get("api_vip", ""),
            "ocp_ingress_vip":        cluster.get("ingress_vip", ""),
            "ocp_machine_network":    common.get("cpu_network", ""),
            "ocp_pull_secret_path":   ocp_settings.get("pull_secret_path", "~/.era-secrets/pull-secret.json"),
            "ocp_ssh_key_path":       ocp_settings.get("ssh_key_path", vault_ssh_key_pub),
            "ocp_node_hostnames":     all_nodes,
            "ocp_install_output_dir": str(ocp_out_dir),
        }

    # Stub — ocp-settings.yml not present
    return {
        "ocp_cluster_name":       "",
        "ocp_cluster_domain":     "",
        "ocp_api_vip":            "",
        "ocp_ingress_vip":        "",
        "ocp_machine_network":    common.get("cpu_network", ""),
        "ocp_pull_secret_path":   "~/.era-secrets/pull-secret.json",
        "ocp_ssh_key_path":       vault_ssh_key_pub,
        "ocp_node_hostnames":     [],
        "ocp_install_output_dir": str(ocp_out_dir),
    }


def build_agent_config(ocp_settings, role_map, era_host_vars, site_vars, arch, nic_mode="real-hw"):
    """Render agent-config.yaml dict."""
    devices  = site_vars.get("devices", {})
    overrides = {}
    cluster_name = "ocp-era"
    oem = "dell"

    if ocp_settings:
        cluster_name = ocp_settings.get("cluster", {}).get("name", cluster_name)
        oem          = ocp_settings.get("oem", oem)
        overrides    = ocp_settings.get("install_disk", {}).get("overrides") or {}

    # rendezvous IP: first control_plane node's bond IP
    rendezvous_ip = None
    for hostname, ocp_role in role_map.items():
        if ocp_role == "control_plane":
            dev = devices.get(hostname, {})
            cidr = dev.get("bond_ip1") or dev.get("bond_ip")
            if cidr:
                rendezvous_ip, _ = _parse_cidr(cidr)
                break

    agent_hosts = []
    for hostname in sorted(role_map):
        ocp_role    = role_map[hostname]
        device_data = devices.get(hostname)
        if device_data is None:
            print(f"  WARNING: {hostname} not in devices block — skipping", file=sys.stderr)
            continue

        disk, _        = resolve_disk(hostname, ocp_role, arch, overrides)
        mac            = device_data.get("mac")
        network_config = build_nmstate_network_config(device_data, site_vars, ocp_role, nic_mode=nic_mode)

        nic_map = device_data.get("nic_map", {})
        if nic_mode == "kvm":
            if nic_map:
                all_entries = [
                    entry for key in _KVM_PROFILE_ORDER
                    for entry in nic_map.get(key, [])
                    if entry.get("kernel")
                ]
                hw_interfaces = [
                    {"name": f"eth{i}", "macAddress": entry.get("mac", "")}
                    for i, entry in enumerate(all_entries)
                ]
            else:
                hw_interfaces = [{"name": "eth0", "macAddress": mac}] if mac else []
        elif nic_map:
            hw_interfaces = [
                {"name": entry["kernel"], "macAddress": entry["mac"]}
                for entries in nic_map.values()
                for entry in entries
                if entry.get("kernel") and entry.get("mac")
            ]
        else:
            hw_interfaces = [{"name": "eth0", "macAddress": mac}] if mac else []

        agent_hosts.append({
            "hostname":        hostname,
            "role":            INSTALLER_ROLE[ocp_role],
            "rootDeviceHints": {"deviceName": disk},
            "interfaces":      hw_interfaces,
            "networkConfig":   network_config,
        })

    return {
        "apiVersion": "v1alpha1",
        "kind":       "AgentConfig",
        "metadata":   {"name": cluster_name},
        "rendezvousIP": rendezvous_ip,
        "hosts":      agent_hosts,
    }


def read_pull_secret(ocp_settings, vault=None):
    """Return the OCP pull secret string.

    Precedence:
      1. ``ocp_pull_secret`` field in .era-secrets/air-secrets.yml (vault)
      2. File at ``pull_secret_path`` from ocp-settings.yml

    Returns (pull_secret_str, source_description) or raises OSError/KeyError.
    """
    vault = vault or {}
    if vault.get("ocp_pull_secret"):
        return vault["ocp_pull_secret"].strip(), "vault (.era-secrets/air-secrets.yml)"
    path = os.path.expanduser(ocp_settings.get("pull_secret_path", ""))
    if not path:
        raise FileNotFoundError("pull_secret_path not set in ocp-settings.yml")
    content = open(path).read().strip()
    return content, f"file ({path})"


def build_install_config(ocp_settings, site_vars, role_map, vault=None):
    """Render install-config.yaml dict.  Returns None if pull_secret unreadable."""
    cluster = ocp_settings.get("cluster", {})
    common  = site_vars.get("common", {})

    try:
        pull_secret, ps_source = read_pull_secret(ocp_settings, vault)
        print(f"  pull secret: {ps_source}", file=sys.stderr)
    except (FileNotFoundError, OSError) as exc:
        print(f"  NOTE: pull secret not readable ({exc}) — skipping install-config.yaml generation",
              file=sys.stderr)
        return None

    ssh_key_path = os.path.expanduser(ocp_settings.get("ssh_key_path", ""))

    try:
        ssh_key = open(ssh_key_path).read().strip()
    except (FileNotFoundError, OSError) as exc:
        print(f"  NOTE: SSH key not readable ({exc}) — skipping install-config.yaml generation",
              file=sys.stderr)
        return None

    cp_count = sum(1 for r in role_map.values() if r == "control_plane")
    worker_count = sum(1 for r in role_map.values() if r != "control_plane")
    cpu_network  = common.get("cpu_network", "")

    return {
        "apiVersion": "v1",
        "baseDomain": cluster.get("domain", ""),
        "metadata":   {"name": cluster.get("name", "")},
        "compute": [{"hyperthreading": "Enabled", "name": "worker", "replicas": worker_count}],
        "controlPlane": {"hyperthreading": "Enabled", "name": "master", "replicas": cp_count},
        "networking": {
            "networkType": "OVNKubernetes",
            "clusterNetwork": [{"cidr": "10.128.0.0/14", "hostPrefix": 23}],
            "serviceNetwork": ["172.30.0.0/16"],
            "machineNetwork": [{"cidr": cpu_network}],
        },
        "platform": {
            "baremetal": {
                "apiVIPs":     [cluster.get("api_vip", "")],
                "ingressVIPs": [cluster.get("ingress_vip", "")],
            }
        },
        "pullSecret": pull_secret,
        "sshKey":     ssh_key,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--arch",  required=True, help="Architecture (e.g. 2-8-5-200)")
    parser.add_argument("--site",  default="default", help="Site name (default: default)")
    parser.add_argument("--ocp-settings", default=None,
                        help="Path to ocp-settings.yml (default: input/<arch>/<site>/ocp-settings.yml)")
    parser.add_argument(
        "--nic-mode",
        choices=("real-hw", "kvm"),
        default="real-hw",
        help="NIC naming mode: real-hw uses Wire Map K/L kernel names; "
             "kvm uses eth0/eth1/ethN (virtio) for KVM and Air simulations.",
    )
    parser.add_argument(
        "--inspection-candidate",
        default=None,
        help="Render only the candidate's minimal IPA callback NMState artifact; "
             "does not generate ABI manifests or modify other OCP output.",
    )
    args = parser.parse_args()

    base_dir   = Path("output") / args.arch / args.site
    inv_dir    = base_dir / "inventory"
    ocp_dir    = base_dir / "ocp"

    if not inv_dir.is_dir():
        sys.exit(
            f"ERROR: ERA inventory not found at {inv_dir}\n"
            f"  Run 'make generate ARCH={args.arch} SITE={args.site}' first."
        )

    # Load ERA inventory
    site_vars = load_yaml(inv_dir / "group_vars" / "all" / "main.yml")
    devices   = site_vars.get("devices", {})

    era_host_vars = {}
    hv_dir = inv_dir / "host_vars"
    if hv_dir.is_dir():
        for f in hv_dir.iterdir():
            if f.suffix in (".yml", ".yaml"):
                era_host_vars[f.stem] = load_yaml(f)

    if args.inspection_candidate:
        candidate = args.inspection_candidate
        device_data = devices.get(candidate)
        if device_data is None:
            sys.exit(
                f"ERROR: inspection candidate {candidate!r} is not present in "
                f"{inv_dir / 'group_vars/all/main.yml'}"
            )
        try:
            inspection_nmstate = build_inspection_nmstate_network_config(
                device_data, site_vars, nic_mode=args.nic_mode
            )
            inspection_early_network = build_inspection_early_network_interfaces(
                device_data, inspection_nmstate, nic_mode=args.nic_mode
            )
        except ValueError as exc:
            sys.exit(f"ERROR: inspection candidate {candidate!r}: {exc}")

        inspection_path = ocp_dir / "inspection" / "nmstate" / f"{candidate}.yaml"
        write_yaml(
            inspection_path,
            inspection_nmstate,
            header=(
                "---\n"
                "# Generated IPA callback-only inspection NMState — do not edit manually.\n"
                "# CPU bond only; excludes OOB, GPU rails, ABI, and Day-2 network state."
            ),
        )
        early_network_path = ocp_dir / "inspection" / "early-network" / f"{candidate}.yaml"
        write_yaml(
            early_network_path,
            {"interfaces": inspection_early_network},
            header=(
                "---\n"
                "# Generated MAC-pinned dracut interface identities — do not edit manually.\n"
                "# Derived from the candidate CPU nic_map and inspection NMState bond members."
            ),
        )
        print(f"✓ {inspection_path}")
        print(f"✓ {early_network_path}")
        print("✅ Inspection networking inputs written; no ABI manifests were generated.")
        return

    # Load ocp-settings.yml (optional)
    settings_path = args.ocp_settings or f"input/{args.arch}/{args.site}/ocp-settings.yml"
    ocp_settings  = None
    if os.path.isfile(settings_path):
        ocp_settings = load_yaml(settings_path)
        print(f"  Using ocp-settings.yml: {settings_path}")
    else:
        print(f"  No ocp-settings.yml found at {settings_path} — using auto-inference")

    # Load shared vault for credentials (ocp_pull_secret takes precedence over pull_secret_path)
    vault = _load_shared_air_vault()

    print(f"Generating OCP inventory for {args.arch} (site: {args.site})")

    role_map = build_role_map(ocp_settings, devices, args.arch, args.site)
    if not role_map:
        sys.exit("ERROR: no OCP nodes found in ERA inventory (devices block is empty or unrecognized)")

    # Summarize role assignment
    for role in OCP_ROLES:
        nodes = [h for h, r in sorted(role_map.items()) if r == role]
        if nodes:
            print(f"  {role:16s}: {', '.join(nodes)}")

    # ── OCP Ansible inventory ─────────────────────────────────────────────
    hosts_yaml = build_hosts_yaml(role_map, era_host_vars)
    hosts_path = ocp_dir / "inventory" / "hosts.yml"
    write_yaml(hosts_path, hosts_yaml,
               header="---\n# Generated by net-configurator generate-ocp-inventory.py — do not edit manually.")
    print(f"\n  ✓ {hosts_path}")

    # ── Per-node host_vars ────────────────────────────────────────────────
    overrides = {}
    oem = "dell"
    if ocp_settings:
        overrides = ocp_settings.get("install_disk", {}).get("overrides") or {}
        oem       = ocp_settings.get("oem", oem)

    fallback_disk_roles = set()
    for hostname, ocp_role in sorted(role_map.items()):
        device_data = devices.get(hostname)
        if device_data is None:
            print(f"  WARNING: {hostname} not in devices block — skipping host_vars", file=sys.stderr)
            continue

        ansible_host = era_host_vars.get(hostname, {}).get("ansible_host", "")
        network_config = build_nmstate_network_config(device_data, site_vars, ocp_role, nic_mode=args.nic_mode)
        disk, used_fallback = resolve_disk(hostname, ocp_role, args.arch, overrides)
        if used_fallback:
            fallback_disk_roles.add(ocp_role)

        hv_content = {
            "ansible_host": ansible_host,
            "ocp_role":     ocp_role,
            "oob_mac":      device_data.get("mac", ""),
            "install_disk": disk,
            "networkConfig": network_config,
        }
        hv_path = ocp_dir / "inventory" / "host_vars" / f"{hostname}.yml"
        write_yaml(hv_path, hv_content,
                   header=f"---\n# Generated by net-configurator — {hostname} OCP node vars")
        print(f"  ✓ {hv_path}")

    if fallback_disk_roles:
        roles_str = ", ".join(sorted(fallback_disk_roles))
        print(
            f"\n  NOTE: install_disk defaulted to {FALLBACK_DISK} for roles: {roles_str}\n"
            f"  Set ocp-settings.yml install_disk.overrides or add OEM disk defaults.",
            file=sys.stderr,
        )

    # ── group_vars ────────────────────────────────────────────────────────
    gv_content = build_ocp_group_vars(ocp_settings, site_vars, str(ocp_dir), vault=vault)
    gv_path = ocp_dir / "inventory" / "group_vars" / "all" / "ocp.yml"
    write_yaml(gv_path, gv_content,
               header="---\n# Generated by net-configurator generate-ocp-inventory.py — do not edit manually.")
    print(f"  ✓ {gv_path}")

    # ── ABI manifests (require ocp-settings.yml) ──────────────────────────
    if ocp_settings:
        agent_cfg  = build_agent_config(ocp_settings, role_map, era_host_vars, site_vars, args.arch, nic_mode=args.nic_mode)
        ac_path    = ocp_dir / "agent-config.yaml"
        write_yaml(ac_path, agent_cfg)
        print(f"  ✓ {ac_path}")

        install_cfg = build_install_config(ocp_settings, site_vars, role_map, vault=vault)
        if install_cfg:
            ic_path = ocp_dir / "install-config.yaml"
            write_yaml(ic_path, install_cfg)
            print(f"  ✓ {ic_path}")
    else:
        print(
            "\n  ABI manifests (agent-config.yaml, install-config.yaml) require ocp-settings.yml.\n"
            f"  Create input/{args.arch}/{args.site}/ocp-settings.yml to enable full generation.\n"
            "  See: era-ocp-configurator/input/ocp-settings.yml for the schema."
        )

    # ── Day-2 GPU rail NMState policies ──────────────────────────────────────
    gpu_nodes = sorted(h for h, r in role_map.items() if r == "worker_gpu")
    nncp_count = 0
    for hostname in gpu_nodes:
        device_data = devices.get(hostname)
        if device_data is None:
            continue
        nncp = build_gpu_nncp(hostname, device_data, site_vars, nic_mode=args.nic_mode)
        if nncp:
            nncp_path = ocp_dir / "day2" / f"nncp-{hostname}-gpu-rails.yaml"
            write_yaml(nncp_path, nncp,
                       header=f"---\n# Stage-2 GPU rail NNCP for {hostname}\n"
                              "# Apply after NVIDIA Network Operator is installed and healthy:\n"
                              "#   oc apply -f ocp/day2/")
            print(f"  ✓ {nncp_path}")
            nncp_count += 1

    if nncp_count:
        print(f"\n  Day-2 GPU rail policies ({nncp_count} nodes): {ocp_dir}/day2/")
        print("  Apply post-install: oc apply -f ocp/day2/")
    elif gpu_nodes:
        print(f"\n  NOTE: {len(gpu_nodes)} worker_gpu node(s) found but no GPU rail data in ERA inventory.",
              file=sys.stderr)

    print(f"\n✅ OCP inventory written to {ocp_dir}/")


if __name__ == "__main__":
    main()
