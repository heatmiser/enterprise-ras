# KVM Digital Twin — Test Deployment Instructions

This guide describes how to deploy and test the KVM Digital Twin environment using `net-configurator`.

---

## 0. Prerequisites

On the KVM test host (RHEL 9 / AlmaLinux 9 / Fedora):

```bash
# KVM stack
dnf install -y qemu-kvm libvirt virt-install genisoimage

# Start and enable libvirt
systemctl enable --now libvirtd

# Python 3.11+
dnf install -y python3 python3-pip git

# Red Hat pull secret (obtained from cloud.redhat.com)
mkdir -p ~/.era-secrets
# Save your Red Hat pull secret JSON file to ~/.era-secrets/pull-secret.json

# SSH key generation for OCP nodes
ssh-keygen -t ed25519 -C "ipp5-ocp" -f ~/.ssh/id_ed25519_ipp5-ocp
```

> **Important:** Run `make deploy-kvm` on the KVM host itself. Play 2 connects to the VMs via `192.168.200.x` — those IPs only exist on the KVM host's bridges. A remote Ansible control node cannot reach them without additional routing.

### Estimated RAM required
- 8 VMs × 2 GiB = **16 GiB minimum**

---

## 1. Get the repo

```bash
# Fresh clone
git clone <repo-url> enterprise-ras
cd enterprise-ras/net-configurator
```

---

## 2. Python / Ansible environment

```bash
cd enterprise-ras/net-configurator

python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install -r requirements.txt

# Verify ansible-core is >= 2.20.4
ansible --version
```

> **Tip:** Add `source .venv/bin/activate` to your shell rc file, or prefix all `make` calls with `source .venv/bin/activate &&`.

---

## 3. Stage the base image

The playbook needs a RHEL 9 or AlmaLinux 9 `GenericCloud` qcow2 image with `cloud-init`. AlmaLinux is freely available:

```bash
# AlmaLinux 9 GenericCloud (substitute latest minor version if needed)
curl -LO https://repo.almalinux.org/almalinux/9/cloud/x86_64/images/AlmaLinux-9-GenericCloud-latest.x86_64.qcow2

# Move to libvirt images dir
mv AlmaLinux-9-GenericCloud-latest.x86_64.qcow2 /var/lib/libvirt/images/almalinux9-base.qcow2
```

---

## 4. Generate the ERA and OCP inventories

These run the Python scripts that produce the NMState/NNCP configs that `deploy-kvm` will consume. You need the ERA Excel workbook (`input/2-8-5-200/kicktires/2-8-5-200.xlsx`) in the repo.

```bash
source .venv/bin/activate

# Step 1: ERA inventory (switch configs, topology)
make generate ARCH=2-8-5-200 SITE=kicktires

# Step 2: OCP inventory + NMState/NNCP files
make generate-ocp ARCH=2-8-5-200 SITE=kicktires
```

Verify that output files exist before proceeding:

```bash
ls output/2-8-5-200/kicktires/ocp/inventory/hosts.yml
ls output/2-8-5-200/kicktires/ocp/inventory/host_vars/
ls output/2-8-5-200/kicktires/ocp/day2/
```

---

## 5. Deploy the KVM digital twin

```bash
make deploy-kvm ARCH=2-8-5-200 SITE=kicktires \
  KVM_BASE_IMAGE=/var/lib/libvirt/images/almalinux9-base.qcow2
```

This command will prompt for your sudo password (`--ask-become-pass`). If running directly as `root`, simply press **Enter**.

### Optional overrides

| Variable | Default | Purpose |
| :--- | :--- | :--- |
| `KVM_BASE_IMAGE` | `/var/lib/libvirt/images/rhel9-base.qcow2` | Path to qcow2 base image |
| `KVM_IMAGE_DIR` | `/var/lib/libvirt/images/digital-twin` | Working dir for overlays and ISOs |

---

## 6. What the playbook does (what to watch)

### Play 1 — KVM host (~5 min)
1. Validates OCP output and base image exist.
2. Generates `/root/.ssh/dt-key` (ed25519) if absent.
3. Creates 7 Linux bridges: `br-dt-mgmt`, `br-dt-inband`, `br-dt-support`, `br-dt-rail1–4`.
4. Assigns `192.168.200.254/24` to `br-dt-mgmt`.
5. Creates thin qcow2 overlays + cloud-init ISOs for all 8 VMs.
6. Deploys via `virt-install --noautoconsole --import`.

### Play 2 — VMs (~10 min, dominated by cloud-init wait)
1. Waits up to 600s for SSH on each VM (30s delay, 10s sleep between attempts).
2. Waits for `cloud-init status --wait`.
3. Reads `host_vars/<node>.yml`, extracts `networkConfig:`, substitutes `eth1`→`enp2s0` … `eth6`→`enp7s0`.
4. Applies Stage 1 NMState (`nmstatectl apply` — bond0).
5. GPU nodes only: reads `day2/nncp-<node>-gpu-rails.yaml`, extracts `spec.desiredState`, applies Stage 2.
6. Pings the bond0 gateway and reports `REACHABLE`/`UNREACHABLE`.

---

## 7. Verify after playbook completes

```bash
# Check all 8 VMs are running
virsh list --all

# Check bridges exist
ip link show type bridge | grep br-dt

# SSH into a GPU node manually (key at /root/.ssh/dt-key)
ssh -i /root/.ssh/dt-key -o StrictHostKeyChecking=no \
  cloud-user@192.168.200.11

# Inside the VM — verify bond0
ip addr show bond0
ip route show
```

### Automated Cross-Server Network Validation

To run an automated All-to-All ($N \times N$) cross-server ping matrix across inband (`bond0`), GPU rails, and gateway reachability following the `testbed-validate` model:

```bash
make validate-kvm ARCH=2-8-5-200 SITE=kicktires
```

---

## 8. Known gotchas

| Issue | Cause | Fix |
| :--- | :--- | :--- |
| `genisoimage: command not found` | Package not installed | `dnf install -y genisoimage` |
| `virsh` shows VMs in shut off state | Cloud-init OOM or disk error | `virsh console <vmname>` to inspect |
| Play 2 SSH timeout (>600s) | Cloud-init blocked on `dnf install nmstate` (slow mirrors) | Re-run playbook — it is idempotent via `creates:` guards |
| `eth1` not found in `host_vars` | `generate-ocp` output incomplete | Re-run `make generate-ocp` and check for errors |
| Bond0 gateway `UNREACHABLE` | `router-vm` VRF `nmcli` commands failed | `virsh console dt-router` → `nmcli connection show` |
| SSH key auto-detection | `kvm_ssh_key_path` not specified | Automatically inherits private key path from `ocp-settings.yml` (`ssh_key_path`), defaulting to `~/.ssh/dt-key` |

---

## 9. Teardown

To clean up all deployed VMs, virtual bridges, and storage images:

```bash
make teardown-kvm
```

### Manual Teardown Fallback

If `make teardown-kvm` is unavailable, execute the following shell commands directly on the KVM host:

```bash
# Destroy and undefine all digital twin VMs
for vm in dt-router $(virsh list --all --name | grep -E 'ipp5|gpu|k8s'); do
  virsh destroy "$vm" 2>/dev/null
  virsh undefine "$vm" --remove-all-storage 2>/dev/null
done

# Delete bridges
for br in br-dt-mgmt br-dt-inband br-dt-support br-dt-rail1 br-dt-rail2 br-dt-rail3 br-dt-rail4; do
  ip link set "$br" down 2>/dev/null
  ip link delete "$br" 2>/dev/null
done

# Remove working directory
rm -rf /var/lib/libvirt/images/digital-twin
```
