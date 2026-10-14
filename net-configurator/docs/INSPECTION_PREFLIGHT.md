<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: MIT -->

# Controlled Bare-Metal Inspection Preflight

`make prepare-inspection-preflight` is the net-configurator operational driver
for the collection's input-preparation role. It is part of the controlled
one-node inspection path, not an OpenShift install workflow.

## Preconditions

The normal site progression is:

1. Import the physical-MAC workbook and generate the site output.
2. Complete the Air fabric validation for the declared topology.
3. Generate the selected node's inspection-only NMState document and its
   sibling MAC-pinned CPU NIC identity artifact.
4. Ensure the imported workbook's Settings-tab `ocp_version` is an exact
   three-part OCP release. The workbook at
   `input/<arch>/<site>/<arch>.xlsx` is the sole declared version source;
   `ocp-settings.yml` is a generated projection and is not read here.

The generator writes the NIC identity artifact at
`output/<arch>/<site>/ocp/inspection/early-network/<candidate>.yaml` from the
workbook-derived CPU `nic_alias_map`. Its ordered `{name, mac}` values must exactly
match the inspection NMState bond members. The collection uses it to render
dracut `ifname=<name>:<MAC>` arguments before the IPA live rootfs is fetched.

The driver resolves the configured EE image to an immutable digest, then reads
`openshift-install version` from that exact image. The EE-reported installer
version must equal that workbook value; its embedded immutable release payload is
the release used in the artifact. It reads the candidate's generated inspection
NMState, then uses `ip -j route get` for its static bond address to derive the
bastion callback source IP and interface.
The driver materializes the shared-vault `ocp_pull_secret` at its controlled
mode-0600 default, `~/.era-secrets/pull-secret.json`.

The reviewed collection must be installed at
`~/.ansible/collections/ansible_collections/rhvp/baremetal_ocp`, or callers
must set `RHV_BAREMETAL_OCP_COLLECTIONS_PATH` or `RHV_BAREMETAL_OCP_ROOT`.
Its ignored site `cluster-vars.yaml` supplies the candidate declaration and
BMC endpoint/CA policy. The driver intentionally does not load BMC credentials.

## Invocation

    make generate-inspection-nmstate ARCH=<arch> SITE=<site> \
      INSPECTION_CANDIDATE=<candidate>

    make prepare-inspection-preflight ARCH=<arch> SITE=<site> \
      INSPECTION_CANDIDATE=<candidate>

The target decrypts `.era-secrets/air-secrets.yml` using the saved
`.era-secrets/vault-pass` when available, otherwise Ansible prompts. It writes
the reviewable, non-secret artifact to:

`<collection-root>/inventories/<site>/preflight-vars.yaml`

## Gate-6 Controlled Inspection

After the preflight artifact and the collection's Gate-4 and Gate-5 checks have
been reviewed, the separately authorized physical action is driven from this
project:

    make inspect-controlled-candidate ARCH=<arch> SITE=<site> \
      INSPECTION_CANDIDATE=<candidate> \
      INSPECTION_AUTHORIZE_PHYSICAL_BOOT=<candidate>

The authorization value must exactly equal the selected candidate. Make derives
three stable single-node R&D destinations under
`output/<arch>/<site>/reports/inspection/test/`:

- `<candidate>-inventory.yaml`
- `<candidate>-failure.yaml`
- `<candidate>-cleanup.yaml`

Any existing destination blocks execution before the physical lifecycle starts,
including a prior failure or cleanup artifact without an inventory report.
Preserve prior evidence explicitly before reusing these test names; the target
does not rename, archive, or delete historical reports.

Optional `INSPECTION_REPORT_PATH`, `INSPECTION_FAILURE_REPORT_PATH`, and
`INSPECTION_CLEANUP_REPORT_PATH` overrides must be distinct absolute `.yaml`
paths outside `/tmp`, without parentheses. If only the inventory path is
supplied, sibling names replace its trailing `-inventory.yaml` with
`-failure.yaml` and `-cleanup.yaml`; a different stem gets these suffixes appended
after removing its extension. No timestamp shell expression is needed.
The wrapper consumes the prepared `preflight-vars.yaml`, collection
`secrets.yaml`, and collection bootstrap inventory at runtime. It maps the
prepared one-node declaration to the collection's `inspect_cluster.yml`; it
does not create a second release, media, NMState, callback, or BMC policy.

The report is non-secret opaque evidence, mode `0640`, and must be retained
for review before any ABI disk, NIC, or LLDP reconciliation work. The
collection owns the physical lifecycle and its `always` teardown: report
persistence, virtual-media detach, BMC postcondition verification, and
ephemeral Ironic/customizer cleanup.

## Fleet inspection through net-configurator

### Optional native all-disk metadata-cleaning preparation

`make prepare-fleet-wipe` creates a fresh non-executable preparation from eligible
physical inspection. Its scope is **all native eligible disks and partitions** on
the selected nodes. Review `review.txt`; installation-volume hints do not limit
this operation and no operator disk exclusions are supplied. IPA's built-in
safeguards remain active. Preparation does not boot nodes or wipe storage.


### Inputs and preparation

Run from `enterprise-ras/net-configurator` on the bastion with the project's
Python/Ansible venv active. Import the reviewed site workbook and run `make generate`
first. The adapter reads the canonical `input/<arch>/<site>/<arch>.xlsx`, generated
ERA inventory, explicit OCP roles/disk policy in `ocp-settings.yml`, and the
collection's `inventories/<site>/cluster-vars.yaml`. Workbook and OCP settings
versions must agree; the existing preflight role checks the EE installer against
the exact workbook version. All OpenShift utilities come from that EE container.

Choose and retain a fresh UTC `FLEET_RUN_ID` in `YYYYMMDDTHHMMSSZ` format. Supply
an explicit comma-separated `FLEET_NODES` list; there is no automatic whole-cluster
selection. The first reviewed test selection is K8S-01 and GPU-01, concurrency 2.
This selection and the commands below do not grant physical execution authority.

    FLEET_RUN_ID=$(date -u +%Y%m%dT%H%M%SZ)

    make prepare-fleet-inspection \
      ARCH=2-8-5-200 \
      SITE=rhaifn02 \
      FLEET_RUN_ID="$FLEET_RUN_ID" \
      FLEET_NODES=ipp5-285-rh-k8s-01,ipp5-285-rh-gpu-01 \
      FLEET_CONCURRENCY=2

Preparation creates a new directory at
`output/<arch>/<site>/ocp/inspection/fleet/<run-id>/` containing:

- `plan.yml`: exact nodes, BMC endpoints, CPU interface MACs, desired Wire Map,
  by-path disk hints, concurrency and timeouts, plus source fingerprints.
- `nmstate/<node>.yaml` and `early-network/<node>.yaml`: callback-only `bond0`
  networking and MAC-pinned CPU NIC names. OOB and GPU rails are excluded.
- `hosts.yml`: one local bootstrap, separate from the inspected nodes.
- `callback-routes.yaml`: route observations for every selected callback address.
- `preflight-vars.yaml`: pinned release/EE and shared runtime inputs, prepared
  once using the first selected node. Its one-node candidate list is not the
  fleet selection or execution authorization.
- `prepared.yaml`: hashes binding the plan, network files, routes, runtime inputs
  and bootstrap inventory. Changes to source inputs also invalidate the handoff.

Preparation queries local routes and EE metadata and materializes the existing
shared-vault pull secret; it does not contact BMCs, start Ironic or boot nodes.
Every selected callback must route through the same bastion source IP/interface.
Existing preparation directories are refused. Preserve a failed preparation and
choose a fresh run ID after addressing its failure; existing artifacts are not removed.

### Review and separate physical authorization

Review `plan.yml`, networking, callback routes and pinned provenance before
authorizing the exact run ID and node list. Default timeouts are manageable 300s,
inspection 1200s, API fetch 30s and total run 3600s. Review their suitability for
the selected node count and concurrency before execution.

After fresh operator approval for this exact physical run:

    make inspect-fleet \
      ARCH=2-8-5-200 \
      SITE=rhaifn02 \
      FLEET_RUN_ID="$FLEET_RUN_ID" \
      FLEET_AUTHORIZE_RUN="$FLEET_RUN_ID" \
      FLEET_AUTHORIZE_NODES=ipp5-285-rh-k8s-01,ipp5-285-rh-gpu-01

The adapter rejects missing or mismatched run/node authority, changed prepared
inputs and existing evidence roots before entering the collection lifecycle.
It reads the collection site's vaulted `secrets.yaml` and maps only the selected
`bmc_credentials` entries to `fleet_inspection_driver_bmc_credentials` under
`no_log`. The collection independently validates physical authorization,
prepared networking and exclusive ownership before shared service startup.
No node boot retries or expanded selection are implied by an earlier approval.

The resulting evidence root is
`output/<arch>/<site>/reports/inspection/<run-id>/`. Per-node evidence lives at
`<node>/inventory.yaml`, raw inventory/port JSON, optional `storage.yaml`, and
terminal/cleanup evidence. Run-level `index.yaml`, `summary.yaml` and `runtime.yaml`
record collection, reconciliation and teardown results. A completed lifecycle
does not imply that reconciliation passed; review these files before proceeding.
Fresh Wire Map mismatches fail validation while preserving observed evidence and
terminal cleanup. The adapter retains spreadsheet expectations and never replaces
them with historical LLDP observations.

The adapter now includes all selected CPU, GPU rail and host OOB NIC Wire Map
rows, regardless of `Display in Air`. Each record carries `purpose: cpu`, `gpu`
or `host_oob`. GPU and host OOB rows require reviewed aliases, MACs and switch/port
assignments matching the generated inventory. iDRAC/BMC NIC/Port rows are excluded:
those endpoints are not host NICs visible to the inspection ramdisk.

CPU alias/MAC identity remains strict. GPU and host OOB aliases are resolved to
the unique PCI-backed observed NIC by reviewed MAC; no OS rename is performed.
Every required physical link must have explicit carrier up, LLDP and a unique
MAC-bound Ironic Port whose persisted attachment agrees with the workbook.
Findings record the purpose, reviewed alias, observed OS name, carrier and
attachment failures. Missing/duplicate evidence, down carrier, wrong attachments
and unplanned physical LLDP links fail reconciliation. Initial inspection/ABI
network configuration remains CPU-only; GPU configuration stays Day2.

New preparation bundles must be generated to include these full link records;
previous sealed bundles are not upgraded or rewritten. Revised historical K8S
names require service-tag/MAC correlation before comparing old reports.

RAID checks run only when a selected
collection `ocp_nodes` entry contains a separately reviewed `storagePolicy` with
`controller_id`, `volume_id`, `raid_type` and `member_count`. Never reuse another
node's controller/volume identifiers. GPU-01 identifiers remain unconfirmed;
omitting its policy does not establish RAID health. Unknown controller health and
strong Redfish-to-OS disk correlation remain explicit limitations of the accepted
best-effort BOSS approach. ABI eligibility remains blocked until later staging integration.

### Expansion to the initial cluster

After reviewing the two-node results, a later separately approved run can use the
same preparation target with this explicit initial-cluster selection:

FLEET_NODES=ipp5-285-rh-k8s-01,ipp5-285-rh-k8s-02,ipp5-285-rh-k8s-03,ipp5-285-rh-gpu-01,ipp5-285-rh-gpu-02,ipp5-285-rh-gpu-04

Use a fresh run ID, review the new plan and timeouts, then obtain exact run/node
authority. GPU-03 remains excluded. Selection does not change spreadsheet ABI
membership. The existing single-node test targets and stable paths remain available.

## Future installation artifact conventions

Complete-cluster installation will use
`reports/installation/<UTC-run-timestamp>/<node>/` with run-level
`installation-status.yaml`. The timestamp format is `YYYYMMDDTHHMMSSZ`.
Installation orchestration remains subsequent work.

## Authorization Boundary

The driver reads local configuration, materializes the local pull-secret file,
and invokes the collection role to read release metadata from the matched EE
container. It does not contact a BMC, deploy Ironic, enroll a node, attach
virtual media, or boot hardware. Authenticated Redfish GET and local ephemeral
Ironic checks are later Gate-4 preflight activities and require Gate-4
authorization. Hardware boot remains exclusively a separately approved Gate-6
action.

## Optional native all-disk metadata cleaning

Use the exported ARCH, SITE, FLEET_RUN_ID and FLEET_NODES from the successful
inspection. Only the wipe run ID changes in an existing shell:

export FLEET_WIPE_RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"

make prepare-fleet-wipe

Verify the fresh bundle and review its `review.txt`. It explicitly lists the
all-disk scope and historical disks for review. Older installation-volume bundles
cannot execute this operation.

After reviewing and authorizing that exact run and node set:

make wipe-fleet-disks FLEET_AUTHORIZE_WIPE_RUN="$FLEET_WIPE_RUN_ID"

`FLEET_AUTHORIZE_WIPE_NODES` must contain the reviewed node set. The retired
`wipe-fleet-install-disks` command fails rather than silently broadening its scope.

Standard Ironic/IPA handles discovery, TLS, hardware managers, metadata erasure
and continuation. The wrapper submits one manual-clean request per node and stops
later nodes after failure. Completion requires native return to `manageable`
with no active step or error, durable evidence, verified Off/media detachment and
runtime/firewall cleanup. An uncertain submission or active operation retains
runtime for review; no wrapper retry is performed.

Reports remain under `reports/wipe/<run-id>/`: `outcome.yaml` per node,
`diagnostics.yaml`, `summary.yaml`, `runtime.yaml` and `receipt.yaml`. Cleaning
remains optional; preparing alone preserves inspection-only ABI eligibility.
Started failed or incomplete cleaning still requires successful completion or an
explicit disposition after verified terminal failure and cleanup.

## OEM binding for optional native metadata cleaning

`make prepare-fleet-wipe` reads the canonical site workbook's Settings
`ocp_oem` and requires agreement with `ocp-settings.yml` `oem`. It seals the
declaration and complete native interface profile into the plan, runtime plan
and `review.txt`. It does not modify the workbook or original inspection bundle.

`dell` selects hardware type `idrac`, boot `idrac-redfish-virtual-media`,
management `idrac-redfish` and supported native `redfish` power. The pinned
Ironic `idrac-redfish` power class is unsupported/deprecated. Explicit `redfish` selects the generic native
Redfish profile. Both retain `direct` deployment, `noop` networking, native
Redfish credentials and configured system IDs. Missing or unverified OEMs are
rejected; no inferred vendor or generic boot fallback is used. HPE/iLO and other
native drivers need separately verified profile contracts before use.

The executor checks every selected interface against the running hardware-type
API before enrollment or boot. Result validation and retained provenance use
the same profile. Older preparations without this OEM binding require a fresh
run ID and review. The pinned-image iDRAC contract check precedes another physical
attempt; local mocked tests do not verify live OEM media behavior.
