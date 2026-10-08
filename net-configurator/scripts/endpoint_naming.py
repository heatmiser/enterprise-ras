# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""Explicit physical-server keys and DNS endpoint identities."""

import ipaddress
import re
import socket

WORKBOOK_ROLE_TO_OCP = {
    "gpu": "worker_gpu", "storage": "worker_storage", "worker": "worker",
    "control": "control_plane", "infra": "infra",
}


def role_endpoint_names(name, role, cluster_name, domain, bmc_subdomain=None):
    """Construct reviewed Role names using only a stable terminal -digits suffix."""
    if role not in WORKBOOK_ROLE_TO_OCP:
        raise ValueError(f"Invalid Nodes.Role for {name}: {role!r}")
    match = re.search(r"-([0-9]+)$", str(name))
    if not match:
        raise ValueError(f"Nodes.Name requires a terminal -<digits> suffix: {name}")
    if not isinstance(cluster_name, str) or not re.fullmatch(
        r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", cluster_name
    ):
        raise ValueError("ocp_cluster_name must be one DNS label")
    domain = dns_name(domain)
    if bmc_subdomain is not None and not isinstance(bmc_subdomain, str):
        raise ValueError("bmc_dns_subdomain must be a DNS namespace")
    subdomain = (bmc_subdomain or "").strip() or "mgmt-01"
    dns_name(subdomain + "." + domain)
    label = role + "-" + match[1]
    return {
        "bmc_fqdn": dns_name(f"{label}.{subdomain}.{domain}"),
        "rhcos_fqdn": dns_name(f"{label}.{cluster_name}.{domain}", hostname=True),
    }


def build_role_endpoints(roles, cluster_name, domain, bmc_subdomain=None):
    """Require globally unique endpoint names across all reviewed physical keys."""
    mapping, seen = {}, set()
    for name, role in sorted(roles.items()):
        entry = role_endpoint_names(name, role, cluster_name, domain, bmc_subdomain)
        names = set(entry.values())
        if len(names) != 2 or seen & names:
            raise ValueError(f"Duplicate generated endpoint DNS name: {name}")
        mapping[name] = entry
        seen.update(names)
    return mapping


def dns_name(value, *, hostname=False):
    """Require a canonical FQDN; installed Linux hostnames fit in 63 bytes."""
    if not isinstance(value, str) or len(value) > (63 if hostname else 253):
        raise ValueError(f"Invalid DNS name: {value!r}")
    labels = value.split(".")
    if len(labels) < 2 or any(
        not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
        for label in labels
    ):
        raise ValueError(f"Invalid DNS name: {value!r}")
    return value


def endpoint_names(settings, name):
    """Return explicitly supplied names, never derive names from a physical key."""
    mapping = (settings or {}).get("node_endpoints", {})
    if not isinstance(mapping, dict):
        raise ValueError("node_endpoints must be a mapping")
    if not mapping:
        return None
    entry = mapping.get(name)
    if not isinstance(entry, dict) or set(entry) != {"bmc_fqdn", "rhcos_fqdn"}:
        raise ValueError(f"Require bmc_fqdn and rhcos_fqdn for physical node: {name}")
    return {
        "bmc_fqdn": dns_name(entry["bmc_fqdn"]),
        "rhcos_fqdn": dns_name(entry["rhcos_fqdn"], hostname=True),
    }


def ipv4(value):
    """Extract a canonical IPv4 address from an existing workbook-derived CIDR."""
    return str(ipaddress.IPv4Interface(str(value)).ip)


def build_endpoint_manifest(settings, devices, role_map):
    """Validate complete name coverage and attach workbook-derived addresses."""
    mapping = (settings or {}).get("node_endpoints", {})
    if not isinstance(mapping, dict):
        raise ValueError("node_endpoints must be a mapping")
    if not mapping:
        return None
    if set(mapping) != set(role_map):
        raise ValueError("node_endpoints must cover exactly all declared OCP physical nodes")
    cluster = settings.get("cluster", {})
    cluster_name = cluster.get("name", "")
    if not isinstance(cluster_name, str) or not re.fullmatch(
        r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", cluster_name
    ):
        raise ValueError("cluster.name must be one DNS label")
    cluster_domain = dns_name(cluster_name + "." + dns_name(cluster.get("domain")))
    reserved = {f"{label}.{cluster_domain}" for label in ("api", "api-int", "apps")}
    nodes, names, addresses = {}, set(), set()
    for key in sorted(role_map):
        entry = endpoint_names(settings, key)
        if not entry["rhcos_fqdn"].endswith("." + cluster_domain):
            raise ValueError(f"RHCOS FQDN must belong to {cluster_domain}: {key}")
        device = devices.get(key, {})
        bmc_ip = ipv4(device.get("eth0_ip"))
        # Match build_nmstate_network_config's role-specific address selection.
        role = role_map[key]
        if role in ("control_plane", "infra", "worker"):
            cidr = device.get("bond_ip") or device.get("bond_ip1")
        elif role == "worker_gpu":
            cidr = device.get("bond_ip")
        elif role == "worker_storage":
            cidr = device.get("bond_ip1") or device.get("bond_ip")
        else:
            raise ValueError(f"Unsupported endpoint OCP role: {role}")
        rhcos_ip = ipv4(cidr)
        pair = {entry["bmc_fqdn"], entry["rhcos_fqdn"]}
        if len(pair) != 2 or names & pair:
            raise ValueError(f"Duplicate endpoint DNS name: {key}")
        if pair & reserved or any(name.endswith(".apps." + cluster_domain) for name in pair):
            raise ValueError(f"Node DNS name overlaps cluster service records: {key}")
        if bmc_ip == rhcos_ip or addresses & {bmc_ip, rhcos_ip}:
            raise ValueError(f"Duplicate endpoint IP address: {key}")
        names.update(pair)
        addresses.update((bmc_ip, rhcos_ip))
        nodes[key] = {
            "bmc": {"hostname": entry["bmc_fqdn"], "ip": bmc_ip},
            "rhcos": {"hostname": entry["rhcos_fqdn"], "ip": rhcos_ip},
        }
    api_ip = ipv4(cluster.get("api_vip"))
    ingress_ip = ipv4(cluster.get("ingress_vip"))
    if api_ip == ingress_ip or addresses & {api_ip, ingress_ip}:
        raise ValueError("Cluster VIPs must be distinct from each other and all node endpoints")
    return {
        "cluster": {"name": cluster_name, "domain": cluster["domain"],
                    "api_vip": api_ip, "ingress_vip": ingress_ip},
        "nodes": nodes,
        "rhcos_to_physical": {entry["rhcos"]["hostname"]: key for key, entry in nodes.items()},
    }


def render_dnsmasq_records(manifest):
    """Render only records/local zones, suitable for an existing bastion dnsmasq."""
    cluster = manifest["cluster"]
    domain = cluster["name"] + "." + cluster["domain"]
    zones = {domain}
    records = ["# Generated physical endpoint DNS records; do not edit."]
    for key, entry in manifest["nodes"].items():
        records.append(f"# {key}")
        for endpoint in ("bmc", "rhcos"):
            host = entry[endpoint]
            zones.add(host["hostname"].split(".", 1)[1])
            records.append(f"host-record={host['hostname']},{host['ip']}")
    records.extend([
        f"host-record=api.{domain},api-int.{domain},{cluster['api_vip']}",
        f"address=/apps.{domain}/{cluster['ingress_vip']}",
    ])
    return "\n".join(records + [f"local=/{zone}/" for zone in sorted(zones)]) + "\n"


def verify_bmc_resolution(host, expected_ip):
    """Require system name resolution to return exactly the reviewed BMC IPv4."""
    try:
        actual = ipv4(host)
    except ValueError:
        dns_name(host)
        try:
            addresses = {item[4][0] for item in socket.getaddrinfo(
                host, None, family=socket.AF_INET, type=socket.SOCK_STREAM
            )}
        except socket.gaierror as exc:
            raise ValueError(f"BMC DNS resolution failed: {host}") from exc
        if addresses != {expected_ip}:
            raise ValueError(f"BMC DNS does not match workbook IP: {host}: {sorted(addresses)}")
    else:
        if actual != expected_ip:
            raise ValueError(f"BMC endpoint does not match workbook IP: {host}")
