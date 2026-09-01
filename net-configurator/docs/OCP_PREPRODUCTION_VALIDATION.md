<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: MIT -->

# OCP Pre-Production Validation Pipeline

Implementation plan for validating OpenShift deployments against simulated ERA switch
fabric and RHEL VMs before committing to real hardware. Covers two sequential stages
and three execution environments.

---

## Strategic Rationale

A production ERA deployment involves multiple interlocking systems — Cumulus VX switch
fabric, RHEL node NMState bond/VLAN/GPU rail configuration, OCP cluster install
mechanics, and NVIDIA GPU operator Day 2 setup. Each layer can fail independently.
Pre-production simulation catches configuration errors cheaply before hardware is
involved.

Three prior validation layers exist but each covers only a subset:

| Layer | What it validates | Gap |
|---|---|---|
| ERA Air sim (Ubuntu nodes) | Cumulus VX NVUE, switch fabric, L2/L3 reachability | Ubuntu netplan ≠ RHEL NMState; GPU rail not tested |
| KVM digital twin (RHEL VMs, existing) | NMState bond/VLAN/GPU rail on RHEL, ping matrix | Kernel bridge only — no Cumulus VX NVUE in path |
| OOB-only OCP sim (direct-sno-install) | OCP install mechanics, node count/roles | No switch fabric, no bond config, no NMState |

**Stage 1** collapses the first two gaps: RHEL VMs replace Ubuntu in the ERA Air
simulation, so NMState bond/VLAN/GPU rail config is validated on actual RHEL across
actual Cumulus VX switches in a single simulation.

**Stage 2** closes the remaining gap: OCP is installed across the ERA virtual switch
fabric rather than an isolated OOB-only network, validating that the full network
architecture supports a working OpenShift cluster.

---

## Architecture Overview

### Three Execution Environments

```
Real Hardware (production target)
  ERA Cumulus switches + RHEL/RHCOS nodes
  Real NIC names: eno8303np0, ens3f0np0, ens5f0np0...
  agent-config.yaml drives ABI install

Stage 1 — Air (RHCOS qcow2 + Cumulus VX)        Stage 1 — KVM (local host)
  ERA topology: Cumulus VX switches               Kernel bridge + iproute2 VRFs
  + RHCOS OpenStack qcow2 server VMs              + RHEL/AlmaLinux9 VMs (existing)
  NI Ignition → nmstatectl apply                  Ansible → nmstatectl apply
  validate-ping-matrix across Cumulus VX         validate-ping-matrix across bridges
  NIC names: eth0/eth1/eth2... (virtio)          NIC names: eth0/eth1/eth2... (virtio)

Stage 2 — Air (ERA fabric + OCP install)
  ERA topology: Cumulus VX switches
  + RHCOS server VMs booting ABI discovery ISO
  OCP installs across virtual switch fabric
  Day 2: GPU rail NNCPs applied post-install
```

### NMState Generation Modes

`scripts/generate-ocp-inventory.py` must produce NMState in two NIC-name modes:

| Mode | Flag | NIC names | Target |
|---|---|---|---|
| `real-hw` (default, existing) | `--nic-mode real-hw` | Wire Map K/L column values | Real hardware ABI install |
| `kvm` (new) | `--nic-mode kvm` | `eth0`, `eth1`, `eth2`... | KVM digital twin, Air Stage 1, Air Stage 2 |

All logical config (VLANs, bond structure, IP addressing, PBR tables 901-904) is
identical between modes. Only the interface name strings change.

### RHCOS OpenStack Image Selection

Stage 1 downloads the official **RHCOS OpenStack qcow2** image directly from
`mirror.openshift.com` for the target `OCP_VERSION` (e.g., `4.22`). The OpenStack
variant includes early initramfs OpenStack metadata provider support, allowing Air Node
Instructions to pass native Ignition v3.4.0 JSON configs on first boot without needing
`subscription-manager` registration or outbound `dnf` package installs.

---

## Stage 1 — RHCOS VMs + ERA Switch Fabric Validation

### Goal

Replace Ubuntu server nodes in the ERA Air simulation with RHCOS OpenStack qcow2 VMs. Apply
NMState bond/VLAN/GPU rail configs via Ignition v3.4.0 Node Instructions on first boot.
Run `validate-ping-matrix` across the Cumulus VX switch fabric. A passing ping matrix
confirms that both the switch NVUE configs and the RHCOS NMState configs are correct
together.

### Prerequisites

- NVIDIA Air org with sufficient storage quota: `(N_server_nodes × 100GB) + switch VMs`
- `AIR_API_KEY` (or `~/.era-secrets/air-api-key`) for Air image catalog upload
- `OCP_VERSION` known (e.g., `4.22`) — determines RHCOS image version to download from `mirror.openshift.com`
- `nv-air-sdk` Python package installed
- Existing ERA Excel workbook with Wire Map K/L columns populated (see `EXCEL_CONFIGURATION_GUIDE.md`)
- `make generate` has been run successfully for the target ARCH/SITE

### Implementation Steps

---

#### Step 1 — `--nic-mode` Flag in `generate-ocp-inventory.py`

**File:** `scripts/generate-ocp-inventory.py`

Add a `--nic-mode` argument to the existing `argparse` block:

```python
parser.add_argument(
    "--nic-mode",
    choices=("real-hw", "kvm"),
    default="real-hw",
    help="NIC naming mode: real-hw uses Wire Map K/L kernel names; "
         "kvm uses eth0/eth1/ethN (virtio) for KVM and Air simulations.",
)
```

Pass `nic_mode` through to all functions that reference NIC names. The substitution
logic applies to three places:

**1a. `build_agent_config()` — `interfaces:` list**

```python
# existing (real-hw mode):
hw_interfaces = [
    {"name": entry["kernel"], "macAddress": entry["mac"]}
    for entries in nic_map.values()
    for entry in entries
    if entry.get("kernel") and entry.get("mac")
]

# kvm mode: enumerate all NICs across profiles, assign eth0, eth1, ethN...
if nic_mode == "kvm":
    all_entries = [
        entry for entries in nic_map.values() for entry in entries
        if entry.get("kernel")
    ]
    hw_interfaces = [
        {"name": f"eth{i}", "macAddress": entry.get("mac", "")}
        for i, entry in enumerate(all_entries)
    ]
```

MAC addresses in kvm mode are informational only (not used by AI SaaS discovery);
they can be left empty or retained from Wire Map column L.

**1b. `build_nmstate_network_config()` — bond member names**

The `_bond_members()` helper currently returns kernel names from `nic_map`. In kvm
mode, substitute `ethN` indices:

```python
def _bond_members(profile_key, nic_mode="real-hw"):
    if nic_map and nic_mode == "real-hw":
        entries = nic_map.get(profile_key, [])
        if entries:
            return [e["kernel"] for e in entries]
    elif nic_map and nic_mode == "kvm":
        # Assign ethN based on position across all profiles in insertion order
        offset = _kvm_offset_for_profile(nic_map, profile_key)
        count = len(nic_map.get(profile_key, []))
        return [f"eth{offset + i}" for i in range(count)]
    return ifaces_map.get(profile_key, [])
```

`_kvm_offset_for_profile()` walks the `nic_map` dict in key order and sums entry
counts for profiles that precede the requested one, yielding the correct `ethN` start
index. Profile key order should be consistent — define a canonical order constant:

```python
_KVM_PROFILE_ORDER = ("oob", "cpu", "gpu", "support", "storage")
```

**1c. `_build_gpu_rail_desiredstate()` — GPU rail iface names**

```python
if nic_mode == "kvm":
    gpu_offset = _kvm_offset_for_profile(nic_map, "gpu")
    gpu_kernel_names = [f"eth{gpu_offset + i}" for i in range(len(gpu_ifaces_list))]
else:
    gpu_kernel_names = [e["kernel"] for e in nic_map.get("gpu", []) if e.get("kernel")]
```

**Makefile integration:** Pass `--nic-mode $(NIC_MODE)` to `generate-ocp-inventory.py`
in the `generate-ocp` target. Default `NIC_MODE=real-hw`. Callers use
`make generate-ocp NIC_MODE=kvm`.

---

#### Step 2 — NI Script Generator: RHCOS Ignition v3.4.0 Payloads

**File:** `scripts/generate-node-instructions.py`

When server nodes run RHCOS OpenStack qcow2 images in Air Stage 1, Air Node
Instructions pass native **Ignition v3.4.0 JSON payloads** (`<node>.ign`) on first boot.
The Ignition config configures:

1. `/etc/hostname`
2. `/etc/nmstate/network-config.yml` (from the per-node `networkConfig` block generated by `generate-ocp-inventory.py --nic-mode kvm`)
3. `era-nmstate.service` systemd unit that runs `nmstatectl apply` on first boot before network-pre.target

**Ignition JSON payload structure (per server node):**

```json
{
  "ignition": {
    "version": "3.4.0"
  },
  "storage": {
    "files": [
      {
        "overwrite": true,
        "path": "/etc/hostname",
        "mode": 420,
        "contents": {
          "source": "data:text/plain;charset=utf-8;base64,..."
        }
      },
      {
        "overwrite": true,
        "path": "/etc/nmstate/network-config.yml",
        "mode": 420,
        "contents": {
          "source": "data:text/plain;charset=utf-8;base64,..."
        }
      }
    ]
  },
  "systemd": {
    "units": [
      {
        "name": "era-nmstate.service",
        "enabled": true,
        "contents": "[Unit]\nDescription=Apply ERA Stage 1 NMState Configuration\nBefore=network-pre.target\nWants=network-pre.target\n\n[Service]\nType=oneshot\nExecStart=/usr/bin/nmstatectl apply /etc/nmstate/network-config.yml\nRemainAfterExit=yes\n\n[Install]\nWantedBy=multi-user.target\n"
      }
    ]
  }
}
```

**Implementation in `generate-node-instructions.py`:**

Add `--server-os` argument with choices `ubuntu` (default), `rhcos`, and `rhel`.
When `--server-os rhcos` (or `rhel`), read `output/<arch>/<site>/ocp/inventory/host_vars/<node>.yml`
for each server node, extract the `networkConfig` block, render the Ignition v3.4.0 JSON
payload, and write to `output/<arch>/<site>/topology/node-instructions/<node>.ign`.

The Air deploy path (`air-deploy.py`) injects Node Instructions directly from that
directory — no changes to the Air SDK call are needed.

---

#### Step 3 — RHCOS OpenStack qcow2 Download & Air Upload Automation

**File:** `scripts/upload_rhcos_image.py`

Downloads official RHCOS OpenStack qcow2 images from `mirror.openshift.com` and
uploads them to the NVIDIA Air organization image catalog.

Responsibilities:
1. Query `mirror.openshift.com/pub/openshift-v4/dependencies/rhcos/<ocp_version>/latest/sha256sum.txt` to find exact filename and SHA256 checksum.
2. Download `.qcow2.gz` image into `.cache/`.
3. Verify SHA256 checksum prior to decompression.
4. Decompress image to `.cache/rhcos-<ocp_version>-openstack.x86_64.qcow2`.
5. Upload to Air org image catalog idempotently (`default_username="core"`).

```python
# Usage:
python3 scripts/upload_rhcos_image.py --ocp-version 4.22
```

**Environment variables required:**

| Variable | Alt | Description |
|---|---|---|
| `OCP_VERSION` | — | Target OCP version (default `4.22`) |
| `AIR_API_KEY` | `AIR_API_KEY_FILE` | NGC API key for Air uploads |

---

#### Step 4 — ERA Topology: Parameterize Server OS Image

**File:** `scripts/topology_generator.py`

Currently, server node OS images in the generated topology JSON are set to an Ubuntu
image name (e.g., `generic/ubuntu2204`). This needs to be configurable so Stage 1
can substitute the RHEL qcow2 image name.

Add a `--server-image` CLI argument:

```python
parser.add_argument(
    "--server-image",
    default=None,
    help="Override Air OS image name for server nodes "
         "(e.g. 'rhel-94-kvm'). Default: existing Ubuntu image.",
)
```

When `--server-image` is provided, substitute it for all non-switch node `"os"`
fields in the generated topology JSON. Switch nodes (Cumulus VX) are unaffected.

**Makefile integration:** Add `SERVER_IMAGE` variable to the `generate` target
passthrough:

```makefile
generate:
    ...
    python3 scripts/topology_generator.py generate \
        --arch $(ARCH) --site $(SITE) \
        $(if $(SERVER_IMAGE),--server-image $(SERVER_IMAGE),) \
        ...
```

Callers use: `make generate ARCH=2-8-5-200 SITE=kicktires SERVER_IMAGE=rhcos-422-openstack`

---

#### Step 5 — New Makefile Target: `validate-air-fabric`

**File:** `Makefile`

New target that runs the full Stage 1 Air validation pipeline. It is additive — does
not modify any existing target.

```makefile
validate-air-fabric: ## Stage 1: Deploy ERA fabric with RHCOS VMs in Air, run ping matrix
    $(call check_ansible)
    @test -n "$(ARCH)" || { echo "ARCH is required"; exit 1; }
    @$(MAKE) --no-print-directory generate \
        ARCH=$(ARCH) SITE=$(SITE) \
        SERVER_IMAGE=$(or $(RHCOS_IMAGE_NAME),rhcos-422-openstack)
    @$(MAKE) --no-print-directory generate-ocp \
        ARCH=$(ARCH) SITE=$(SITE) NIC_MODE=kvm
    @python3 scripts/generate-node-instructions.py \
        --arch $(ARCH) --site $(SITE) --server-os rhcos
    @python3 scripts/upload_rhcos_image.py \
        --ocp-version $(or $(OCP_VERSION),4.22) \
        --image-name $(or $(RHCOS_IMAGE_NAME),rhcos-422-openstack)
    @python3 scripts/air-deploy.py \
        --arch $(ARCH) --site $(SITE) --no-ztp
    @ansible-playbook playbooks/validate-ping-matrix.yml \
        -i output/$(ARCH)/$(SITE)/inventory/hosts \
        $(if $(SITE_VARS),-e @$(SITE_VARS),) \
        -e ansible_user=$(or $(SERVER_ANSIBLE_USER),core) \
        -e arch=$(ARCH) -e site=$(SITE)
```

Variables:

| Variable | Default | Description |
|---|---|---|
| `ARCH` | required | ERA architecture identifier |
| `SITE` | `default` | Site name |
| `OCP_VERSION` | `4.22` | OCP release version |
| `RHCOS_IMAGE_NAME` | `rhcos-422-openstack` | Air image name for server nodes (set by `upload_rhcos_image.py`) |
| `SERVER_ANSIBLE_USER` | `core` | SSH user on RHCOS cloud VMs |

**Full invocation example:**

```bash
export OCP_VERSION=4.22
export AIR_API_KEY="$(cat ~/.era-secrets/air-api-key)"

make validate-air-fabric ARCH=2-8-5-200 SITE=kicktires
```

---

#### Step 6 — KVM Mode: Verify `--nic-mode kvm` Alignment

**Status:** The KVM digital twin (`make deploy-kvm`) is already operational (Steps
1-5 complete as of 2026-08-22). It deploys RHEL/AlmaLinux9 qcow2 VMs with `eth0`–`eth6`
NIC naming and applies NMState via Ansible.

**Gap:** `playbooks/deploy-kvm.yml` currently extracts `networkConfig` from
`output/<arch>/<site>/ocp/inventory/host_vars/<node>.yml`. Those files are generated
with real hardware NIC names unless `--nic-mode kvm` is passed to
`generate-ocp-inventory.py`. Without this flag, the NMState applied to KVM VMs
references `eno8303np0` etc., which do not exist in the VM — the apply silently
fails or creates no-op state.

**Fix:** Update the `generate-ocp` invocation in the `deploy-kvm` pipeline (or in
documentation) to always pass `NIC_MODE=kvm`:

```makefile
deploy-kvm:
    @$(MAKE) --no-print-directory generate-ocp \
        ARCH=$(ARCH) SITE=$(SITE) NIC_MODE=kvm
    @ansible-playbook -i localhost, playbooks/deploy-kvm.yml \
        ...
```

This is a one-line Makefile fix that ensures KVM mode always gets `ethN` NMState.

---

### Stage 1 Validation Flow

```
make validate-air-fabric ARCH=2-8-5-200 SITE=kicktires
  │
  ├── make generate (with SERVER_IMAGE=rhel-94-kvm)
  │     validate_excel.py → excel_parser.py → topology_generator.py
  │     topology JSON: Cumulus VX switches + RHEL server nodes
  │
  ├── make generate-ocp NIC_MODE=kvm
  │     generate-ocp-inventory.py → host_vars/<node>.yml with eth0/eth1/eth2...
  │
  ├── generate-node-instructions.py --server-os rhel
  │     NI scripts: nmstatectl apply per node (reads host_vars NMState)
  │
  ├── upload_rhel_image.py
  │     RH API → download rhel-9.X-kvm.qcow2 → upload to Air catalog (idempotent)
  │
  ├── air-deploy.py --no-ztp
  │     Import topology → start sim → inject NI scripts → configure jump host SSH
  │     (Cumulus VX switches: ZTP configures NVUE as in existing Air flow)
  │     (RHEL VMs: NI scripts run nmstatectl apply on first boot)
  │
  └── validate-ping-matrix.yml
        SSH via jump host → all server nodes
        Ping matrix: bond0 VLANs, GPU rail VLANs, cross-VLAN failures
        PASS: all expected paths reachable, all cross-VRF paths unreachable
```

### Stage 1 Success Criteria

- All RHEL VMs boot and reach `cloud-user` SSH via OOB jump host
- `nmstatectl apply` completes without errors on each VM (check via NI script exit code)
- `validate-ping-matrix` passes: same PASS/FAIL matrix as existing Ubuntu Air sim
- Bond0 is up with correct VLAN tagging on all server nodes
- GPU rail VLANs (901-904) are reachable between GPU nodes
- Cross-VRF pings fail at router boundary (correct behavior)

---

## Stage 2 — Full OCP Install on ERA Virtual Switch Fabric

### Goal

Deploy OCP using the Agent-Based Installer (ABI) on server VMs running inside an ERA
Air simulation that includes the full Cumulus VX switch fabric. The cluster installs
and reaches `Installed` state with all nodes `Ready`. GPU rail Day 2 NNCPs are applied
post-install.

This is the highest-fidelity pre-production validation. It proves that:
- The switch fabric correctly routes OCP bootstrap, API VIP, and Ingress VIP traffic
- The ABI `agent-config.yaml` NMState (in kvm/ethN mode) successfully configures the
  nodes during install
- The cluster is reachable through the virtual fabric (not just OOB)

### How Stage 2 Differs from Stage 1

In Stage 1, server VMs are long-lived — they boot, apply NMState, and stay up for
ping testing. In Stage 2, server VMs must boot from the ABI discovery ISO, be
discovered by the Assisted Installer SaaS, have their identity confirmed by MAC
address matching, install OCP to their blank disk, and reboot into a running cluster.

The switch fabric must carry all of this traffic: ABI bootstrap on bond0, API VIP
traffic, Ingress VIP traffic, and eventually GPU rail traffic post-install.

### Prerequisites for Stage 2

All Stage 1 infrastructure must be complete and working:
- `--nic-mode kvm` NMState generation working correctly
- RHEL qcow2 upload automation in place
- ERA topology parameterized for server OS image

Additional prerequisites:
- AI SaaS account with `AI_OFFLINETOKEN` (Red Hat offline token with AI SaaS access)
- OCP pull secret (`PULL_SECRET_PATH`)
- SSH public key for cluster node access
- Air org storage: (N_nodes × 100GB blank disk) + switch VMs — verify quota before start
- `direct-sno-install` repository at `/home/msavage/projects/enterprise-ras-collab/direct-sno-install/`
  on branch `3-node-ha-multinode-cluster` — this provides the AI SaaS orchestration scripts
- `uv` package manager for running `direct-sno-install` scripts (`pip install uv`)

### Key Technical Challenges

#### Challenge 1 — NIC Names in ABI vs Air VMs

The ABI `agent-config.yaml` identifies each host by MAC address in the `interfaces:`
list. In Air VMs, the virtio NICs present as `eth0`, `eth1`, etc. with Air-assigned
MACs. The `agent-config.yaml` generated with `--nic-mode kvm` uses `ethN` names, but
the MAC addresses in Wire Map column L are real hardware MACs — they will not match
what Air's virtio NICs report.

**Resolution:** For Stage 2, the `interfaces:` MAC addresses in `agent-config.yaml`
must be either:
- Left empty (if ABI supports MAC-less host identification by hostname), OR
- Populated from the Air topology's OOB interface MACs (which Air assigns dynamically)

The safer approach is to use AI SaaS (Assisted Installer) rather than ABI for Stage 2.
AI SaaS does not use `agent-config.yaml` at all — hosts are discovered by IP on the
machine network, not by pre-declared MACs. This is the approach used by
`direct-sno-install`. The implication: Stage 2 uses AI SaaS discovery ISO, not ABI.

#### Challenge 2 — Machine Network CIDR Must Traverse the Fabric

In the OOB-only approach (direct-sno-install), the machine network is
`192.168.200.0/24` (OOB). In Stage 2, the goal is for OCP to install across the ERA
switch fabric — meaning the machine network should be the bond0 VLAN subnet (e.g.,
VLAN 300, `10.78.221.0/24` for GPU nodes) rather than OOB.

This requires:
- Server nodes to have data-plane links in the topology JSON (not just OOB)
- Bond0 NMState applied before AI SaaS discovers the nodes
- AI SaaS configured with the correct machine network CIDR (`10.78.221.0/24` etc.)
- API VIP and Ingress VIP on a subnet reachable through the fabric

The discovery sequence becomes:
1. Node boots ABI/AI discovery ISO via cdrom
2. Bond0 NMState is applied by NI script on boot (Stage 1 NI script, reused)
3. Bond0 comes up on VLAN 300 with an IP in `10.78.221.0/24`
4. AI SaaS discovers the node via bond0 IP (not OOB)
5. Install proceeds across bond0 / switch fabric

This is feasible but requires that the NI script runs and applies NMState BEFORE the
AI SaaS agent attempts to register — timing is non-trivial. The discovery ISO must
execute the NI script early in boot, before the assisted-installer agent contacts
`api.openshift.com`.

#### Challenge 3 — VIP Routing Through Virtual Fabric

API VIP and Ingress VIP must be routable through the Cumulus VX switches. In the ERA
topology, the router-vm (iproute2 VRFs) provides L3 gateways. VIPs must be in the
bond0 VLAN subnet range and reachable from the jump host and from within the cluster.

If using AI SaaS discovery, the VIPs are set via `ai.update_cluster()` call. They
must be addresses in the machine network CIDR that are not already assigned by DHCP.
Static IP assignment for VIPs requires either:
- DHCP reservation at the router-vm, OR
- Unused addresses at the top of the subnet (current approach in `init-ocp-settings.py`)

#### Challenge 4 — Stage 1 Topology vs Stage 2 Topology

Stage 1 deploys long-lived RHEL VMs. Stage 2 requires blank-disk VMs that boot from
a discovery ISO. These are different node configurations and cannot coexist in the
same Air simulation without topology changes.

The operational flow is: run Stage 1 (RHEL VMs, ping matrix) → destroy the sim →
rebuild with Stage 2 topology (blank disks + discovery ISO cdrom) → run Stage 2 (OCP
install). Or maintain two separate topology JSON files.

### Stage 2 Implementation Steps

The following steps assume Stage 1 is complete and working.

#### Step 2.1 — Extend ERA Topology with Blank-Disk Server Nodes + Discovery ISO

The ERA topology JSON currently has server nodes with `"os": "<rhel-image>"` (from
Stage 1). For Stage 2, these become:

```json
"ocp-cp-0": {
    "cpu": 16,
    "memory": 65536,
    "storage": 100,
    "os": "blank-100g",
    "cdrom": "dsxair-discovery-<epoch>",
    "boot": ["hd", "cdrom"],
    "cpu_mode": "host-passthrough",
    "uefi": false,
    "secureboot": false
}
```

`topology_generator.py` needs a `--stage2` flag (or `--server-mode ocp-install`) that
switches server nodes to blank-disk + cdrom configuration. The timestamped cdrom name
matches what `upload_discovery_iso.py` uploads (from `direct-sno-install`).

Data-plane links for server nodes must also be added to the topology — nodes need
eth1/eth2 for bond0 (connected to leaf switch ports). This is a topology structural
change: `"links": []` is no longer sufficient. The link definitions mirror what
`topology_generator.py` already generates for switch-to-switch and switch-to-server
connections.

#### Step 2.2 — AI SaaS Cluster Creation for ERA Node Count

Adopt `direct-sno-install/scripts/00_create_discovery_iso.py` with ERA-specific
overrides:

```python
# ERA 2-8-5-200 multinode cluster
overrides = {
    "openshift_version": env_config.ocp_version(),
    "cpu_architecture": "x86_64",
    "sno": False,
    "control_plane_count": 3,       # from ocp-settings.yml
    "user_managed_networking": False,
    "api_vip": "<bond0-vlan-subnet-vip>",       # from ocp-settings.yml, NOT 192.168.200.10
    "ingress_vip": "<bond0-vlan-subnet-vip+1>", # from ocp-settings.yml
    "machine_networks": ["10.78.221.0/24"],     # bond0 VLAN 300 subnet (GPU nodes)
}
```

Worker count (GPU workers) is derived from `ocp-settings.yml` `node_roles` block.

#### Step 2.3 — NI Script for Stage 2 Boot Sequence

The Stage 2 NI script must execute in two phases:
1. Apply bond0 NMState immediately on boot (before AI SaaS agent starts)
2. Optionally: wait for AI SaaS discovery to complete, then do nothing further
   (the AI SaaS installer handles the rest)

The NI script is the same as Stage 1 (`nmstatectl apply`) but must be injected
before the discovery ISO's AI agent service starts. This may require embedding the
NMState apply in a pre-boot systemd unit or RHCOS extension. Research required —
the ABI / AI SaaS discovery ISO boot sequence may or may not permit pre-agent NI
execution. This is the most uncertain piece of Stage 2.

An alternative: accept that Stage 2 uses OOB network for AI SaaS discovery (same as
`direct-sno-install`), and consider "OCP installs on fabric" to mean that post-install
the Day 1 NMState (bond0) is applied and verified — not that the install itself
traverses the fabric. This is a reduced but still meaningful validation: proves the
cluster can be installed AND bond0 works on the same RHEL nodes.

#### Step 2.4 — Role Assignment and Install Orchestration

Adopt `direct-sno-install`'s `run_cluster.py` orchestration pattern, parameterized
for ERA node counts:

```bash
CLUSTER_PROFILE=multinode \
CLUSTER_NAME=$(ocp_cluster_name_from_ocp_settings) \
OCP_VERSION=4.22 \
CONTROL_PLANE_COUNT=3 \
EXPECTED_HOSTS=7 \
CONTROL_PLANE_NODES="ocp-cp-0,ocp-cp-1,ocp-cp-2" \
uv run scripts/run_cluster.py --yes --profile multinode
```

The `env_config.control_plane_node_names()` heuristic (names containing `cp`,
`master`, `control`) correctly identifies ERA control plane nodes by name convention.

#### Step 2.5 — Day 2 GPU Rail NNCPs

After the cluster reaches `Installed` state, apply the GPU rail NNCPs generated by
`make generate-ocp`:

```bash
# For each GPU worker node
oc apply -f output/2-8-5-200/kicktires/ocp/day2/nncp-<hostname>-gpu-rails.yaml
```

In `--nic-mode kvm`, the GPU rail NNCPs use `eth3`–`eth6` interface names. These
must match the Air topology link assignments for GPU rail ports on each server node.

#### Step 2.6 — New Makefile Target: `validate-air-ocp`

```makefile
validate-air-ocp: ## Stage 2: Deploy ERA fabric + OCP install in Air
    $(call check_ansible)
    @test -n "$(ARCH)" || { echo "ARCH is required"; exit 1; }
    @$(MAKE) --no-print-directory generate \
        ARCH=$(ARCH) SITE=$(SITE) SERVER_IMAGE=blank-100g \
        AIR_SERVER_MODE=ocp-install
    @$(MAKE) --no-print-directory generate-ocp \
        ARCH=$(ARCH) SITE=$(SITE) NIC_MODE=kvm
    @python3 scripts/upload_rhel_image.py --arch $(ARCH) --site $(SITE)
    @python3 scripts/upload_blank_disk.py
    @python3 scripts/upload_discovery_iso.py --name dsxair-discovery-$(shell date +%s)
    @python3 scripts/air-deploy.py --arch $(ARCH) --site $(SITE) --no-ztp
    @cd $(DIRECT_SNO_DIR) && \
        CLUSTER_PROFILE=multinode \
        EXPECTED_HOSTS=$(or $(OCP_EXPECTED_HOSTS),7) \
        OCP_VERSION=$(OCP_VERSION) \
        uv run scripts/run_cluster.py --yes --profile multinode
```

`DIRECT_SNO_DIR` points to the `direct-sno-install` repo checkout.

### Stage 2 Success Criteria

- All 7 nodes (3 CP + 4 GPU workers) discovered by AI SaaS with expected OOB or bond0 IPs
- OCP cluster reaches `Installed` state with 0 failed hosts
- `oc get nodes` shows all 7 nodes `Ready`
- `oc get co` shows all cluster operators `Available=True`
- Day 2 GPU rail NNCPs apply without errors
- Ping matrix from GPU nodes across GPU rail VLANs passes post-install

---

## Resource Requirements

### NVIDIA Air

| Resource | Stage 1 | Stage 2 |
|---|---|---|
| Server node storage | N × 100GB (RHEL qcow2 disk) | N × 100GB (blank disk) |
| Switch node storage | Existing ERA topology size | Same |
| Image catalog | rhel-9X-kvm (≈8GB) + existing | + blank-100g (≈100GB) + discovery ISO (≈1GB) |
| Verify org quota | Before each stage | Before each stage |

For 2-8-5-200: 7 server nodes + 2 spines + 8 leaves = 9 switch VMs. Verify total
storage budget in Air org before deploying.

### Local KVM Host (for KVM digital twin OCP extension)

The existing KVM digital twin runs at 2 vCPU / 2GB per VM (suitable for ping matrix
only). Full OCP installation requires production-grade sizing:

| Node type | Count | Min vCPU | Min RAM |
|---|---|---|---|
| Control plane | 3 | 16 | 64GB |
| GPU worker | 4 | 16 | 64GB |
| Router VM | 1 | 2 | 2GB |
| **Total** | **8** | **114** | **514GB** |

Available lab servers:
- Single-proc: Intel Xeon 6 6761P (64C/128T, 1TB RAM) — sufficient for full cluster
- Dual-proc: Intel Xeon 6 6787P × 2 (172C/344T, 2TB RAM) — sufficient for full cluster
  plus headroom for concurrent simulations

KVM OCP extension is a future milestone — it does not gate Stage 1 or Stage 2.

---

## Related Documentation

- `docs/AIR_DEPLOYMENT_GUIDE.md` — existing ERA Air deploy reference
- `docs/KVM_DIGITAL_TWIN.md` — existing KVM digital twin deploy instructions
- `docs/EXCEL_CONFIGURATION_GUIDE.md` — Wire Map K/L column setup for real hardware NIC names
- `direct-sno-install/` repo, branch `3-node-ha-multinode-cluster` — AI SaaS orchestration
  reference implementation; adopt patterns for Stage 2

## Related Scripts

| Script | Location | Role |
|---|---|---|
| `generate-ocp-inventory.py` | `scripts/` | Add `--nic-mode kvm` (Step 1) |
| `generate-node-instructions.py` | `scripts/` | Add `--server-os rhel` (Step 2) |
| `upload_rhel_image.py` | `scripts/` (new) | RHEL qcow2 download + Air upload (Step 3) |
| `topology_generator.py` | `scripts/` | Add `--server-image` (Step 4) |
| `00_create_discovery_iso.py` | `direct-sno-install/scripts/` | AI SaaS cluster creation (Stage 2) |
| `run_cluster.py` | `direct-sno-install/scripts/` | Stage 2 install orchestrator |
| `env_config.py` | `direct-sno-install/scripts/` | Config/secret resolution pattern to adopt |
