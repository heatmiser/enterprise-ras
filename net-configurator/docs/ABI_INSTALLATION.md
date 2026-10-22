# Existing ABI ISO preparation and serving

Generate the ISO once using the existing `generate-ocp-iso` target. Preserve its
entire `ocp-iso/<inspection-run-id>` directory, including private installer state
and authentication assets. The commands below do not regenerate it.

With the active ARCH/SITE context and successful inspection FLEET_RUN_ID set:

    make prepare-ocp-install

Review `output/<arch>/<site>/ocp/install/fleet/<inspection-run-id>/review.txt`, then:

    make serve-ocp-iso

No new run-ID or node-list exports are needed. Membership comes from the inspected
initial ABI manifests. An unsuccessful or unresolved optional cleaning operation
continues to block preparation/serving through the existing cleaning ledger.

## Repeat installation using the existing ISO

For a repeat, retain `FLEET_RUN_ID` as the original successful inspection ID. Use:

    make prepare-ocp-reinstall

This creates a fresh UTC/unique-suffix installation attempt ID, validates the
existing ISO and private source assets, and prints the new preparation path for
review. It starts no services, submits no cleaning and contacts no BMC. Native
all-disk metadata cleaning remains separately prepared, reviewed and authorized
through the existing wipe workflow before the repeat boot.

Each repeat uses `ocp/install/fleet/<inspection-run-id>/attempts/<attempt-id>/`
for its preparation and `reports/installation/<inspection-run-id>/attempts/<attempt-id>/`
for serving and boot evidence. Original preparation, claims, reports, ISO and auth
are preserved. A private `current-attempt.yaml` reference selects the new attempt
under the site lease only after its preparation succeeds. The reference binds the
inspection, attempt ID and prepared plan fingerprint; subsequent verification
checks them before execution. No new attempt-ID export is needed.

Preparation refuses a retained runtime lock, unresolved prior boot cleanup, or an
unconsumed serving runtime, including when its lock is missing. A prior serving
receipt remains immutable: its exact owner may instead be proven consumed by the
corresponding completed boot cleanup receipt. Re-running preparation creates
another distinct attempt, never overwrites a previous attempt or retries boot.

After reviewing the new preparation, existing commands select it automatically:

    make serve-ocp-iso
    make prepare-ocp-boot

The printed boot review includes the exact authorization command:

    make boot-ocp-nodes FLEET_AUTHORIZE_BOOT_RUN=<installation-attempt-id>

Use that same attempt ID for `resume-ocp-install` if observation must resume.
Authorization for the original inspection or another installation attempt is
rejected. Plans predating attempt selection retain their original paths and
inspection-ID authorization. The collection also binds its boot/resume receipt to
the selected attempt, separately from the unchanged inspection provenance.

## Serving and boot behavior

Preparation verifies physical inspection eligibility, staged eligibility and
endpoint mapping, immutable installer/release provenance, selected membership,
routes and reviewed BMC addresses. It records hashes for the ISO and private
installer assets. Serving rechecks these bindings and the collection implementation.
The original ISO checksum is displayed for operator review; this verifies identity,
not the ISO's internal boot contents.

Serving copies only `agent.x86_64.iso` into a dedicated media directory. The
credential-bearing installer directory is never mounted into Ironic/HTTPD.
The release-matched Ironic image is resolved through the pinned installer EE.
Its native publisher copies the ISO into the owned shared HTTP volume. A streaming
HTTP download from the bastion verifies length and SHA256 without retaining another
ISO copy. BMC-to-bastion access remains a live boot check.

The collection reserves `/tmp/rhvp-fleet-inspection-runtime.lock`, refuses existing
named runtime resources, and opens runtime-only HTTP firewall rules for the
reviewed BMC IPv4 addresses. It leaves a successful serving runtime running for
the subsequent boot phase. Repeating serving while that runtime is owned is
rejected; no existing runtime is silently replaced.

Each invocation records an `attempt-*` directory under
`reports/installation/<inspection-run-id>/serving/`. It contains the plan,
`runtime.yaml`, and bounded redacted service diagnostics. Failed startup/serving
attempts remove only resources owned by that attempt. Cleanup failures retain
the lock and an honest recovery receipt. Existing runtime conflicts are left alone.
Do not delete the lock or use broad Podman/firewall cleanup to force a retry.

This phase does not enroll nodes, change power or media, inspect hardware, erase
storage, or run installer monitoring. Node boot and completion-gated cleanup are
subsequent separately reviewed phases. The workbook OEM selects the subsequent boot implementation. For Dell, use a
one-time virtual-media override before power-on; completion cleanup ejects media
without powering off the installed cluster.

## Boot, monitoring and completion cleanup

After successful serving, `make prepare-ocp-boot` derives the selected nodes from
that same inspected fleet and reads the successful attempt through the retained
runtime lock owner. It creates a separate `ocp/install/fleet/<run>/boot/` review;
it does not restart serving, contact a BMC, regenerate media, inspect, or clean.
Existing serving preparations remain valid because boot implementation hashes are
bound separately. No new node list or run ID is needed.

The operator entry point is `make boot-ocp-nodes` with one explicit
`FLEET_AUTHORIZE_BOOT_RUN` matching `FLEET_RUN_ID`. It boots the fleet, monitors native installer completion and performs completion-only cleanup.
Reviewing boot preparation is not physical authorization.

The workbook `ocp_oem` setting selects a boot profile, sealed independently of the
serving plan. Currently `dell` selects OpenManage virtual media, UEFI/CD/Once,
`reset_type: none`, and a separate Redfish power-on. Unsupported OEMs are rejected
at preparation; additional OEM/BMC profiles require their own implementation and
verification. Dell virtual-media support in OpenManage 9.3.0 uses
`System.Embedded.1`; another system ID is rejected explicitly.

The collection adopts the running Ironic/HTTPD and private API secrets, verifies
media, requires an empty Ironic inventory and all selected systems Off, then boots
the exact fleet directly through its OEM profile. Ironic serves the ISO but does
not enroll or provision these nodes. Inspection and cleaning are not requested.

For each Dell node, discover the optical device from advertised media types,
attach the exact published ISO, and verify its insertion and URL. Configure and
read back UEFI/CD/Once while the system remains Off. Power on once, then poll
read-only for On. `reset_type: none` prevents the boot-configuration module from
restarting the server. The persistent BIOS boot order is unchanged; after the
one-time override is consumed, firmware resumes its normal boot order.

Each selected node's intent and boot outcome are retained under
`reports/installation/<run>/boot/runtime.yaml`. A claim prevents silently
re-executing a partially submitted fleet. Failures stop subsequent nodes and
retain services, media and the serving lock for reviewed recovery, with bounded
redacted diagnostics. An uncertain mutation response is never resubmitted
automatically by the driver.

`BOOTED` means the reviewed media and one-time override were verified before
power-on and the system was observed On. It does not prove ISO startup or
OpenShift installation completion. The pinned installer EE runs native `agent wait-for bootstrap-complete` and
`agent wait-for install-complete`. Default outer deadlines are 60 and 90 minutes;
the installer has no timeout flag. Each wait uses a uniquely named temporary
container; timeout removes only that waiter, retaining ISO-serving resources.

Monitoring copies sealed installer assets into `boot/installer/`, excluding the
ISO. The original stage, auth and installer state remain unchanged. Complete private
bootstrap/install logs and a bounded ring of API readiness, node and cluster
operator snapshots are retained beside `runtime.yaml`. These files may contain
credentials or sensitive cluster information; do not publish the report tree.

Native completion is followed by a bounded API gate requiring every expected
Kubernetes node from the sealed endpoint map to be present and Ready, with no
unexpected nodes. The default readiness deadline is 900 seconds. Missing or
not-Ready nodes retain serving services and virtual media; `fleet-ready.json`
records the last observation. Resume repeats this gate even when native waits
have already completed. Output reception times in `installer-observations.jsonl`
are observation timestamps, not reconstructed historical source event times.

After native install completion and full-fleet readiness, cleanup verifies every node remains powered On,
disables the temporary override without resetting or changing persistent boot
order, ejects only the optical device and ISO recorded during boot, and verifies
detachment. It archives complete private timestamped service logs with size and
SHA256 manifests, retains bounded redacted diagnostics, and removes owned services,
secrets, volume and runtime firewall rules, verifies absence and releases ownership.
`SUCCEEDED` requires native installation completion, expected-fleet readiness and verified cleanup.

For a failed installer wait after all selected nodes were booted:

    make resume-ocp-install FLEET_AUTHORIZE_BOOT_RUN=<inspection-run-id>

Resume revalidates the same handoff and serving owner. It never attaches media or
submits power actions, and skips native wait phases already completed. An existing
waiter or observation claim prevents concurrent observation. Partial boot and
interrupted shared teardown require reviewed recovery instead of automatic replay.
Original auth remains private; installed nodes are never powered off by completion
cleanup. Mixed-OEM fleets remain a follow-up.

## HTTPD policy in ABI serving preparations

ABI ISO serving reads `input/<arch>/<site>/ironic-httpd.yml` during preparation,
using the same three inputs documented in [INSPECTION_PREFLIGHT.md](INSPECTION_PREFLIGHT.md#httpd-tuning-preparation-policy).
The resolved values are sealed in `abi_install_plan.httpd_tuning`, displayed in
`review.txt`, and passed explicitly to the Ironic deployment role. The Apache
profile and privileged host sysctl management remain separate opt-ins.

The serving entry point rejects conflicting execution inputs before starting the
runtime. A missing policy or legacy plan defaults to disabled tuning. Changing the
site file does not rewrite an already reviewed serving plan; prepare a fresh
installation attempt to select different settings. Executor drift checks include
the HTTPD tuning template. No existing ISO, installer state, credentials or boot
authorization is changed by preparing this policy. Runtime image validation and
host application remain separate operational steps.


## Planned replacement and runtime retirement

A healthy operating cluster may be voluntarily replaced at the same OpenShift
version or by a fresh installation of an explicitly selected earlier/later version.
A failed installation uses the same runtime-retirement process. Preserve the
actual historical outcome; never mark an installation failed to permit replacement.

Use the standard targets from net-configurator:

1. Select `ARCH`, `SITE`, `FLEET_RUN_ID` (inspection lineage), and the explicit
   `ABI_RETIREMENT_ATTEMPT` (installation attempt ID; original layouts use the
   inspection run ID). Supply `ABI_RETIREMENT_REASON` and
   `ABI_RETIREMENT_TARGET_RELEASE` as an immutable release pullspec with digest.
2. Run `make prepare-ocp-retirement` with those values. Preparation writes only
   the sealed review under the selected preparation's `retirement/` directory.
   Read `review.txt`, including owner, nodes, prior outcome and intended release.
3. Run `make retire-ocp-runtime` with the same selection and
   `FLEET_AUTHORIZE_RETIREMENT_RUN` matching the exact installation attempt.
   Existing passwordless sudo works for owned runtime firewall removal; the
   workflow does not load BMC credentials or request new registry authentication.
4. Require `reports/installation/<inspection>/attempts/<attempt>/retirement/receipt.yaml`
   to report `RETIRED`, `cleanup_verified: true`, and `node_mutations: false`.
   Original-layout reports omit the `attempts/<attempt>/` component.
5. Separately prepare/review/authorize the selected fleet wipe and fresh installation.
   Retirement does not wipe or change membership. Confirm the intended selected nodes
   explicitly; an excluded Day 2 node remains untouched.

Retirement archives complete private timestamped Ironic/HTTPD logs and checksums,
checks the exact owner and concrete resource IDs, refuses active monitors or enrolled
Ironic nodes, and removes only the recorded services, volume, secrets and owned
runtime firewall rules. Nodes keep their current power and media state. Persistent
HTTPD/sysctl settings, original ISO, auth, installer state and installation receipts
remain unchanged. `prepare-ocp-reinstall` recognizes a matching verified retirement
receipt without rewriting the earlier installation as successful.

If an installation already completed normal cleanup, retirement verifies that owned
resources are absent and the previous complete service archive is intact; it does
not require an Ironic API. Missing/foreign ownership or unverifiable partial state
is rejected with diagnostics. No force-reset option is provided.

If retirement is interrupted, retain its reports and repeat the same authorized
command after resolving the reported cause. The durable identity record supports
already-removed resources; replacement objects with the same names are rejected.
An active monitor claim requires investigation; do not remove claims by age or
silently stop an installer. Failed evidence archival prevents teardown.

`resume-ocp-install` continues observation of an existing attempt and performs normal
completion cleanup only when the full fleet is Ready. Runtime retirement deliberately
ends that serving lifecycle for replacement while preserving its actual outcome.
Neither action automatically reboots or reinstalls nodes.

The target release recorded during retirement is intent only. Current
`prepare-ocp-reinstall` reuses the existing validated ISO. A different target release
requires a fresh release-matched installer build, media and provenance; this retirement
phase does not implement cross-release media preparation or reuse old sealed assets
under a new version. Same-version reuse also requires unchanged validated inputs.
