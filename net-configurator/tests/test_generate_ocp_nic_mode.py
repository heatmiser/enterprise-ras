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
        "bond_ip": "10.78.221.10/24",
        "interfaces": {
            "oob": ["eth0"],
            "cpu": ["eth1", "eth2"],
            "gpu": ["eth3", "eth4"],
        },
        "nic_map": {
            "oob": [{"kernel": "eno1", "mac": "00:11:22:33:44:00"}],
            "cpu": [
                {"kernel": "ens3f0np0", "mac": "00:11:22:33:44:01"},
                {"kernel": "ens3f1np0", "mac": "00:11:22:33:44:02"},
            ],
            "gpu": [
                {"kernel": "ens5f0np0", "mac": "00:11:22:33:44:03"},
                {"kernel": "ens5f1np0", "mac": "00:11:22:33:44:04"},
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
        "devices": {"su-1-node-1": sample_device_data()},
    }


def test_kvm_offset_calculation():
    nic_map = sample_device_data()["nic_map"]
    assert gen_ocp._kvm_offset_for_profile(nic_map, "oob") == 0
    assert gen_ocp._kvm_offset_for_profile(nic_map, "cpu") == 1
    assert gen_ocp._kvm_offset_for_profile(nic_map, "gpu") == 3


def test_real_hw_nic_mode_bond_and_gpu():
    dev = sample_device_data()
    site = sample_site_vars()

    # NMState networkConfig in real-hw mode
    cfg_real = gen_ocp.build_nmstate_network_config(dev, site, "worker_gpu", nic_mode="real-hw")
    bond_ports_real = cfg_real["interfaces"][0]["link-aggregation"]["port"]
    assert bond_ports_real == ["ens3f0np0", "ens3f1np0"]

    # GPU rail NNCP desiredState in real-hw mode
    gpu_state_real = gen_ocp._build_gpu_rail_desiredstate(dev, site, nic_mode="real-hw")
    gpu_iface_names_real = [iface["name"] for iface in gpu_state_real["interfaces"]]
    assert gpu_iface_names_real == ["ens5f0np0", "ens5f1np0"]


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


def test_agent_config_nic_modes():
    site = sample_site_vars()
    role_map = {"su-1-node-1": "worker_gpu"}
    ocp_settings = {"cluster": {"name": "test-cluster"}}

    agent_real = gen_ocp.build_agent_config(ocp_settings, role_map, {}, site, "2-8-5-200", nic_mode="real-hw")
    host_ifaces_real = [iface["name"] for iface in agent_real["hosts"][0]["interfaces"]]
    assert host_ifaces_real == ["eno1", "ens3f0np0", "ens3f1np0", "ens5f0np0", "ens5f1np0"]

    agent_kvm = gen_ocp.build_agent_config(ocp_settings, role_map, {}, site, "2-8-5-200", nic_mode="kvm")
    host_ifaces_kvm = [iface["name"] for iface in agent_kvm["hosts"][0]["interfaces"]]
    assert host_ifaces_kvm == ["eth0", "eth1", "eth2", "eth3", "eth4"]
