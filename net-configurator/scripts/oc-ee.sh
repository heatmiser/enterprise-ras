# SPDX-License-Identifier: MIT
# Source once in Bash; resolve the selected completed installation on every call.
_ERA_OC_EE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

oc_ee() {
    python3 "${_ERA_OC_EE_ROOT}/scripts/oc-ee.py" "$@"
}
