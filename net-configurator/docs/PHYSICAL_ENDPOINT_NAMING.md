# Physical cluster endpoint names

The workbook `Nodes.Name` identifies a physical server. It remains the key for
Wire Map rows, Ironic nodes, BMC credentials, inspection authorization, report
directories, inventory host keys, and generated filenames. DNS names identify
two separate endpoints: its BMC and its installed RHCOS CPU bond.

Site-local `input/<arch>/<site>/ocp-settings.yml` supplies the explicit
`node_endpoints` mapping generated from reviewed workbook Roles. Construction
uses the terminal numeric suffix of the physical key, without prefix replacement.
The workbook continues to own numeric
addresses and `Settings.dns_servers`; the generated ERA device data supplies
the BMC IP from `eth0_ip` and RHCOS IP from `bond_ip1` or `bond_ip`.
The physical meaning of `eth0_ip` is the BMC address; it does not add an eth0
interface to ABI networking.

## Illustrative site configuration

All names, addresses, and membership choices below are documentation examples.
Replace them with reviewed site values before generating deployment inputs.
The address ranges are reserved for documentation.

Set the physical workbook's Settings values to:

| Setting | Value |
| --- | --- |
| `ocp_cluster_name` | `cluster-01` |
| `ocp_cluster_domain` | `example.com` |
| `bmc_dns_subdomain` (MANAGEMENT) | `mgmt-01` |
| `dns_servers` | `203.0.113.10` |
| `ocp_api_vip` | `198.51.100.254` |
| `ocp_ingress_vip` | `198.51.100.253` |

Set `Nodes.Role` (column M) independently of `Nodes.Function`:

| Role | Generated OCP role | Endpoint label |
| --- | --- | --- |
| `control` | `control_plane` | `control-<digits>` |
| `gpu` | `worker_gpu` | `gpu-<digits>` |
| `storage` | `worker_storage` | `storage-<digits>` |
| `worker` | `worker` | `worker-<digits>` |
| `infra` | `infra` (installer worker) | `infra-<digits>` |

`Function` continues to select fabric profiles and addressing. Role does not
configure Kubernetes labels, taints, or infrastructure workload placement.
General workers use the existing CPU/support bond network generation.

After importing the reviewed workbook and regenerating ERA inventory, run:

make init-ocp-settings ARCH=<arch> SITE=<site>

With explicit Roles, this generates `node_roles` and `node_endpoints`. Every
enabled OCP inventory server must have a Role; mixed blank/assigned Roles fail.
Disabled and Air-documentary rows remain excluded. All-blank or absent Roles
retain legacy inventory-group role inference and physical-key names, unless
an existing explicit endpoint mapping is preserved.

`Nodes.Name` must end in `-<digits>`: `server-cp-01` with Role `control` yields
`control-01`, preserving `01`. Exactly one dash joins Role and digits; row order
never determines numbering. Missing suffixes and duplicate endpoint names fail.
Explicit Role mode requires workbook cluster name and domain. A blank
`bmc_dns_subdomain` uses `mgmt-01`.

For this example, assign `control`, `worker`, and `worker` to the three rows.
The resulting mapping is:

```yaml
node_endpoints:
  server-cp-01:
    bmc_fqdn: control-01.mgmt-01.example.com
    rhcos_fqdn: control-01.cluster-01.example.com
  server-worker-01:
    bmc_fqdn: worker-01.mgmt-01.example.com
    rhcos_fqdn: worker-01.cluster-01.example.com
  server-worker-02:
    bmc_fqdn: worker-02.mgmt-01.example.com
    rhcos_fqdn: worker-02.cluster-01.example.com
```

| Physical key | BMC IP | RHCOS bond IP | Initial ABI |
| --- | --- | --- | --- |
| server-cp-01 | 192.0.2.11 | 198.51.100.11 | Yes |
| server-worker-01 | 192.0.2.21 | 198.51.100.21 | Yes |
| server-worker-02 | 192.0.2.22 | 198.51.100.22 | No; later Day 2 |

This abbreviated example demonstrates endpoint mapping and initial versus
Day-2 membership; it is not a complete cluster installation configuration.

Mappings cover all declared OCP nodes, including Day-2 nodes. Initial inclusion
still comes solely from `Nodes -> Include in Initial ABI`; a DNS entry does not
include a node in initial installation. Names must be unique, canonical lowercase
FQDNs; RHCOS names belong to `<cluster.name>.<cluster.domain>` and fit in 63 bytes.
Node addresses and API/ingress VIPs must be distinct. These checks run before
OCP output is written. Existing settings without `node_endpoints` retain the
previous physical-key hostname behavior. The mapping applies to `NIC_MODE=real-hw`.

`make init-ocp-settings ... FORCE=1` preserves an existing explicit cluster name
unless the workbook supplies `ocp_cluster_name`, and preserves `node_endpoints`.
It rejects node coverage or cluster-domain changes incompatible with that mapping
before overwriting the file. In explicit Role mode it also rejects conflicts
between preserved endpoints and workbook-generated names; operators must review
and resolve such conflicts before regeneration. Other bootstrap behavior remains in effect: roles,
disk overrides, and credentials paths require operator review after regeneration.

## Generated handoff

From the checkout's `enterprise-ras/net-configurator` directory, generate physical
OCP inputs using the selected architecture and site:

Substitute the architecture and site placeholders before executing:

make generate-ocp ARCH=<arch> SITE=<site> NIC_MODE=real-hw

Review these outputs under `output/<arch>/<site>/ocp/`:

- `endpoint-map.yaml`: physical keys, BMC/RHCOS names and numeric addresses,
  plus `rhcos_to_physical` for collection consumers.
- `dns/dnsmasq-records.conf`: BMC and RHCOS `host-record` entries (A and PTR),
  API/API-int records, and wildcard apps address records. It is an include for
  the dnsmasq service providing cluster DNS, whether local or external; it does
  not change service listeners, upstream
  forwarding, firewall policy, or install anything.
- `dns/bind-records.conf` **(planned; generation not yet implemented)**: a
  `named.conf` include declaring the generated forward and reverse zones and
  referencing their companion zone files under `dns/bind/`.
- `dns/bind/` **(planned; generation not yet implemented)**: companion zone files
  containing BMC and RHCOS A records, API/API-int A records, wildcard apps A
  records, and BMC/RHCOS PTR records. Complete zone files require SOA and NS
  records plus a serial-management policy; those inputs remain part of the
  subsequent generator implementation scope.
- `inventory/`: keys and filenames remain physical; `ansible_host` is the RHCOS
  FQDN. Host vars separately expose `physical_server_name`, `rhcos_hostname`,
  `rhcos_ip`, `bmc_host`, and `bmc_ip`. The ERA inventory remains separate.
- `agent-config.yaml`: initial-install RHCOS FQDNs and the existing CPU-only
  network/disk inputs; no BMC address appears in installed hostname fields.
- `day2/workers/nodes-config.yaml`: deferred workers' RHCOS FQDNs and network inputs.
- `day2/nncp-<physical-key>-gpu-rails.yaml`: selectors target the RHCOS FQDN.

### Collection identity handoff

For collection `playbooks/stage_fleet_abi.yml` or the ISO preparation phase of
`playbooks/provision_cluster.yml`, supply `ocp_endpoint_map_path` pointing to
the reviewed `output/<arch>/<site>/ocp/endpoint-map.yaml` on the execution host.
The caller validates the document and supplies its `rhcos_to_physical` mapping
to `fleet_abi_staging_hostname_map` and `ocp_agent_iso_build_hostname_map`.
Conflicting explicit role mappings are rejected before manifest processing.
Omitting the path preserves existing role inputs and legacy hostname behavior.

The OpenShift installer reads `agent-config.yaml` and `install-config.yaml`;
it does not read `endpoint-map.yaml`. The map connects RHCOS manifest names to
physical-keyed disk/NIC evidence in our automation. Extra Day2 entries do not
change initial ABI membership. Staging still requires eligible inspection
evidence. The provisioning reference playbook performs hardware operations and
must not be used as a local naming test.

`make generate-ocp-iso` invokes the installer directly. It does not invoke
collection staging or apply observed inspection facts through this handoff.
Its successful build alone does not establish fleet reconciliation or eligibility.

After reviewing records against the site's DNS design, the operator can install
the generated include into the existing dnsmasq configuration, syntax-check
dnsmasq, and reload it. Verify A and PTR for both endpoints of every node,
API/API-int at 198.51.100.254, and an arbitrary apps name at 198.51.100.253.
Use endpoint FQDNs explicitly and review any unqualified `/etc/hosts` aliases
for ambiguity between BMC and RHCOS addresses. An ABI ISO generated before a
naming change must be regenerated after the complete naming handoff is ready.


### Planned BIND handoff

The BIND output will represent the same endpoint mapping as the dnsmasq output.
For the illustrative configuration, forward zones cover
`mgmt-01.example.com` and `cluster-01.example.com`; reverse zones cover the
reviewed BMC and RHCOS address ranges. Reverse-zone boundaries must follow the
site's actual DNS authority and delegation rather than assuming that every
address range is a /24.

Before loading generated files, the operator must review zone ownership,
existing records, authoritative server names, SOA parameters, serial handling,
and the deployed paths referenced by `bind-records.conf`. If an existing service
already owns a zone, its records must be integrated through that service's
established configuration process rather than declaring a duplicate zone.

Validate the configuration and each zone with `named-checkconf` and
`named-checkzone` before activation. After activation, verify the same endpoint
A/PTR, API/API-int, and wildcard apps answers described above. Generated files
will not install BIND or change listeners, recursion policy, forwarding, or
firewall rules. BIND generation and service deployment remain pending separate
implementation approval.

## Inspection and collection boundary

Collection `ocp_nodes[].name` and the BMC credential dictionary remain keyed by
the physical server. During fleet preparation, `bmc.host` can be the reviewed
numeric BMC IP or mapped BMC FQDN. With endpoint mapping configured, preparation
uses the mapped FQDN and requires system IPv4 name resolution to return exactly
the workbook BMC IP. Wrong or missing resolution fails before creating the run
bundle. This performs a resolver query, not a Redfish operation.

The collection naming handoff is a subsequent implementation phase. Its ABI
staging/report and disk-policy lookups must consume the physical/FQDN mapping
before these outputs are used for the physical install workflow. Generation
does not establish inspection eligibility, boot nodes, or build an ISO.
DSX Air DNS functionality and deployment validation are deferred.

## Physical inspection to ABI ISO eligibility handoff

Generate the reviewed real-hardware OCP inventory and both candidate manifests
before preparing the next fleet inspection. Preparation checks agent hostnames,
roles, MACs, network configuration and root-device hints against the workbook and
settings, and checks install-config cluster identity, replica counts, VIPs and
machine network. It seals the candidate file hashes and endpoint map with the
source inputs. Pull secrets stay in the private candidate file; only its hash is
included in the non-secret handoff.

Inspection-only preparation without candidate manifests remains supported.
Such a run cannot grant ISO eligibility. A subset inspection likewise cannot
grant eligibility for a larger initial-install host list.

After every selected physical node validates and terminal/shared-runtime cleanup
succeeds, the collection finalizes `summary.yaml` and writes `eligibility.yaml`.
Eligibility requires exact initial-install membership and candidate binding.
The receipt binds the candidate hashes to the durable aggregate and node evidence.
Synthetic evidence never grants physical eligibility. No RAID verification is
added unless the reviewed plan explicitly requests a storage policy.

`make generate-ocp-iso ARCH=<architecture> SITE=<site> FLEET_RUN_ID=<reviewed-run>`
requires this receipt. It verifies the sealed inputs, evidence hashes, physical
validation, exact node membership, disk identities and cleanup before staging any
installer inputs. Changed workbook, settings, candidates or evidence require a
new preparation and physical inspection. Archived evidence whose files have been
sanitized cannot be used as an executable handoff.

The default installer is the immutable EE image resolved during inspection
preflight. An explicit `OCP_EE_IMAGE` must match that digest or the sealed site's
configured image; the build still uses the prepared digest. An explicitly selected
`OPENSHIFT_INSTALL` must report the same version and release image as preflight.
The target does not implicitly select a local installer from PATH.

Validated candidate bytes are copied into a new private directory:
`output/<architecture>/<site>/ocp-iso/<run-id>/`. Existing installer directories
are not reused. The resulting ISO is `agent.x86_64.iso` in that directory.
ISO generation does not authorize installation boot or change BMC power state.

The direct Make handoff uses the current fleet evidence layout. Integrating the
collection's separate `stage_fleet_abi.yml` role with that layout remains a follow-up.

### To-Do / WIP: end-to-end orchestration

A future single Make target will coordinate DSX Air deployment, complete fabric
verification, physical inspection, eligibility, ABI ISO build, installation boot,
installation monitoring and final cluster verification. This orchestration is not
implemented by the eligibility handoff. Full four-rail logical fabric validation,
stage-specific physical authorization and installation verification remain required
implementation work.
