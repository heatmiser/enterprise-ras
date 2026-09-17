#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""
Bootstrap input/<arch>/<site>/ocp-settings.yml from the ERA inventory.

Reads the ERA inventory hosts file produced by `make generate` and writes a
pre-populated ocp-settings.yml with node_roles derived from the inventory groups:

  [nodes]   → worker_gpu
  [support] → control_plane
  [storage] → worker_storage  (if present)

The cluster stanza (name, domain, version, VIPs) and credentials paths are
written with clearly marked TODO values for the operator to fill in before
running `make generate-ocp`.

Usage:
    make init-ocp-settings ARCH=2-8-5-200 SITE=kicktires
    python3 scripts/init-ocp-settings.py --arch 2-8-5-200 --site kicktires [--force]
"""

import argparse
import ipaddress
import sys
from pathlib import Path

try:
    from airlib.env import _load_shared_air_vault
except ImportError:
    def _load_shared_air_vault(*_args, **_kwargs):
        return {}

def find_excel(arch, site):
    """Return the first *.xlsx found in input/<arch>/<site>/, or None."""
    candidates = list((Path("input") / arch / site).glob("*.xlsx"))
    return candidates[0] if candidates else None


def read_support_vlan(excel_path):
    """Read VLANs & Profiles sheet and return (subnet, gateway) for the Support VLAN, or (None, None)."""
    try:
        import openpyxl  # noqa: F401
        from excel_parser import load_workbook_safe
    except ImportError:
        return None, None
    try:
        wb = load_workbook_safe(excel_path, data_only=True)
        ws = wb["VLANs & Profiles"]
        for row in ws.iter_rows(values_only=True):
            # Row layout: VLAN ID, Name, Purpose, Subnet, Gateway, VRF, ...
            if len(row) < 5:
                continue
            name = str(row[1]).strip() if row[1] is not None else ""
            subnet = row[3]
            gateway = row[4]
            if name.lower() == "support" and subnet:
                return str(subnet), str(gateway) if gateway else None
    except Exception:
        pass
    return None, None


# Section headers that signal the end of the OPENSHIFT block.
_KNOWN_SECTIONS = {
    "GENERAL", "AIR DEPLOYMENT", "NETWORK", "MANAGEMENT",
    "TELEMETRY", "ADVANCED", "VERSIONS",
}


def read_ocp_settings(excel_path):
    """Read OPENSHIFT section from Settings tab, return dict of ocp_* values.

    Returns an empty dict when the section is absent or openpyxl is unavailable.
    Only non-empty cell values are included — blank cells are omitted so callers
    can fall back to derived or TODO defaults.
    """
    try:
        import openpyxl  # noqa: F401
        from excel_parser import load_workbook_safe
    except ImportError:
        return {}
    try:
        wb = load_workbook_safe(excel_path, data_only=True)
        ws = wb["Settings"]
        in_section = False
        result = {}
        for row in ws.iter_rows(values_only=True):
            key = row[0]
            if key == "OPENSHIFT":
                in_section = True
                continue
            if not in_section:
                continue
            if key == "Setting":       # column header row
                continue
            if key is None or key in _KNOWN_SECTIONS:
                break                  # blank row or next section — done
            val = row[1]
            if val is not None and str(val).strip():
                result[str(key).strip()] = str(val).strip()
        return result
    except Exception:
        return {}


def suggest_vips(subnet_str):
    """Return (api_vip, ingress_vip) as the last two usable IPs in the subnet."""
    net = ipaddress.ip_network(subnet_str, strict=False)
    hosts = list(net.hosts())
    if len(hosts) < 2:
        return None, None
    return str(hosts[-1]), str(hosts[-2])


# ERA inventory group → OCP role
GROUP_TO_OCP_ROLE = {
    "nodes":   "worker_gpu",
    "support": "control_plane",
    "storage": "worker_storage",
}


def parse_hosts_file(hosts_path):
    """Parse an Ansible INI-format hosts file and return {group: [hostname, ...]}."""
    groups = {}
    current_group = None

    for raw_line in hosts_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            # Skip :children and :vars meta-sections
            if ":" in section:
                current_group = None
            else:
                current_group = section
                groups.setdefault(current_group, [])
        elif current_group is not None:
            # Take the first token (hostname); ignore inline vars
            hostname = line.split()[0]
            groups[current_group].append(hostname)

    return groups


def build_node_roles(groups):
    """Return {ocp_role: [hostnames]} from ERA inventory groups."""
    role_map = {}
    for group, ocp_role in GROUP_TO_OCP_ROLE.items():
        hosts = groups.get(group, [])
        if hosts:
            role_map[ocp_role] = sorted(hosts)
    return role_map


def format_node_roles_yaml(role_map):
    """Render node_roles block as indented YAML lines."""
    lines = []
    for role, hosts in role_map.items():
        lines.append(f"  {role}:")
        for h in hosts:
            lines.append(f"    - {h}")
    return "\n".join(lines)


def write_settings(out_path, arch, site, node_roles_yaml,
                   api_vip="", ingress_vip="", ocp=None, ssh_key_pub=None):
    """Write ocp-settings.yml, preferring values from the spreadsheet OPENSHIFT section.

    Priority for each field:
      1. Spreadsheet OPENSHIFT section value (ocp dict)
      2. Derived heuristic (VIPs from support subnet, passed as api_vip/ingress_vip)
      3. TODO placeholder
    """
    ocp = ocp or {}

    # domain
    domain = ocp.get("ocp_cluster_domain", "")
    if domain:
        domain_line = f'  domain: "{domain}"\n'
    else:
        domain_line = '  domain: "era.example.com"  # TODO: set base domain\n'

    # version
    version = ocp.get("ocp_version", "")
    if version:
        version_line = f'  version: "{version}"\n'
    else:
        version_line = '  version: "4.17.0"  # TODO: set OCP version\n'

    # VIPs: spreadsheet > derived heuristic > TODO
    xl_api_vip     = ocp.get("ocp_api_vip", "")
    xl_ingress_vip = ocp.get("ocp_ingress_vip", "")
    if xl_api_vip and xl_ingress_vip:
        api_line     = f'  api_vip: "{xl_api_vip}"\n'
        ingress_line = f'  ingress_vip: "{xl_ingress_vip}"\n'
    elif api_vip and ingress_vip:
        comment      = "# suggested from support subnet — confirm with IPAM before deploy"
        api_line     = f'  api_vip: "{api_vip}"  {comment}\n'
        ingress_line = f'  ingress_vip: "{ingress_vip}"  {comment}\n'
    else:
        api_line     = '  api_vip: ""  # TODO: set API VIP (reserved IP in the support subnet)\n'
        ingress_line = '  ingress_vip: ""  # TODO: set Ingress VIP (reserved IP in the support subnet)\n'

    # OEM
    oem_val  = ocp.get("ocp_oem", "")
    oem_line = f'oem: {oem_val}\n' if oem_val else 'oem: dell  # TODO: verify OEM (dell | hpe | lenovo)\n'

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        f"# ocp-settings.yml — generated by: make init-ocp-settings ARCH={arch} SITE={site}\n"
        f"# Fill in every field marked TODO before running: make generate-ocp ARCH={arch} SITE={site}\n"
        f"\n"
        f"cluster:\n"
        f"  name: ocp-{site}\n"
        + domain_line
        + version_line
        + api_line
        + ingress_line
        + f"\n"
        f"# Pull secret: vault key 'ocp_pull_secret' in .era-secrets/air-secrets.yml takes precedence.\n"
        f"# Fallback: path below is read only when the vault key is absent.\n"
        f"pull_secret_path: ~/.era-secrets/pull-secret.json\n"
        f"ssh_key_path: {ssh_key_pub or '~/.ssh/id_ed25519.pub'}\n"
        + oem_line
        + f"\n"
        f"node_roles:\n"
        f"{node_roles_yaml}\n"
        f"\n"
        f"install_disk:\n"
        f"  overrides: {{}}\n"
        f"\n"
        f"day2: {{}}\n"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--arch",  required=True, help="Architecture (e.g. 2-8-5-200)")
    parser.add_argument("--site",  default="default", help="Site name (default: default)")
    parser.add_argument("--force", action="store_true",
                        help="Overwrite existing ocp-settings.yml")
    args = parser.parse_args()

    hosts_path = Path("output") / args.arch / args.site / "inventory" / "hosts"
    if not hosts_path.is_file():
        sys.exit(
            f"ERROR: ERA inventory not found at {hosts_path}\n"
            f"  Run 'make generate ARCH={args.arch} SITE={args.site}' first."
        )

    out_path = Path("input") / args.arch / args.site / "ocp-settings.yml"
    if out_path.exists() and not args.force:
        sys.exit(
            f"ERROR: {out_path} already exists.\n"
            f"  Use --force (or FORCE=1) to overwrite."
        )

    groups = parse_hosts_file(hosts_path)
    node_roles = build_node_roles(groups)

    if not node_roles:
        sys.exit(
            "ERROR: no OCP-mappable groups found in ERA inventory.\n"
            "  Expected at least one of: [nodes], [support], [storage]."
        )

    node_roles_yaml = format_node_roles_yaml(node_roles)

    excel_path = find_excel(args.arch, args.site)
    support_subnet, support_gateway = read_support_vlan(excel_path) if excel_path else (None, None)
    api_vip, ingress_vip = suggest_vips(support_subnet) if support_subnet else ("", "")
    ocp = read_ocp_settings(excel_path) if excel_path else {}

    vault = _load_shared_air_vault()
    air_ssh_key = vault.get("air_ssh_key_path", "")
    ssh_key_pub = (air_ssh_key + ".pub") if air_ssh_key else None

    write_settings(out_path, args.arch, args.site, node_roles_yaml,
                   api_vip=api_vip, ingress_vip=ingress_vip, ocp=ocp,
                   ssh_key_pub=ssh_key_pub)

    print(f"\n  Wrote {out_path}")
    print(f"\n  Node roles populated from ERA inventory:")
    for role, hosts in node_roles.items():
        print(f"    {role:16s}: {', '.join(hosts)}")

    # Report which fields came from the spreadsheet vs still need manual input
    todos = []
    if not ocp.get("ocp_cluster_domain"):
        todos.append("cluster.domain      base domain (e.g. era.example.com)")
    if not ocp.get("ocp_version"):
        todos.append("cluster.version     OCP version (e.g. 4.22.0)")
    if not (ocp.get("ocp_api_vip") or api_vip):
        reason = "Excel not found" if not excel_path else "support VLAN not in spreadsheet"
        todos.append(f"cluster.api_vip     (set manually — {reason})")
    if not (ocp.get("ocp_ingress_vip") or ingress_vip):
        todos.append(f"cluster.ingress_vip (set manually — same reason as api_vip)")
    if not ocp.get("ocp_oem"):
        todos.append("oem                 dell | hpe | lenovo")

    if ocp:
        print(f"\n  Spreadsheet OPENSHIFT section values applied:")
        for k, v in ocp.items():
            print(f"    {k}: {v}")

    if todos:
        print(f"\n  TODO — fill in before running 'make generate-ocp':")
        for t in todos:
            print(f"    {t}")
    elif not (ocp.get("ocp_api_vip") and ocp.get("ocp_ingress_vip")) and api_vip and ingress_vip:
        print(f"\n  VIPs suggested from support subnet {support_subnet} — confirm with IPAM:")
        print(f"    cluster.api_vip:     {api_vip}")
        print(f"    cluster.ingress_vip: {ingress_vip}")

    _ssh_hint = (air_ssh_key + ".pub") if air_ssh_key else "~/.ssh/id_ed25519.pub"
    print(f"\n  Credentials (create before running generate-ocp):")
    print(f"    pull_secret: vault key ocp_pull_secret in .era-secrets/air-secrets.yml (or pull-secret.json fallback)")
    print(f"    ssh_key_path: {_ssh_hint}  (from vault air_ssh_key_path)" if air_ssh_key else
          f"    ssh_key_path: {_ssh_hint}  (update if different)")
    print()


if __name__ == "__main__":
    main()
