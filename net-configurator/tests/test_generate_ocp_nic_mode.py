# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""Unit tests for --nic-mode flag and ethN mapping in generate-ocp-inventory.py."""

import sys
from pathlib import Path

# Add net-configurator/scripts to sys.path so we can import generate-ocp-inventory helpers
scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))

import importlib.util

spec = importlib.util.spec_from_file_location("generate_ocp_inventory", scripts_dir / "generate-ocp-inventory.py")
gen_ocp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen_ocp)


def sample_device_data():
    return {
        "mac": "00:11:22:33:44:00",
        "eth0_ip": "192.0.2.10/24",
        "bond_ip": "10.78.221.10/24",
        "interfaces": {
            "oob": ["eth0"],
            "cpu": ["eth1", "eth2"],
            "gpu": ["eth3", "eth4"],
        },
        "nic_alias_map": {
            "oob": [{"alias": "host-oob0", "mac": "00:11:22:33:44:00"}],
            "cpu": [
                {"alias": "ns-nic0", "mac": "00:11:22:33:44:01"},
                {"alias": "ns-nic1", "mac": "00:11:22:33:44:02"},
            ],
            "gpu": [
                {"alias": "rail0", "mac": "00:11:22:33:44:03"},
                {"alias": "rail1", "mac": "00:11:22:33:44:04"},
            ],
        },
        "gpu_interfaces": [
            {"iface": "ens5f0np0", "ip": "192.168.1.10/24", "gateway": "192.168.1.1", "table": 901},
            {"iface": "ens5f1np0", "ip": "192.168.2.10/24", "gateway": "192.168.2.1", "table": 902},
        ],
    }


def sample_site_vars():
    return {
        "common": {"cpu_gateway": "10.78.221.1"},
        "dns_servers": ["192.0.2.53", "192.0.2.54"],
        "devices": {"su-1-node-1": sample_device_data()},
    }


def test_kvm_offset_calculation():
    nic_alias_map = sample_device_data()["nic_alias_map"]
    assert gen_ocp._kvm_offset_for_profile(nic_alias_map, "oob") == 0
    assert gen_ocp._kvm_offset_for_profile(nic_alias_map, "cpu") == 1
    assert gen_ocp._kvm_offset_for_profile(nic_alias_map, "gpu") == 3


def test_dell_disk_defaults_are_arch_oem_and_role_specific():
    defaults = gen_ocp.load_disk_defaults()

    assert gen_ocp.resolve_disk(
        "ipp5-285-rh-k8s-01", "control_plane", "2-8-5-200", "dell", defaults, {}
    ) == ("/dev/disk/by-path/pci-0000:01:00.0-nvme-1", False)
    assert gen_ocp.resolve_disk(
        "ipp5-285-rh-gpu-01", "worker_gpu", "2-8-5-200", "dell", defaults, {}
    ) == ("/dev/disk/by-path/pci-0000:81:00.0-nvme-1", False)
    assert gen_ocp.resolve_disk(
        "ipp5-285-rh-k8s-01", "control_plane", "2-8-5-200", "dell", defaults,
        {"ipp5-285-rh-k8s-01": "/dev/disk/by-id/operator-selected"},
    ) == ("/dev/disk/by-id/operator-selected", False)
    assert gen_ocp.resolve_disk(
        "unknown", "control_plane", "2-8-5-200", "hpe", defaults, {}
    ) == ("/dev/sda", True)


def test_real_hw_nic_mode_bond_and_gpu():
    dev = sample_device_data()
    site = sample_site_vars()

    # NMState networkConfig in real-hw mode
    cfg_real = gen_ocp.build_nmstate_network_config(dev, site, "worker_gpu", nic_mode="real-hw")
    assert cfg_real["interfaces"][:2] == [
        {
            "name": "ns-nic0",
            "type": "ethernet",
            "identifier": "mac-address",
            "mac-address": "00:11:22:33:44:01",
            "state": "up",
        },
        {
            "name": "ns-nic1",
            "type": "ethernet",
            "identifier": "mac-address",
            "mac-address": "00:11:22:33:44:02",
            "state": "up",
        },
    ]
    bond_ports_real = cfg_real["interfaces"][2]["link-aggregation"]["port"]
    assert cfg_real["interfaces"][2]["name"] == "ns-bond0"
    assert bond_ports_real == ["ns-nic0", "ns-nic1"]
    assert cfg_real["routes"]["config"][0]["next-hop-interface"] == "ns-bond0"
    assert cfg_real["dns-resolver"] == {"config": {"server": ["192.0.2.53", "192.0.2.54"]}}

    # GPU rail NNCP desiredState in real-hw mode
    gpu_state_real = gen_ocp._build_gpu_rail_desiredstate(dev, site, nic_mode="real-hw")
    gpu_iface_names_real = [iface["name"] for iface in gpu_state_real["interfaces"]]
    assert gpu_iface_names_real == ["rail0", "rail1"]
    assert gpu_state_real["interfaces"][0]["identifier"] == "mac-address"
    assert gpu_state_real["interfaces"][0]["mac-address"] == "00:11:22:33:44:03"


def test_kvm_nic_mode_bond_and_gpu():
    dev = sample_device_data()
    site = sample_site_vars()

    # NMState networkConfig in kvm mode
    cfg_kvm = gen_ocp.build_nmstate_network_config(dev, site, "worker_gpu", nic_mode="kvm")
    bond_ports_kvm = cfg_kvm["interfaces"][0]["link-aggregation"]["port"]
    assert bond_ports_kvm == ["eth1", "eth2"]

    # GPU rail NNCP desiredState in kvm mode
    gpu_state_kvm = gen_ocp._build_gpu_rail_desiredstate(dev, site, nic_mode="kvm")
    gpu_iface_names_kvm = [iface["name"] for iface in gpu_state_kvm["interfaces"]]
    assert gpu_iface_names_kvm == ["eth3", "eth4"]


def test_inspection_nmstate_is_cpu_bond_only():
    dev = sample_device_data()
    site = sample_site_vars()

    cfg = gen_ocp.build_inspection_nmstate_network_config(dev, site, nic_mode="real-hw")

    assert [iface["name"] for iface in cfg["interfaces"]] == ["bond0"]
    assert cfg["interfaces"][0]["link-aggregation"]["port"] == ["ns-nic0", "ns-nic1"]
    assert cfg["routes"]["config"][0]["next-hop-interface"] == "bond0"
    assert cfg["dns-resolver"]["config"]["server"] == ["192.0.2.53", "192.0.2.54"]


def test_inspection_nmstate_requires_valid_site_dns_servers():
    dev = sample_device_data()
    site = sample_site_vars()
    site["dns_servers"] = ["192.0.2.53", "192.0.2.53"]

    try:
        gen_ocp.build_inspection_nmstate_network_config(dev, site, nic_mode="real-hw")
    except ValueError as exc:
        assert "duplicate address" in str(exc)
    else:
        raise AssertionError("expected DNS validation failure")


def test_inspection_nmstate_requires_site_dns_servers():
    dev = sample_device_data()
    site = sample_site_vars()
    site.pop("dns_servers")

    try:
        gen_ocp.build_inspection_nmstate_network_config(dev, site, nic_mode="real-hw")
    except ValueError as exc:
        assert "at least one IPv4 resolver" in str(exc)
    else:
        raise AssertionError("expected missing DNS validation failure")


def test_inspection_early_network_identities_follow_bond_member_order():
    dev = sample_device_data()
    nmstate = gen_ocp.build_inspection_nmstate_network_config(
        dev, sample_site_vars(), nic_mode="real-hw"
    )

    assert gen_ocp.build_inspection_early_network_interfaces(
        dev, nmstate, nic_mode="real-hw"
    ) == [
        {"name": "ns-nic0", "mac": "00:11:22:33:44:01"},
        {"name": "ns-nic1", "mac": "00:11:22:33:44:02"},
    ]


def test_inspection_early_network_requires_valid_cpu_mac():
    dev = sample_device_data()
    dev["nic_alias_map"]["cpu"][1]["mac"] = ""
    nmstate = gen_ocp.build_inspection_nmstate_network_config(
        dev, sample_site_vars(), nic_mode="real-hw"
    )

    try:
        gen_ocp.build_inspection_early_network_interfaces(
            dev, nmstate, nic_mode="real-hw"
        )
    except ValueError as exc:
        assert "lacks a valid workbook NIC MAC" in str(exc)
    else:
        raise AssertionError("expected MAC validation failure")


def test_inspection_nmstate_requires_cpu_bond_data():
    dev = sample_device_data()
    dev["nic_alias_map"]["cpu"] = []

    try:
        gen_ocp.build_inspection_nmstate_network_config(
            dev, sample_site_vars(), nic_mode="real-hw"
        )
    except ValueError as exc:
        assert str(exc) == "candidate has no CPU bond members"
    else:
        raise AssertionError("expected CPU-bond validation failure")


def test_agent_config_nic_modes():
    site = sample_site_vars()
    role_map = {"su-1-node-1": "worker_gpu"}
    ocp_settings = {"cluster": {"name": "test-cluster"}}

    agent_real = gen_ocp.build_agent_config(ocp_settings, role_map, {}, site, "2-8-5-200", nic_mode="real-hw")
    host_ifaces_real = [iface["name"] for iface in agent_real["hosts"][0]["interfaces"]]
    assert host_ifaces_real == ["host-oob0", "ns-nic0", "ns-nic1", "rail0", "rail1"]
    network_ifaces_real = agent_real["hosts"][0]["networkConfig"]["interfaces"]
    assert [interface["name"] for interface in network_ifaces_real] == [
        "ns-nic0", "ns-nic1", "ns-bond0"
    ]
    assert "host-oob0" not in [interface["name"] for interface in network_ifaces_real]
    assert "rail0" not in [interface["name"] for interface in network_ifaces_real]
    assert "eth0" not in [interface["name"] for interface in network_ifaces_real]

    agent_kvm = gen_ocp.build_agent_config(ocp_settings, role_map, {}, site, "2-8-5-200", nic_mode="kvm")
    host_ifaces_kvm = [iface["name"] for iface in agent_kvm["hosts"][0]["interfaces"]]
    assert host_ifaces_kvm == ["eth0", "eth1", "eth2", "eth3", "eth4"]


def test_agent_config_rejects_duplicate_alias_mac_identity():
    site = sample_site_vars()
    site["devices"]["su-1-node-1"]["nic_alias_map"]["gpu"][0]["mac"] = "00:11:22:33:44:01"

    try:
        gen_ocp.build_agent_config(
            {"cluster": {"name": "test-cluster"}},
            {"su-1-node-1": "worker_gpu"},
            {},
            site,
            "2-8-5-200",
            nic_mode="real-hw",
        )
    except ValueError as exc:
        assert "duplicate NIC MAC address" in str(exc)
    else:
        raise AssertionError("expected duplicate NIC MAC validation failure")
