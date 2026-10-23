# ABI media modes

An optional `input/<arch>/<site>/abi-media.yml` selects `mode: full` (the default),
`mode: full-ram`, `mode: minimal`, or `mode: manual`. Manual selects full ISO
content and a separate operator-owned boot method.

`full` uses the existing full ISO via virtual media. Existing builds and sealed
attempts without a media field remain full. A fresh named full build is supported.

`full-ram` builds a full ISO and appends only `coreos.liveiso.fromram=1` using
`coreos-installer iso kargs modify` in the separately pinned official tool image.
Exact argument readback must preserve every original argument. The shared verifier
extracts this ISO's own initramfs and uses explicit host `lsinitrd` to capture the
RHCOS live-generator RAM-copy source. This selects copying the embedded rootfs into
RAM; it does not copy the entire ISO or establish runtime RAM residency. Physical
boot and mount/backing evidence remain separate.

`minimal` sets `minimalISO: true` and requires `boot_artifacts_base_url` equal to
`http://<reviewed-bastion-source>:6180/redfish`. The installer writes the matching
`boot-artifacts/agent.x86_64-rootfs.img`; its URL must appear in ISO kernel arguments.
The service publishes that rootfs and the ISO, validates both using streaming
SHA256/size checks, and permits HTTP from selected node addresses as well as BMCs.
Rootfs is sourced from this build, never independently downloaded from a guessed
release URL. Node early networking must reach the reviewed endpoint.

For fresh variants supply `ABI_MEDIA_BUILD_ID=<unique-build-name>` to
`make generate-ocp-iso` and to installation preparation. Named outputs live under
`output/<arch>/<site>/ocp-iso/<inspection-run>/builds/<build-name>/`. The original
run directory is retained. A destination is never reused. Failed builds remain
private and lack a successful `media-build.yaml`; select another identity after
review. The build receipt binds inspected manifest hashes, the permitted derived
agent configuration, immutable installer/release identities, kernel arguments,
and artifact filenames, hashes and sizes.

Use the existing prepare/serve/prepare-boot/boot reviews and authorization sequence.
Preparation seals the selected build. Changing `ABI_MEDIA_BUILD_ID` later cannot
switch an existing attempt. A changed policy requires matching generation and a
fresh installation attempt. Absence of a build identity selects the legacy run
path only; nondefault generation requires an explicit fresh identity. Variant builds
require the original digest-pinned installer EE (openshift-install and xorriso),
explicit collection root, and host `/usr/bin/lsinitrd`. Full-ram editing additionally
uses `quay.io/coreos/coreos-installer@sha256:db8c1dc7aa5955613243c5cd73d2be58ae7b839873563dc44e0bf0a15e7fed71`
with verified tool version 0.27.0. Make exposes `ABI_COREOS_INSTALLER_IMAGE` and
`ABI_LSINITRD`; an unreviewed full-ram tool pin is rejected. Verification containers
use cached images, no network and private bounded command logs. No EE rebuild is
required. Legacy full/manual builds do not require these variant tools.

Both generation paths consume the collection's shared `module_utils/abi_media.py`.
Variant receipts use schema 2 and bind the shared verifier source, tool identities,
release, extracted source/configuration evidence and final artifact hashes/sizes.
Preparation rechecks those bindings and rejects obsolete variant receipts. Existing
schema-1 full receipts remain valid.

Minimal validation reads actual GRUB and ISOLINUX entries and checks matching kernel
arguments, the exact rootfs URL, and required kernel/ignition/custom-initrd references.
The pinned installer has produced inconsistent kargs metadata: the ISOLINUX offset
points inside its initrd list and quoting differs between bootloaders. The verifier
retains this as `argument_metadata.status: INCONSISTENT` and overall `PARTIAL` while
accepting valid actual boot entries. It never repairs upstream media or treats that
metadata anomaly alone as evidence of a boot failure. The anomaly is sealed in the
private validation receipt; physical bootability remains unverified.

Original inspected inputs, installer state, auth and prior outcomes are preserved.
Only boot artifacts are mounted into the server; installer observation copies an
explicit private state/auth allowlist and excludes ISO/rootfs/build metadata.
Installation still requires native completion and every expected node Ready.
Completion and standard retirement remove the owned shared serving volume and
recorded firewall rules, including minimal-mode node rules. Retirement does not
change node power/media/disks or infer a successful installation.

Source references: [Agent installer](https://docs.redhat.com/en/documentation/openshift_container_platform/4.22/html-single/installing_an_on-premise_cluster_with_the_agent-based_installer/index),
[ISO tooling](https://coreos.github.io/coreos-installer/cmd/iso/).

## Manual insertion and boot

Select `mode: manual` before preparing a fresh serving attempt. Existing full ISO
content may be reused; generation uses the full-content path and records full
artifact policy. The serving plan independently seals `boot_method: manual`.
Use the normal serving and boot preparation reviews and exact attempt authorization.

`make boot-ocp-nodes FLEET_AUTHORIZE_BOOT_RUN=<attempt>` writes
`boot/operator-handoff.yaml` with `WAITING_FOR_OPERATOR`, displays the selected
media URL/hash and node names, and stops at an interactive prompt. Insert that
reviewed media and boot the selected nodes, then press Enter. Ctrl-C, EOF or a
noninteractive invocation leaves the gate waiting and starts no installer waits.
Return with `make continue-ocp-install FLEET_AUTHORIZE_BOOT_RUN=<attempt>`.
There is no unattended confirmation flag.

The durable `OPERATOR_CONFIRMED` record binds the exact attempt, plan fingerprint,
ISO identity/URL and node set. It is operator attestation, not verified BMC power,
media insertion or OS startup. The collection validates it before any observation;
no BMC credentials, vault prompt, boot action or BMC request is required. Installer
observation deadlines start after confirmation. Confirmed interrupted observation
uses `resume-ocp-install`; it never asks for another boot or confirmation.

Native installation completion and every expected node Ready remain mandatory.
Completion archives service evidence and removes only owned serving resources.
Operator media is untouched; `media_cleanup` reports operator ownership and
`media_detached_verified: false`. The operator remains responsible for ejecting
media and clearing any manually configured boot override. Do not interpret
`boot_complete: false` as a failed automated boot: none was attempted in this mode.
