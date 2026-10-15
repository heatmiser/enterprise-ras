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
subsequent separately reviewed phases. Native virtual-media boot may select
persistent CD-ROM boot; installation completion must restore local-disk boot and
eject media without powering off the installed cluster.
