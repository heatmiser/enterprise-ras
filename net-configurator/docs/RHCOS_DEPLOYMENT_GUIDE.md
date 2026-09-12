# RHCOS Air Deployment — Operator Guide

Complete step-by-step guide for deploying an RHCOS-based cluster in NVIDIA Air for
architecture `2-8-5-200`. Assumes no prior knowledge of the toolchain.

---

## Table of Contents

1. [One-Time Prerequisites](#one-time-prerequisites)
2. [Deployment Steps](#deployment-steps)
3. [Validation](#validation)
4. [Troubleshooting](#troubleshooting)
5. [Background: How RHCOS Ignition Works in Air](#background-how-rhcos-ignition-works-in-air)

---

## One-Time Prerequisites

Complete each section once per operator workstation. Skip any step already done.

### A. Install system tools

These tools are required by `make rhcos-image-prep` to patch the RHCOS boot image.

**Fedora / RHEL:**
```bash
sudo dnf install libguestfs-tools zstd
```

**Ubuntu / Debian:**
```bash
sudo apt install libguestfs-tools zstd
```

Verify:
```bash
guestfish --version && zstd --version
```

### B. Install Python packages

From the `net-configurator/` directory:
```bash
pip install -r requirements.txt
```

### C. Set your Air API token

Obtain your token from the NVIDIA Air portal (Account → API Tokens), then export it:
```bash
export NVIDIA_AIR_TOKEN=<your-token>
```

Add the export to `~/.bashrc` (or `~/.zshrc`) so it persists across sessions:
```bash
echo 'export NVIDIA_AIR_TOKEN=<your-token>' >> ~/.bashrc
```

### D. Configure your SSH key

The cluster embeds an Ed25519 public key into each RHCOS ignition config, authorizing
the `core` user for SSH and Ansible access. The key path is read from
`.era-secrets/air-secrets.yml` under `air_ssh_key_path` — set there by `make air-setup`.

Verify the setting and confirm both key files exist:
```bash
ansible-vault view .era-secrets/air-secrets.yml | grep air_ssh_key_path
ls -la ~/.ssh/<your-key> ~/.ssh/<your-key>.pub
```

If you need to change the key, re-run:
```bash
make air-setup
```

and enter the path to your existing Ed25519 private key when prompted. If no suitable
key exists, generate one first:
```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_lab -N ""
```

> **Do not set a passphrase.** Ansible requires unattended key access during deployment.
> Both the private key and its `.pub` companion must exist — `air-deploy.py` reads the
> public key at deploy time to embed it in each node's ignition payload.

---

## Deployment Steps

All `make` commands are run from the `net-configurator/` directory.

```bash
cd /path/to/enterprise-ras/net-configurator
```

### Step 1: Create the site input directory

Each deployment uses a named site directory under `input/<ARCH>/`. Use a new name
for each fresh deployment to avoid inheriting stale state.

```bash
mkdir -p input/2-8-5-200/rhcos02
```

### Step 2: Place your Excel workbook in the site directory

```bash
cp /path/to/RH-2-8-5-200.xlsx input/2-8-5-200/rhcos02/
```

The workbook defines all hardware: node names, MACs, IPs, VLANs, and Air-specific
settings. See `docs/EXCEL_CONFIGURATION_GUIDE.md` if you need to fill out a new one.

### Step 3: Import inventory from the workbook

```bash
make import EXCEL=input/2-8-5-200/rhcos02/RH-2-8-5-200.xlsx ARCH=2-8-5-200 SITE=rhcos02
```

This reads the workbook and generates Ansible inventory files under
`inventories/2-8-5-200/`. It also writes `.era-context` recording the active
`ARCH` and `SITE` so subsequent `make` targets pick them up automatically.

### Step 4: Destroy any existing Air simulation

Safe to run even if no simulation is currently active.

```bash
make air-destroy
```

### Step 5: Prepare the RHCOS image in Air

```bash
make rhcos-image-prep
```

This command is **idempotent** — run it as many times as you like. On each run it:

1. Checks whether `rhcos-422-openstack-gd` already exists in your Air image catalog
   → exits immediately if found (subsequent runs complete in seconds)
2. Downloads the base RHCOS 4.22 OpenStack qcow2 from Red Hat's public mirror (~1 GB)
3. Patches the image:
   - **GRUB boot delay (30 s)** — shifts kernel start so the Air OOB bridge is ready
     before ignition's fetch window opens
   - **Ignition fetch timeout (12 m)** — extends the built-in 2-minute limit to survive
     the ~131 s Air switch boot delay
4. Uploads the patched image to Air as `rhcos-422-openstack-gd`

**First-run time:** 15–30 minutes (dominated by download and zstd recompression).

Required system tools: `guestfish`, `zstd` (see [Prerequisites A](#a-install-system-tools)).

### Step 6: Generate switch and server configs

```bash
make generate SERVER_OS=rhcos SERVER_IMAGE=rhcos-422-openstack-gd
```

Produces switch Cumulus config files and the Air topology JSON. `SERVER_IMAGE` sets
the image name for server nodes in the topology; `SERVER_OS` controls which
node-instruction scripts are generated. Both parameters are required for an RHCOS
deployment — omitting `SERVER_IMAGE` leaves the topology defaulting to
`generic/ubuntu2204`.

### Step 7: Generate OCP inventory

```bash
make generate-ocp NIC_MODE=kvm
```

Reads the MAC addresses auto-generated into the topology JSON at Step 6 and writes
them into the OCP inventory host_vars. These MACs are used by NMState to match
network profiles to the correct physical interfaces on each RHCOS node.

> **Order matters:** Step 7 must run after Step 6. If Step 6 is re-run (e.g. to fix
> `SERVER_IMAGE`), Step 7 must also be re-run — the topology regeneration produces
> new MACs.

### Step 8: Deploy the Air simulation

```bash
make air-deploy NOZTP=1
```

Creates the Air topology, boots all VMs (4 Cumulus switches + 7 RHCOS nodes +
1 utility VM), and injects switch configs as Node Instructions.

`NOZTP=1` means switch configurations are delivered via Air Node Instructions rather
than a DHCP/ZTP server — no ZTP infrastructure is required.

The simulation reaches ACTIVE state in approximately 2 minutes.

### Step 9: Verify SSH reachability

```bash
make air-ssh-check FIX=1
```

Clears any stale SSH host keys from `~/.ssh/known_hosts` and confirms all nodes
are reachable. Re-run if any nodes are not yet ready.

### Step 10: Push switch configs

```bash
make switch-ztp-deploy NOZTP=1
```

Runs Ansible against all 4 Cumulus switches and applies the fabric configuration
(BGP, EVPN, VLANs, bonds). Takes 2–5 minutes.

### Step 11: Wait for RHCOS nodes to finish booting

```bash
make wait-rhcos-ready
```

This command blocks until every RHCOS node (GPU and k8s) is SSH-reachable **and**
`era-nmstate.service` has applied network config successfully (exit status 0).
It polls every 15 seconds with a 30-minute overall timeout.

RHCOS boot is intentionally slow in Air due to OOB bridge initialization time.
The patched image accounts for this — the full timeline:

| Time from power-on | Event |
|--------------------|-------|
| T + 0 s | All VMs power on simultaneously |
| T + 90 s | RHCOS kernel starts (GRUB delay expires) |
| T ≈ 131 s | Air OOB bridge operational |
| T ≈ 134 s | DHCP lease delivered to OOB interface; DHCP option 121 installs `169.254.169.254/32` host route |
| T ≈ 136 s | DHCP DISCOVER sent — dnsmasq not yet ready; NM schedules 5-minute retry |
| T ≈ 8 min | dnsmasq DHCP ACK delivered with option 121 (169.254.169.254/32 route) |
| T ≈ 8 min | Ignition fetches config from utility metadata server |
| T ≈ 15–17 min | NMState network config applied; node fully booted and SSH-ready |

> **Why 90 s GRUB delay?** NetworkManager's DHCP client starts at kernel boot with a 90 s timeout.
> The Air OOB bridge becomes operational at T≈131 s wall time. With a 30 s GRUB delay, the kernel
> starts at T≈30 s and NM's DHCP window closes at T≈124 s — 7 seconds before the bridge is ready.
> This causes NM to miss the first attempt and enter a ~300 s backoff, which pushes DHCP success
> to T≈424 s — well past the ignition fetch timeout. A 90 s GRUB delay starts the kernel at T≈90 s,
> so the OOB bridge becomes ready 37 s into NM's DHCP window, guaranteeing success on the first try.

---

## Validation

### Run the ping matrix

```bash
make validate-ping-matrix
```

Executes an N×N ping matrix across all expected server-to-server paths.

**Expected result: 80 passed, 0 failed.**

The matrix tests:
- Inband fabric paths (bond0 / bond0.400 VLAN sub-interface)
- GPU rail paths (rail1–rail4 per GPU node)
- Cross-VRF isolation (12 paths correctly blocked — these are counted as expected)

---

## Troubleshooting

### Ignition never fetched (nodes stuck at boot prompt)

Check the utility metadata server is running:
```bash
make air-ssh-check
# then SSH to utility and check:
systemctl status rhcos-metadata
```

Ensure DHCP delivered the `169.254.169.254/32` host route:
```bash
# On an RHCOS node console:
ip route show | grep 169.254
```

If the route is absent, DHCP did not complete before ignition timed out. The 5-minute
timeout should cover this — if it still fails, check that the patched image
(`rhcos-422-openstack-gd`) is the one actually deployed (not a legacy image).

### Ping matrix failures

If fewer than 80 paths pass:

1. Verify NMState applied on all nodes:
   ```bash
   make air-ssh-check
   # SSH to a failing node and check:
   systemctl status era-nmstate
   journalctl -u era-nmstate --no-pager | tail -30
   ```

2. Check bond0.400 sub-interface exists on k8s nodes:
   ```bash
   ip link show bond0.400
   ip addr show bond0.400
   ```

3. Verify switch fabric is up:
   ```bash
   make validate-switch-health
   ```

### SSH key rejected

Confirm the key path in `.era-secrets/air-secrets.yml` and that the `.pub` file exists:
```bash
ansible-vault view .era-secrets/air-secrets.yml | grep air_ssh_key_path
ls -la ~/.ssh/<your-key>.pub
```

If the path is wrong, re-run `make air-setup` to correct it, then redeploy from Step 4.

---

## Background: How RHCOS Ignition Works in Air

### Metadata service endpoint convention

Both cloud-init and RHCOS ignition use `http://169.254.169.254` as a well-known
metadata service address — an industry convention originating from AWS EC2 that every
major cloud platform adopted. Both tools fetch instance configuration from this
address, but they are completely independent tools:

| | cloud-init | RHCOS ignition |
|---|---|---|
| Endpoint | `169.254.169.254/latest/user_data` or OpenStack path | `169.254.169.254/openstack/latest/user_data` |
| Format expected | cloud-config YAML, shell script, etc. | Ignition JSON (version 3.x) |
| When it runs | Early userspace, after full OS boot | Initramfs stage, before pivot root |
| Used by | Ubuntu, CentOS, most Linux distros | RHCOS exclusively |

RHCOS does not use cloud-init. The ignition binary itself (running in initramfs via
`ignition-fetch.service`) makes the HTTP GET to
`169.254.169.254/openstack/latest/user_data`. The "openstack" in the image flavor
name refers to which platform source ignition uses to locate its config — not to
cloud-init.

`rhcos_metadata_server.py` serves raw ignition JSON (version `3.4.0`, with `storage`
and `systemd` stanzas) at the OpenStack metadata path. If a cloud-init-based node
(such as the utility Ubuntu VM) queried that endpoint it would receive ignition JSON
it cannot parse — but it does not, because the utility VM has its own separate
cloud-init datasource.

### Why the OpenStack RHCOS image flavor

RHCOS ships in many platform-specific image formats (`aws`, `azure`, `gcp`,
`ibmcloud`, `kubevirt`, `metal`, `nutanix`, `openstack`, `oraclecloud`, `qemu`,
`vmware`, and others). Of these, only two are qcow2 images compatible with
KVM-based hypervisors:

| Format | Ignition config source |
|---|---|
| `openstack` | `http://169.254.169.254/openstack/latest/user_data` |
| `qemu` | QEMU `fw_cfg` device |

Air runs KVM-backed VMs. The `qemu` format cannot be used because Air does not
populate the QEMU `fw_cfg` device, so ignition would never receive its config.
The `openstack` format reads ignition from the HTTP metadata endpoint, which the
utility VM spoof-serves — this is the correct choice for Air. The patched image
`rhcos-422-openstack-gd` is the `openstack` format with two additional patches
applied (see below).

### Why a patched image is needed

Two patches in the boot image are required to survive Air's OOB bridge timing:

| Patch | What it changes | Why |
|-------|----------------|-----|
| GRUB boot delay (90 s) | `set timeout=90` + `set timeout_style=menu` in `/boot/grub2/grub.cfg` | Delays kernel start so that the Air OOB bridge (ready at T≈131 s wall time) becomes available 37 s into NetworkManager's 90 s DHCP window, guaranteeing a first-attempt DHCP success. A shorter delay (e.g. 30 s) causes NM's DHCP to time out 7 s before the bridge is ready, triggering a ~300 s retry backoff and pushing DHCP delivery past the ignition fetch timeout. |
| Ignition fetch timeout (12 m) | `--fetch-timeout 12m` replacing `${IGNITION_ARGS}` in `ignition-fetch.service` inside the initramfs zstd CPIO | The compiled-in default is 2 minutes. `${IGNITION_ARGS}` is never populated by the ignition generator (it only writes `PLATFORM_ID`), so the timeout silently remains 2 m. The ERA metadata server (dnsmasq + rhcos_metadata_server.py) starts on the utility VM alongside the RHCOS nodes; the utility takes ~3 minutes to run its Node Instruction script, and NetworkManager's DHCP retry interval after an unanswered first attempt is ~5 minutes. In practice the first successful DHCP ACK with option 121 (the `169.254.169.254/32` host route) arrives ~8 minutes after power-on — well beyond the old 5-minute window. 12 minutes provides sufficient margin. |

### GPU NIC configuration pipeline (Air digital twin)

Understanding where GPU rail interface configuration comes from is non-obvious. The
pipeline has three distinct stages:

**Stage 1 — OCP inventory (eth0–eth2 only)**

`make generate-ocp NIC_MODE=kvm` writes per-node host_vars files under
`output/<ARCH>/<SITE>/ocp/inventory/host_vars/`. For GPU nodes these contain MACs for
eth0 (OOB), eth1, and eth2 (bond0 members) only. That is all the OCP inventory itself
needs.

**Stage 2 — Full MAC table from topology JSON (eth0–eth6)**

At `make air-deploy NOZTP=1` time, `air-deploy.py` reads the full topology JSON and
builds a complete `node_iface_macs[node][eth_name] = mac` table for all interfaces
eth0–eth6 on every node. The topology JSON is the authoritative MAC source. eth3–eth6
(GPU rails 1–4) are present there but are not written into the OCP host_vars.

Interface assignments for GPU nodes:

| Interface | Role |
|---|---|
| eth0 | OOB management |
| eth1 | bond0 member 1 (CPU VLAN) |
| eth2 | bond0 member 2 (CPU VLAN) |
| eth3 | GPU rail 1 |
| eth4 | GPU rail 2 |
| eth5 | GPU rail 3 |
| eth6 | GPU rail 4 |

**Stage 3 — NMState config built and embedded in ignition**

`air-deploy.py` passes the complete eth0–eth6 MAC set into
`build_rhcos_nmstate_config()` in `scripts/airlib/rhcos.py`. This produces a full
NMState network config for the node:

- `bond0` (active-backup, CPU VLAN IP, metric 100)
- OOB interface matched by MAC, metric 200 (lower priority than bond0)
- rail1–rail4: static IPs matched by MAC, PBR routing tables 901–904, source-IP rules
  (`ip-from: <rail_ip>/32`)
- NM keyfile stubs for all non-OOB interfaces (prevents initrd NM from attempting
  DHCP on fabric links, eliminating a 28-minute `nm-wait-online-initrd` timeout)

`generate_rhcos_ignition_payload()` wraps the NMState config into an ignition JSON:

- NMState YAML lives in `storage.files`
- `era-nmstate.service` lives in `systemd.units` and applies the NMState config
  post-pivot

The ignition JSON is base64-encoded and written into the utility Node Instruction as:

```bash
echo '<b64>' | base64 -d > /opt/era/ignition/<node-name>.ign
```

An `ip-map.json` maps each node's OOB IP → its `.ign` path.
`rhcos_metadata_server.py` reads this map and serves the correct `.ign` at
`169.254.169.254/openstack/latest/user_data` based on the requesting node's IP.

The RHCOS ignition payloads are never written to the local `output/` directory —
they exist only on the utility VM at `/opt/era/ignition/`.

### Day-2 NNCP YAMLs vs. ignition NMState payload

`make generate-ocp` also produces
`output/<ARCH>/<SITE>/ocp/day2/nncp-<node>-gpu-rails.yaml` —
`NodeNetworkConfigurationPolicy` CRs for the OpenShift NMState operator. These are
applied post-install to a running OCP cluster:

```bash
oc apply -f output/<ARCH>/<SITE>/ocp/day2/
```

The NNCP YAMLs and the ignition NMState payload express **equivalent GPU rail
configuration** but are generated independently from the same source data
(Excel → inventory). Neither is derived from the other.

| Output | Generated by | Delivery mechanism | Context |
|---|---|---|---|
| `nncp-...-gpu-rails.yaml` | `generate-ocp-inventory.py` | `oc apply` via NMState operator | Bare-metal OCP cluster post-install |
| Ignition NMState payload | `build_rhcos_nmstate_config()` in `rhcos.py` | `era-nmstate.service` at first boot | Air digital twin simulation |
