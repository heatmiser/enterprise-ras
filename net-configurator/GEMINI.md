# Net-Configurator Workspace & KVM Digital Twin Guidance

## KVM Digital Twin Architectural Conventions

### 1. NIC Naming (qcow2 Mode)
- **CentOS Stream 9 GenericCloud qcow2** images use `eth0` through `eth6` PCI naming:
  - `eth0`: Management interface (`br-dt-mgmt`, static IP via `cloud-init` network-config)
  - `eth1`: Inband interface (`br-dt-inband`, `bond0` member 1)
  - `eth2`: Support/Control interface (`br-dt-support`, `bond0` member 2)
  - `eth3`–`eth6`: GPU Rail interfaces (`br-dt-rail1` through `br-dt-rail4`)

### 2. VM Sizing & Capacity
- Defaults set to 2 vCPUs and 2048 MB RAM per VM (16 GiB total across all 8 VMs: 1 router + 4 GPU nodes + 3 k8s control_plane nodes).

### 3. Dynamic Gateway Extraction
- `playbooks/deploy-kvm.yml` slurps generated OCP `host_vars/` to dynamically extract router gateway IPs (`kvm_router_support_ip`, `kvm_router_inband_ip`) and prefix lengths (`kvm_router_support_prefix`). Avoid hardcoding gateway subnets in playbooks.

### 4. Outbound NAT & Package Management
- KVM host management bridge (`192.168.200.0/24`) requires firewalld masquerading and trusted zone source routing for VM internet access.
- Guest VMs require `crb` repository enabled before `dnf install -y nmstate` can succeed.

### 5. Ansible & Python 3.12 Environment
- Requires `setuptools>=70.0.0` in `requirements.txt` for `ansible.posix.firewalld` compatibility on Python 3.12.
- Local virtual environments executing host firewalld/libvirt bindings must be created with `python3 -m venv --system-site-packages .venv` (setting `include-system-site-packages = true` in `pyvenv.cfg`).

### 6. Verification Workflows
- **Deployment**: `make deploy-kvm ARCH=<arch> SITE=<site> KVM_BASE_IMAGE=<path>`
- **Validation**: `make validate-kvm ARCH=<arch> SITE=<site>` (Runs $N \times N$ cross-server ping matrix across inband `bond0`, GPU rails, and gateways following `testbed-validate.yaml` model)
- **Teardown**: `make teardown-kvm`
