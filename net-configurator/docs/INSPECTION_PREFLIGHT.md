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
3. Generate the selected node's inspection-only NMState document.
4. Ensure the imported workbook's Settings-tab `ocp_version` is an exact
   three-part OCP release. The workbook at
   `input/<arch>/<site>/<arch>.xlsx` is the sole declared version source;
   `ocp-settings.yml` is a generated projection and is not read here.

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

After the report artifact and the collection's Gate-4 and Gate-5 checks have
been reviewed, the separately authorized physical action is driven from this
project:

    make inspect-controlled-candidate ARCH=<arch> SITE=<site> \
      INSPECTION_CANDIDATE=<candidate> \
      INSPECTION_AUTHORIZE_PHYSICAL_BOOT=<candidate> \
      INSPECTION_REPORT_PATH=/absolute/durable/path/<candidate>-inventory.yaml

The authorization value must exactly equal the selected candidate. The report
path must be absolute, must not be under `/tmp`, and must not already exist.
The wrapper consumes the prepared `preflight-vars.yaml`, collection
`secrets.yaml`, and collection bootstrap inventory at runtime. It maps the
prepared one-node declaration to the collection's `inspect_cluster.yml`; it
does not create a second release, media, NMState, callback, or BMC policy.

The report is non-secret opaque evidence, mode `0640`, and must be retained
for review before any ABI disk, NIC, or LLDP reconciliation work. The
collection owns the physical lifecycle and its `always` teardown: report
persistence, virtual-media detach, BMC postcondition verification, and
ephemeral Ironic/customizer cleanup.

## Authorization Boundary

The driver reads local configuration, materializes the local pull-secret file,
and invokes the collection role to read release metadata from the matched EE
container. It does not contact a BMC, deploy Ironic, enroll a node, attach
virtual media, or boot hardware. Authenticated Redfish GET and local ephemeral
Ironic checks are later Gate-4 preflight activities and require Gate-4
authorization. Hardware boot remains exclusively a separately approved Gate-6
action.
