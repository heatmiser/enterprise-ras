# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""Exercise wipe preparation against sealed synthetic physical handoffs."""

import hashlib
import importlib.util
import json
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap

import pytest
import yaml

from tests.test_abi_eligibility_handoff import (
    candidate_workspace, eligible_run, gate, refresh_receipt, write,
)
from tests.test_fleet_inspection_adapter import workspace as inspection_workspace, NAMES, RUN, ROOT


spec = importlib.util.spec_from_file_location("wipe_adapter", ROOT / "scripts/prepare-fleet-wipe.py")
wipe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wipe)
WIPE = "20000101T130000Z"
COLLECTION = Path(os.environ.get("RHV_BAREMETAL_OCP_ROOT", ROOT.parents[1] / "rhvp.baremetal_ocp")).resolve()


@pytest.fixture
def workspace(tmp_path, request):
    """Declare OEM before inspection seals the workbook and generated settings."""
    import openpyxl
    root, collection = inspection_workspace.__wrapped__(tmp_path)
    inputs = root / "input/2-8-5-200/test01"
    oem, settings_oem = getattr(request, "param", ("dell", "dell"))
    path = inputs / "2-8-5-200.xlsx"
    workbook = openpyxl.load_workbook(path)
    if oem is not None:
        workbook["Settings"].append(["ocp_oem", oem])
    workbook.save(path)
    workbook.close()
    settings = gate.read_yaml(inputs / "ocp-settings.yml")
    settings["oem"] = settings_oem
    if settings_oem == "redfish":
        settings["install_disk"]["overrides"] = {name: f"/dev/disk/by-path/fixture-{name}" for name in NAMES}
    write(inputs / "ocp-settings.yml", settings)
    if oem == "redfish":
        config = collection / "inventories/test01/cluster-vars.yaml"
        value = gate.read_yaml(config)
        for node in value["ocp_nodes"]:
            node["bmc"]["driver"] = "redfish"
        write(config, value)
    return root, collection


@pytest.fixture
def wipe_source(eligible_run):
    root, bundle, evidence = eligible_run
    for index, name in enumerate(NAMES):
        node = gate.read_yaml(bundle / "plan.yml")["inspect_fleet_plan"]["authorized_nodes"][index]
        disk = {"name": f"/dev/nvme{index + 1}n1", "by_path": node["rootDeviceHints"]["deviceName"],
                "serial": f"fixture-volume-{index}", "wwn": f"fixture-wwn-{index}",
                "model": "Example Boot Volume", "size": 1000000000}
        (evidence / name / "inventory-raw.json").write_text(json.dumps({"inventory": {
            "disks": [disk, *[
                {"name": f"/dev/sd{letter}", "by_path": f"/dev/disk/by-path/example-data-{letter}",
                 "serial": f"fixture-data-{index}-{letter}", "wwn": f"fixture-data-wwn-{index}-{letter}",
                 "model": "Example Data Volume", "size": 2000000000} for letter in ("a", "b")]],
            "system_vendor": {"manufacturer": "Example", "product_name": "Server",
                                               "serial_number": f"fixture-chassis-{index}"}}}))
        validation = gate.read_yaml(evidence / name / "validation.yaml")
        validation["disk_validation"].update(node=name, selected_disk=disk, disk_match_verified=True,
                                              match_count=1, failures=[])
        write(evidence / name / "validation.yaml", validation)
    refresh_receipt(evidence)
    collection = root / "wipe-collection"
    for relative in wipe.collection_hashes(COLLECTION):
        target = collection / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(COLLECTION / relative, target)
    return root, bundle, evidence, collection


def prepare(source, names=NAMES, run=WIPE, clean_uefi_nvram=True):
    return wipe.prepare(source[0], "2-8-5-200", "test01", RUN, run, names, source[3], 1, clean_uefi_nvram)



@pytest.mark.parametrize("workspace", [(None, "dell"), ("dell", "redfish")], indirect=True)
def test_missing_or_mismatched_declaration_is_rejected_without_bundle(wipe_source):
    with pytest.raises(ValueError, match="Explicit workbook ocp_oem"):
        prepare(wipe_source)
    assert not (wipe_source[0] / "output/2-8-5-200/test01/ocp/wipe").exists()


@pytest.mark.parametrize("workspace", [("redfish", "redfish")], indirect=True)
def test_explicit_generic_redfish_profile_is_sealed(wipe_source):
    bundle = prepare(wipe_source)
    plan = gate.read_yaml(bundle / "plan.yml")["fleet_native_disk_clean_plan"]
    assert plan["oem"] == "redfish"
    assert plan["native_profile"]["driver"] == "redfish"
    assert plan["native_profile"]["boot_interface"] == "redfish-virtual-media"


@pytest.mark.parametrize("case", ["plan", "runtime"])
def test_tampered_sealed_profile_is_rejected(wipe_source, case):
    bundle = prepare(wipe_source)
    path = bundle / ("plan.yml" if case == "plan" else "runtime-plan.yml")
    value = gate.read_yaml(path)
    key = "fleet_native_disk_clean_plan" if case == "plan" else "fleet_wipe_runtime_plan"
    value[key]["native_profile"]["boot_interface"] = "redfish-virtual-media"
    write(path, value)
    with pytest.raises(ValueError, match="Native OEM profile binding"):
        wipe.verify_sources(wipe_source[0], "2-8-5-200", "test01", bundle)


def test_sealed_plan_matches_identities_without_credentials_or_authority(wipe_source):
    root, _, evidence, _ = wipe_source
    before = {str(p): p.read_bytes() for p in evidence.rglob("*") if p.is_file()}
    bundle = prepare(wipe_source)
    plan = gate.read_yaml(bundle / "plan.yml")["fleet_native_disk_clean_plan"]
    sealed = gate.read_yaml(bundle / "prepared.yaml")
    assert plan["concurrency_limit"] == 1 and plan["execution_ready"] is False
    assert plan["operation"] == "native_all_disks_metadata_cleaning"
    assert plan["oem"] == "dell"
    assert plan["native_profile"]["driver"] == "idrac"
    assert plan["native_profile"]["boot_interface"] == "idrac-redfish-virtual-media"
    runtime = gate.read_yaml(bundle / "runtime-plan.yml")["fleet_wipe_runtime_plan"]
    assert runtime["native_profile"] == plan["native_profile"] and runtime["oem"] == "dell"
    assert "Workbook OEM: dell; native hardware type: idrac" in (bundle / "review.txt").read_text()
    assert plan["clean_steps"] in ([{"interface": "deploy", "step": "erase_devices_metadata", "args": {}}], [{"interface": "deploy", "step": "clean_uefi_nvram", "args": {}}, {"interface": "deploy", "step": "erase_devices_metadata", "args": {}}])
    assert plan["policy"]["optional"] is True
    assert "require_post_reset_readback" not in plan["policy"]
    assert plan["schema_version"] == 3 and plan["scope"] == "all_native_eligible_disks"
    for node in plan["authorized_nodes"]:
        assert len(node["observed_disks"]) == 3
        assert "installation_volume" not in node and "protected_disks" not in node
    assert plan["policy"]["operator_disk_exclusions"] == []
    assert sealed["schema_version"] == 3 and sealed["operation"] == plan["operation"]
    assert "ALL NATIVE ELIGIBLE DISKS AND PARTITIONS" in (bundle / "review.txt").read_text()
    assert sealed["node_names"] == sorted(NAMES)
    assert plan["manifest_sha256"] == gate.read_yaml(evidence / "eligibility.yaml")["manifest_sha256"]
    for name, digest in sealed["sha256"].items():
        assert hashlib.sha256((bundle / name).read_bytes()).hexdigest() == digest
    text = (bundle / "plan.yml").read_text()
    assert "FIXTURE_NOT_A_CREDENTIAL" not in text and "redfish_password" not in text
    assert set(p.name for p in bundle.iterdir()) == {
        "plan.yml", "prepared.yaml", "review.txt", "runtime-plan.yml", "hosts.yml", "nmstate", "early-network"}
    assert sealed["runtime_sealed"] is False
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in bundle.rglob("*") if p.is_file())
    assert all(p.stat().st_mode & 0o777 == 0o700 for p in bundle.rglob("*") if p.is_dir())
    assert not (root / "output/2-8-5-200/test01/reports/wipe").exists()
    assert before == {str(p): p.read_bytes() for p in evidence.rglob("*") if p.is_file()}
    # Preparation leaves the successful inspection-only ISO handoff eligible.
    gate.validate_handoff(root, "2-8-5-200", "test01", RUN)


@pytest.mark.parametrize("case", ["changed_evidence", "changed_manifest", "changed_workbook",
                                 "failed_validation", "missing_host_serial",
                                 "duplicate_disk", "empty_selection", "duplicate_node", "extra_node",
                                 "unsafe_run", "unsafe_site", "missing_plugin"])
def test_invalid_preparation_creates_no_bundle(wipe_source, case):
    root, _, evidence, collection = wipe_source
    names, run, site = NAMES, WIPE, "test01"
    if case == "changed_evidence":
        (evidence / NAMES[0] / "ports-raw.json").write_text("{}")
    elif case == "changed_manifest":
        (root / "output/2-8-5-200/test01/ocp/agent-config.yaml").write_text("changed")
    elif case == "changed_workbook":
        (root / "input/2-8-5-200/test01/2-8-5-200.xlsx").write_bytes(b"changed")
    elif case == "failed_validation":
        value = gate.read_yaml(evidence / "summary.yaml")
        value["validation_passed"] = False
        write(evidence / "summary.yaml", value)
        refresh_receipt(evidence)
    elif case in ("missing_disk_serial", "missing_host_serial", "duplicate_disk"):
        path = evidence / NAMES[0] / "inventory-raw.json"
        raw = json.loads(path.read_text())
        if case == "missing_disk_serial":
            raw["inventory"]["disks"][0]["serial"] = ""
        elif case == "missing_host_serial":
            raw["inventory"]["system_vendor"]["serial_number"] = ""
        else:
            raw["inventory"]["disks"].append(dict(raw["inventory"]["disks"][0]))
        path.write_text(json.dumps(raw))
        refresh_receipt(evidence)
    elif case == "empty_selection":
        names = []
    elif case == "duplicate_node":
        names = NAMES + [NAMES[0]]
    elif case == "extra_node":
        names = NAMES + ["node-extra"]
    elif case == "unsafe_run":
        run = "../unsafe"
    elif case == "unsafe_site":
        site = "../test01"
    else:
        (collection / "plugins/filter/fleet_native_disk_clean_plan.py").unlink()
    with pytest.raises((ValueError, FileNotFoundError)):
        wipe.prepare(root, "2-8-5-200", site, RUN, run, names, collection, 1, True)
    assert not (root / "output/2-8-5-200/test01/ocp/wipe").exists()


@pytest.mark.parametrize("case", ["existing_bundle", "existing_evidence", "symlink_parent", "symlink_evidence"])
def test_existing_or_symlink_paths_are_preserved(wipe_source, case):
    root = wipe_source[0]
    base = root / "output/2-8-5-200/test01"
    bundle = base / "ocp/wipe/fleet" / WIPE
    evidence = base / "reports/wipe" / WIPE
    if case.startswith("existing"):
        target = bundle if case == "existing_bundle" else evidence
        target.mkdir(parents=True)
        marker = target / "preserve.txt"
        marker.write_text("original")
    else:
        parent = bundle.parent if case == "symlink_parent" else evidence.parent
        parent.parent.mkdir(parents=True, exist_ok=True)
        target = root / "unrelated"
        target.mkdir()
        parent.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError):
        prepare(wipe_source)
    if case.startswith("existing"):
        assert marker.read_text() == "original"
    else:
        assert list(target.iterdir()) == []


def test_repeat_preparation_does_not_rewrite_seal(wipe_source):
    bundle = prepare(wipe_source)
    before = {str(p.relative_to(bundle)): p.read_bytes() for p in bundle.rglob("*") if p.is_file()}
    with pytest.raises(ValueError):
        prepare(wipe_source)
    assert before == {str(p.relative_to(bundle)): p.read_bytes() for p in bundle.rglob("*") if p.is_file()}


def test_real_make_preparation_invokes_only_preflight_driver(wipe_source):
    root, _, _, collection = wipe_source
    shutil.copyfile(ROOT / "Makefile", root / "Makefile")
    shutil.copytree(ROOT / "scripts", root / "scripts")
    bin_dir = root / "bin"
    bin_dir.mkdir()
    capture = root / "unexpected-runtime"
    for name in ("podman", "openshift-install"):
        command = bin_dir / name
        command.write_text("#!/bin/sh\nprintf 'unexpected' > \"$WIPE_RUNTIME_CAPTURE\"\nexit 99\n")
        command.chmod(0o700)
    preflight_capture = root / "preflight-call.json"
    command = bin_dir / "ansible-playbook"
    command.write_text("#!/usr/bin/env python3\n" + textwrap.dedent("""\
        import json, os, sys, subprocess
        from pathlib import Path
        import yaml
        Path(os.environ['PREFLIGHT_CAPTURE']).write_text(json.dumps(sys.argv[1:]))
        runtime_path = next(Path(arg[1:]) for arg in sys.argv if arg.startswith('@') and arg.endswith('runtime-plan.yml'))
        bundle = runtime_path.parent
        runtime = yaml.safe_load(runtime_path.read_text())['fleet_wipe_runtime_plan']
        original = yaml.safe_load(Path(os.environ['WIPE_PREFLIGHT_SOURCE']).read_text())
        candidate = runtime['authorized_nodes'][0]
        preflight = {
            **original, **runtime.get('httpd_tuning', {}),
            'inspection_preflight_provenance': {**original['inspection_preflight_provenance'],
                'callback_target_ip': candidate['devices']['bond_ip']},
            'preflight_inspection_nodes': [candidate],
            'ironic_deploy_provisioning_ip': '192.0.2.10',
            'ironic_deploy_provisioning_interface': 'eth-test',
        }
        (bundle / 'preflight-vars.yaml').write_text(yaml.safe_dump(preflight))
        routes = {'nodes': [n['name'] for n in runtime['authorized_nodes']],
                  'routes': [{'item': n, 'stdout': json.dumps([{'dst': n['devices']['bond_ip'],
                              'dev': 'eth-test', 'prefsrc': '192.0.2.10'}])} for n in runtime['authorized_nodes']]}
        (bundle / 'callback-routes.yaml').write_text(yaml.safe_dump(routes))
        subprocess.run([sys.executable, 'scripts/prepare-fleet-wipe.py', 'seal',
                        '--arch', '2-8-5-200', '--site', 'test01', '--bundle', str(bundle)], check=True)
        """))
    command.chmod(0o700)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "WIPE_RUNTIME_CAPTURE": str(capture),
           "PREFLIGHT_CAPTURE": str(preflight_capture),
           "WIPE_PREFLIGHT_SOURCE": str(wipe_source[1] / "preflight-vars.yaml")}
    write(root / '.era-context', {'arch': '2-8-5-200', 'site': 'test01', 'server_os': 'rhcos',
                                  'fleet_run_id': RUN, 'fleet_wipe_run_id': WIPE})
    env = {key: value for key, value in env.items() if not key.startswith('FLEET_')}
    command = ["make", "prepare-fleet-wipe", "FLEET_WIPE_CONCURRENCY=" + str(len(NAMES)),
               "RHV_BAREMETAL_OCP_ROOT=" + str(collection)]
    result = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not capture.exists()
    assert "playbooks/prepare-fleet-wipe.yml" in json.loads(preflight_capture.read_text())
    assert "no physical authority" in result.stdout
    context = wipe.CONTEXT.read_context(root / '.era-context')
    assert context['fleet_run_id'] == RUN and context['fleet_wipe_run_id'] != WIPE
    selected = root / 'output/2-8-5-200/test01/ocp/wipe/fleet' / context['fleet_wipe_run_id']
    verified = subprocess.run(
        [sys.executable, str(root / 'scripts/prepare-fleet-wipe.py'), 'verify',
         '--arch', '2-8-5-200', '--site', 'test01', '--bundle', str(selected)],
        cwd=root, env=env, capture_output=True, text=True, timeout=60)
    assert verified.returncode == 0, verified.stdout + verified.stderr
    selected_plan = gate.read_yaml(selected / 'plan.yml')['fleet_native_disk_clean_plan']
    assert selected_plan['concurrency_limit'] == len(NAMES)
    assert sorted(n['name'] for n in selected_plan['authorized_nodes']) == sorted(NAMES)
    assert not (root / 'output/2-8-5-200/test01/reports/wipe').exists()


def test_previous_preparation_is_preserved_when_fresh_native_plan_is_created(wipe_source):
    root = wipe_source[0]
    previous = root / "output/2-8-5-200/test01/ocp/wipe/fleet/20000101T123000Z"
    previous.mkdir(parents=True)
    marker = previous / "plan.yml"
    marker.write_text("legacy preparation; execution_ready: false\n")
    before = marker.read_bytes()
    bundle = prepare(wipe_source)
    assert bundle != previous and marker.read_bytes() == before


def fresh_preflight(source, bundle):
    """Model the role's selected candidate and callback, retaining release provenance."""
    runtime = gate.read_yaml(bundle / "runtime-plan.yml")["fleet_wipe_runtime_plan"]
    original = gate.read_yaml(source[1] / "preflight-vars.yaml")
    candidate = runtime["authorized_nodes"][0]
    preflight = {
        **original,
        "inspection_preflight_provenance": {
            **original["inspection_preflight_provenance"],
            "callback_target_ip": candidate["devices"]["bond_ip"],
        },
        **runtime.get("httpd_tuning", {}),
        "preflight_inspection_nodes": [candidate],
        "ironic_deploy_provisioning_ip": "192.0.2.10",
        "ironic_deploy_provisioning_interface": "eth-test",
    }
    write(bundle / "preflight-vars.yaml", preflight)
    write(bundle / "callback-routes.yaml", {"nodes": [n["name"] for n in runtime["authorized_nodes"]],
        "routes": [{"item": n, "stdout": json.dumps([{"dst": n["devices"]["bond_ip"],
            "dev": "eth-test", "prefsrc": "192.0.2.10"}])} for n in runtime["authorized_nodes"]]})
    return preflight


def seal_bundle(source, bundle):
    fresh_preflight(source, bundle)
    wipe.seal(source[0], "2-8-5-200", "test01", bundle)
    return bundle


def selected_inspection(source):
    write(source[0] / '.era-context', {'arch': '2-8-5-200', 'site': 'test01',
                                      'server_os': 'rhcos', 'fleet_run_id': RUN})


def test_preparation_resolves_receipt_membership_and_fresh_id(wipe_source):
    selected_inspection(wipe_source)
    inspection, run, names = wipe.resolve_preparation(wipe_source[0], '2-8-5-200', 'test01')
    assert inspection == RUN and names == sorted(NAMES) and run > RUN
    assert not (wipe_source[0] / 'output/2-8-5-200/test01/ocp/wipe').exists()
    assert wipe.resolve_preparation(wipe_source[0], '2-8-5-200', 'test01', RUN, WIPE, NAMES[:1]) == (
        RUN, WIPE, NAMES[:1])


@pytest.mark.parametrize('case', ['missing', 'wrong-site', 'invalid-run', 'altered-evidence'])
def test_automatic_preparation_rejects_invalid_context_or_receipt(wipe_source, case):
    root = wipe_source[0]
    selected_inspection(wipe_source)
    if case == 'missing':
        (root / '.era-context').unlink()
    elif case == 'wrong-site':
        write(root / '.era-context', {'arch': '2-8-5-200', 'site': 'other', 'fleet_run_id': RUN})
    elif case == 'invalid-run':
        write(root / '.era-context', {'arch': '2-8-5-200', 'site': 'test01', 'fleet_run_id': '../bad'})
    else:
        (wipe_source[2] / NAMES[0] / 'cleanup.yaml').write_text('{}')
    with pytest.raises(ValueError):
        wipe.resolve_preparation(root, '2-8-5-200', 'test01')
    assert not (root / 'output/2-8-5-200/test01/ocp/wipe').exists()


def test_selection_publishes_only_sealed_preparation(wipe_source):
    selected_inspection(wipe_source)
    root = wipe_source[0]
    previous = (root / '.era-context').read_bytes()
    bundle = prepare(wipe_source)
    with pytest.raises(ValueError):
        wipe.select_prepared(root, '2-8-5-200', 'test01', bundle)
    assert (root / '.era-context').read_bytes() == previous
    seal_bundle(wipe_source, bundle)
    assert wipe.select_prepared(root, '2-8-5-200', 'test01', bundle) == WIPE
    assert wipe.CONTEXT.read_context(root / '.era-context') == {
        'arch': '2-8-5-200', 'site': 'test01', 'server_os': 'rhcos',
        'fleet_run_id': RUN, 'fleet_wipe_run_id': WIPE}
    assert wipe.select_prepared(root, '2-8-5-200', 'test01', bundle) == WIPE
    assert not (root / 'output/2-8-5-200/test01/reports/wipe').exists()


def test_selection_rejects_context_changed_during_preflight(wipe_source):
    selected_inspection(wipe_source)
    bundle = seal_bundle(wipe_source, prepare(wipe_source))
    path = wipe_source[0] / '.era-context'
    write(path, {'arch': '2-8-5-200', 'site': 'other'})
    previous = path.read_bytes()
    with pytest.raises(ValueError, match='context changed'):
        wipe.select_prepared(wipe_source[0], '2-8-5-200', 'test01', bundle)
    assert path.read_bytes() == previous


class TerminalInput(io.StringIO):
    def isatty(self):
        return True


@pytest.mark.parametrize('response,terminal', [('', True), ('yes\n', True), ('wipe\n', False)])
def test_confirmation_cancellation_creates_no_execution(wipe_source, response, terminal):
    root = wipe_source[0]
    bundle = seal_bundle(wipe_source, prepare(wipe_source))
    stream = TerminalInput(response) if terminal else io.StringIO(response)
    with pytest.raises(ValueError):
        wipe.confirm_wipe(root, '2-8-5-200', 'test01', bundle, WIPE, stream, io.StringIO())
    assert not (root / 'output/2-8-5-200/test01/reports/wipe').exists()


def test_confirmation_records_exact_scope_under_existing_lease(wipe_source):
    root = wipe_source[0]
    bundle = seal_bundle(wipe_source, prepare(wipe_source))
    output = io.StringIO()
    with wipe.state.Lease(wipe.state.site_root(root, '2-8-5-200', 'test01')):
        authority = wipe.confirm_wipe(root, '2-8-5-200', 'test01', bundle, WIPE,
                                      TerminalInput('wipe\n'), output)
    assert authority['fleet_wipe_authorization']['node_names'] == sorted(NAMES)
    intent = gate.read_yaml(root / 'output/2-8-5-200/test01/reports/wipe' / WIPE / 'execution-intent.yaml')
    assert intent['node_names'] == sorted(NAMES) and intent['scope'] == 'all_native_eligible_disks'
    assert 'ALL NATIVE ELIGIBLE DISKS' in output.getvalue()
    assert 'clean_uefi_nvram, erase_devices_metadata' in output.getvalue()


def test_confirmation_revalidates_inputs_after_prompt(wipe_source):
    root = wipe_source[0]
    bundle = seal_bundle(wipe_source, prepare(wipe_source))

    class ChangedInput(TerminalInput):
        def readline(self):
            path = bundle / 'nmstate' / (NAMES[0] + '.yaml')
            path.write_bytes(path.read_bytes() + b'\n# changed\n')
            return 'wipe\n'

    with pytest.raises(ValueError):
        wipe.confirm_wipe(root, '2-8-5-200', 'test01', bundle, WIPE, ChangedInput(), io.StringIO())
    assert not (root / 'output/2-8-5-200/test01/reports/wipe').exists()


def test_cli_honors_nvram_opt_out(wipe_source):
    root = wipe_source[0]
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/prepare-fleet-wipe.py'),
        '--root', str(root), '--arch', '2-8-5-200', '--site', 'test01',
        '--inspection-run-id', RUN, '--wipe-run-id', WIPE, '--nodes', ','.join(NAMES),
        '--collection-root', str(wipe_source[3]), '--no-clean-uefi-nvram', '--print-bundle'],
        text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    bundle = Path(result.stdout.strip())
    plan = gate.read_yaml(bundle / 'plan.yml')['fleet_native_disk_clean_plan']
    assert plan['clean_steps'] == [{'interface': 'deploy', 'step': 'erase_devices_metadata', 'args': {}}]
    assert 'clean_uefi_nvram' not in (bundle / 'review.txt').read_text()


def test_fresh_seal_and_exact_authorization_do_not_start_execution(wipe_source):
    bundle = prepare(wipe_source)
    with pytest.raises(ValueError):
        wipe.verify(wipe_source[0], "2-8-5-200", "test01", bundle)
    seal_bundle(wipe_source, bundle)
    authority = wipe.authorize(wipe_source[0], "2-8-5-200", "test01", bundle, WIPE, NAMES)
    assert authority["fleet_wipe_authorization"]["confirmed"] is True
    assert not (wipe_source[0] / "output/2-8-5-200/test01/reports/wipe").exists()
    with pytest.raises(ValueError):
        wipe.authorize(wipe_source[0], "2-8-5-200", "test01", bundle, WIPE, NAMES[:1])


@pytest.mark.parametrize("case", ["executor", "network", "preflight", "route", "source"])
def test_sealed_runtime_changes_are_rejected(wipe_source, case):
    bundle = seal_bundle(wipe_source, prepare(wipe_source))
    path = {"executor": wipe_source[3] / "roles/fleet_native_disk_clean_driver/tasks/clean_node.yml",
            "network": bundle / "nmstate" / (NAMES[0] + ".yaml"),
            "preflight": bundle / "preflight-vars.yaml", "route": bundle / "callback-routes.yaml",
            "source": wipe_source[0] / "input/2-8-5-200/test01/2-8-5-200.xlsx"}[case]
    path.write_bytes(path.read_bytes() + b"\n# changed\n")
    with pytest.raises(ValueError):
        wipe.verify(wipe_source[0], "2-8-5-200", "test01", bundle)


def test_old_installation_volume_preparation_cannot_authorize_all_disks(wipe_source):
    bundle = seal_bundle(wipe_source, prepare(wipe_source))
    path = bundle / "plan.yml"
    value = gate.read_yaml(path)
    value["fleet_native_disk_clean_plan"].update(schema_version=2, operation=wipe.state.LEGACY_OPERATION)
    write(path, value)
    with pytest.raises(ValueError, match="fresh all-disk"):
        wipe.authorize(wipe_source[0], "2-8-5-200", "test01", bundle, WIPE, NAMES, start=True)
    assert not (wipe_source[0] / "output/2-8-5-200/test01/reports/wipe").exists()


def test_historical_data_disk_serials_do_not_become_exclusion_hints(wipe_source):
    path = wipe_source[2] / NAMES[0] / "inventory-raw.json"
    raw = json.loads(path.read_text())
    raw["inventory"]["disks"][1]["serial"] = None
    raw["inventory"]["disks"][2]["wwn"] = None
    path.write_text(json.dumps(raw))
    refresh_receipt(wipe_source[2])
    bundle = prepare(wipe_source)
    plan = gate.read_yaml(bundle / "plan.yml")["fleet_native_disk_clean_plan"]
    assert plan["policy"]["operator_disk_exclusions"] == []


def test_retired_make_command_cannot_silently_widen_scope(wipe_source):
    root = wipe_source[0]
    shutil.copyfile(ROOT / "Makefile", root / "Makefile")
    result = subprocess.run(["make", "wipe-fleet-install-disks"], cwd=root, capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert "fresh all-disk preparation" in result.stdout


@pytest.mark.parametrize("physical_source", [False, True])
def test_selected_node_preparation_and_authorization_are_exact(wipe_source, physical_source):
    if physical_source:
        from tests.test_physical_inspection_handoff import make_physical_source
        make_physical_source(wipe_source, NAMES[1:])
    bundle = seal_bundle(wipe_source, prepare(wipe_source, names=NAMES[1:]))
    plan, runtime = wipe.verify(wipe_source[0], "2-8-5-200", "test01", bundle)
    preflight = gate.read_yaml(bundle / "preflight-vars.yaml")
    selected_callback = runtime["authorized_nodes"][0]["devices"]["bond_ip"]
    assert preflight["inspection_preflight_provenance"] == {
        **runtime["original_provenance"], "callback_target_ip": selected_callback}
    assert preflight["preflight_inspection_nodes"][0]["name"] == NAMES[1]
    if not physical_source:
        assert selected_callback != runtime["original_provenance"]["callback_target_ip"]
    assert runtime["original_provenance"] == gate.read_yaml(wipe_source[1] / "preflight-vars.yaml")["inspection_preflight_provenance"]
    assert [n["name"] for n in plan["authorized_nodes"]] == NAMES[1:]
    assert [n["name"] for n in runtime["authorized_nodes"]] == NAMES[1:]
    assert sorted(p.stem for p in (bundle / "early-network").iterdir()) == NAMES[1:]
    assert sorted(p.stem for p in (bundle / "nmstate").iterdir()) == NAMES[1:]
    expected = "physical-readiness.yaml" if physical_source else "eligibility.yaml"
    assert runtime["inspection_receipt_name"] == expected
    if physical_source:
        assert plan["manifest_sha256"] == {}
    authority = wipe.authorize(wipe_source[0], "2-8-5-200", "test01", bundle, WIPE, NAMES[1:])
    assert authority["fleet_wipe_authorization"]["node_names"] == NAMES[1:]
    with pytest.raises(ValueError, match="Exact wipe run"):
        wipe.authorize(wipe_source[0], "2-8-5-200", "test01", bundle, WIPE, NAMES)
    assert not (wipe_source[0] / "output/2-8-5-200/test01/reports/wipe").exists()


def test_selected_runtime_cannot_expand_after_preparation(wipe_source):
    bundle = prepare(wipe_source, names=NAMES[:1])
    value = gate.read_yaml(bundle / "runtime-plan.yml")
    value["fleet_wipe_runtime_plan"]["authorized_nodes"] = gate.read_yaml(wipe_source[1] / "plan.yml")["inspect_fleet_plan"]["authorized_nodes"]
    write(bundle / "runtime-plan.yml", value)
    with pytest.raises(ValueError, match="Runtime node binding"):
        wipe.verify_sources(wipe_source[0], "2-8-5-200", "test01", bundle)


@pytest.mark.parametrize("stage", ["seal", "verify"])
@pytest.mark.parametrize("field", ["callback_target_ip", "release_image", "ee_image", "installer_version",
                                    "installer_architecture", "stream_sha256", "architecture"])
def test_selected_preflight_rejects_wrong_callback_or_release(wipe_source, stage, field):
    bundle = prepare(wipe_source, names=NAMES[1:])
    preflight = fresh_preflight(wipe_source, bundle)
    if stage == "verify":
        wipe.seal(wipe_source[0], "2-8-5-200", "test01", bundle)
    preflight["inspection_preflight_provenance"][field] = "unexpected-value"
    write(bundle / "preflight-vars.yaml", preflight)
    if stage == "verify":
        # Exercise semantic validation independently of the file checksum guard.
        prepared = gate.read_yaml(bundle / "prepared.yaml")
        prepared["sha256"] = wipe.bundle_hashes(bundle)
        write(bundle / "prepared.yaml", prepared)
    operation = wipe.seal if stage == "seal" else wipe.verify
    with pytest.raises(ValueError, match="Release provenance or selected callback"):
        operation(wipe_source[0], "2-8-5-200", "test01", bundle)
    assert not (wipe_source[0] / "output/2-8-5-200/test01/reports/wipe").exists()


@pytest.mark.parametrize("manage", [False, True])
def test_wipe_seals_new_httpd_policy_without_changing_inspection_source(wipe_source, manage):
    root, original, _, collection = wipe_source
    before = (original / "preflight-vars.yaml").read_bytes()
    write(root / "input/2-8-5-200/test01/ironic-httpd.yml", {
        "ironic_deploy_httpd_tuning": True, "ironic_deploy_httpd_manage_sysctl": manage,
        "ironic_deploy_httpd_tuning_path": "/var/tmp/fixture-httpd"})
    bundle = seal_bundle(wipe_source, prepare(wipe_source))
    _, runtime = wipe.verify(root, "2-8-5-200", "test01", bundle)
    assert runtime["httpd_tuning"]["ironic_deploy_httpd_manage_sysctl"] is manage
    assert "HTTPD profile: True" in (bundle / "review.txt").read_text()
    assert before == (original / "preflight-vars.yaml").read_bytes()
    assert "roles/ironic_deploy/templates/ironic-tuning.conf.j2" in runtime["collection_sha256"]
    assert "roles/ironic_deploy/tasks/prepare_httpd_tuning.yml" in runtime["collection_sha256"]


def test_wipe_seal_rejects_httpd_policy_mismatch(wipe_source):
    write(wipe_source[0] / "input/2-8-5-200/test01/ironic-httpd.yml", {"ironic_deploy_httpd_tuning": True})
    bundle = prepare(wipe_source)
    preflight = fresh_preflight(wipe_source, bundle)
    preflight["ironic_deploy_httpd_tuning"] = False
    write(bundle / "preflight-vars.yaml", preflight)
    with pytest.raises(ValueError, match="HTTPD preflight settings differ"):
        wipe.seal(wipe_source[0], "2-8-5-200", "test01", bundle)


@pytest.mark.parametrize("limit", [1, 2])
def test_concurrency_is_sealed_and_reviewed(wipe_source, limit):
    bundle = wipe.prepare(wipe_source[0], "2-8-5-200", "test01", RUN, WIPE, NAMES, wipe_source[3], limit, True)
    plan = gate.read_yaml(bundle / "plan.yml")["fleet_native_disk_clean_plan"]
    runtime = gate.read_yaml(bundle / "runtime-plan.yml")["fleet_wipe_runtime_plan"]
    assert plan["concurrency_limit"] == runtime["concurrency_limit"] == limit
    assert f"Concurrency: {limit}; independent node lifecycles" in (bundle / "review.txt").read_text()
    runtime["concurrency_limit"] = 2 if limit == 1 else 1
    write(bundle / "runtime-plan.yml", {"fleet_wipe_runtime_plan": runtime})
    with pytest.raises(ValueError, match="Native concurrency binding"):
        wipe.verify_sources(wipe_source[0], "2-8-5-200", "test01", bundle)


@pytest.mark.parametrize("limit", [0, -1, 3, True, "2"])
def test_invalid_concurrency_cannot_create_preparation(wipe_source, limit):
    with pytest.raises(ValueError, match="Concurrency"):
        wipe.prepare(wipe_source[0], "2-8-5-200", "test01", RUN, WIPE, NAMES, wipe_source[3], limit, True)
    assert not (wipe_source[0] / "output/2-8-5-200/test01/ocp/wipe/fleet" / WIPE).exists()


def test_parallel_task_files_are_in_executor_seal(wipe_source):
    hashes = wipe.collection_hashes(wipe_source[3])
    for task in ("clean_batch", "manage_node", "submit_clean", "observe_clean"):
        assert f"roles/fleet_native_disk_clean_driver/tasks/{task}.yml" in hashes
    (wipe_source[3] / "roles/fleet_native_disk_clean_driver/tasks/submit_clean.yml").unlink()
    with pytest.raises(ValueError, match="incomplete"):
        wipe.collection_hashes(wipe_source[3])


def test_cli_execution_cannot_override_concurrency(tmp_path):
    result = subprocess.run(["python3", str(ROOT / "scripts/prepare-fleet-wipe.py"), "begin",
                             "--arch", "2-8-5-200", "--site", "test01", "--root", str(tmp_path),
                             "--bundle", str(tmp_path / "bundle"), "--concurrency", "7"],
                            capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("enabled", [False, True])
def test_wipe_seals_redfish_settings_without_changing_inspection(wipe_source, enabled):
    root, original, _, collection = wipe_source
    before = (original / "preflight-vars.yaml").read_bytes()
    policy = wipe.REDFISH.policy_module(collection).validate_policy()
    policy.update(max_attempts=7, budget_seconds=240)
    write(root / "input/2-8-5-200/test01/ironic-redfish.yml", {
        wipe.REDFISH.ENABLED: enabled, wipe.REDFISH.POLICY: policy})
    bundle = seal_bundle(wipe_source, prepare(wipe_source))
    plan, runtime = wipe.verify(root, "2-8-5-200", "test01", bundle)
    selected = runtime["redfish_retry"]
    assert selected == plan["redfish_retry"]
    assert selected[wipe.REDFISH.ENABLED] is enabled
    assert selected[wipe.REDFISH.POLICY] == policy
    assert selected[wipe.REDFISH.STAGING] == str(Path(runtime["artifact_root"]) / "redfish-retry")
    assert f"Redfish adaptive reads: {enabled}" in (bundle / "review.txt").read_text()
    assert before == (original / "preflight-vars.yaml").read_bytes()
    assert "roles/ironic_deploy/tasks/prepare_redfish_retry.yml" in runtime["collection_sha256"]
    assert "plugins/module_utils/redfish_retry.py" in runtime["collection_sha256"]


def test_fresh_wipes_enable_reviewed_retries_by_default(wipe_source):
    bundle = prepare(wipe_source)
    runtime = gate.read_yaml(bundle / "runtime-plan.yml")["fleet_wipe_runtime_plan"]
    assert runtime["redfish_retry"][wipe.REDFISH.ENABLED] is True
    assert runtime["redfish_retry"][wipe.REDFISH.POLICY]["max_attempts"] == 11


@pytest.mark.parametrize("value", [None, [], {"unknown": True},
    {"ironic_deploy_redfish_retry_enabled": "true"},
    {"ironic_deploy_redfish_retry_path": "/tmp/../unsafe"},
    {"ironic_deploy_redfish_retry_policy": {"max_attempts": 11}},
    {"ironic_deploy_redfish_retry_policy": {"max_attempts": True, "budget_seconds": 300,
        "initial_delay_seconds": 5, "max_delay_seconds": 60, "jitter_fraction": 0.1}},
    {"ironic_deploy_redfish_retry_policy": {"max_attempts": 11, "budget_seconds": float("inf"),
        "initial_delay_seconds": 5, "max_delay_seconds": 60, "jitter_fraction": 0.1}}])
def test_invalid_retry_settings_create_no_preparation(wipe_source, value):
    root = wipe_source[0]
    write(root / "input/2-8-5-200/test01/ironic-redfish.yml", value)
    with pytest.raises(ValueError):
        prepare(wipe_source)
    assert not (root / "output/2-8-5-200/test01/ocp/wipe").exists()


@pytest.mark.parametrize("field", ["policy", "enabled", "path"])
def test_retry_binding_is_checked_independently_of_bundle_checksum(wipe_source, field):
    bundle = prepare(wipe_source)
    path = bundle / "runtime-plan.yml"
    document = gate.read_yaml(path)
    settings = document["fleet_wipe_runtime_plan"]["redfish_retry"]
    if field == "policy":
        settings[wipe.REDFISH.POLICY]["max_attempts"] = 1
    elif field == "enabled":
        settings[wipe.REDFISH.ENABLED] = False
    else:
        settings[wipe.REDFISH.STAGING] = "/tmp/foreign"
    write(path, document)
    with pytest.raises(ValueError, match="Redfish retry policy binding"):
        wipe.verify_sources(wipe_source[0], "2-8-5-200", "test01", bundle)


@pytest.mark.parametrize("asset", ["plugins/module_utils/redfish_retry.py",
    "plugins/module_utils/redfish_deadline.py", "plugins/modules/fleet_native_disk_clean_deadline.py",
    "roles/ironic_deploy/files/redfish_retry/rhvp_ironic_retry_launcher.py",
    "roles/ironic_deploy/files/native_disk_clean/rhvp_redfish_retry.py",
    "roles/ironic_deploy/meta/argument_specs.yml",
    "plugins/action/fleet_native_disk_clean_coordinator.py",
    "plugins/module_utils/fleet_native_disk_clean_scheduler.py",
    "playbooks/_fleet_native_disk_clean_worker.yml", "plugins/modules/redfish_read.py",
    "roles/ironic_node_power_off/tasks/main.yml", "roles/ironic_vmedia_detach/tasks/main.yml"])
def test_retry_executor_assets_are_bound(wipe_source, asset):
    bundle = seal_bundle(wipe_source, prepare(wipe_source))
    path = wipe_source[3] / asset
    path.write_text(path.read_text() + "\n# changed\n")
    with pytest.raises(ValueError, match="Executor changed"):
        wipe.verify(wipe_source[0], "2-8-5-200", "test01", bundle)


def test_conductor_deadlines_are_sealed_to_exact_bmc_selection(wipe_source):
    bundle = prepare(wipe_source)
    plan = gate.read_yaml(bundle / "plan.yml")["fleet_native_disk_clean_plan"]
    runtime = gate.read_yaml(bundle / "runtime-plan.yml")["fleet_wipe_runtime_plan"]
    assert runtime["redfish_deadlines"] == plan["redfish_deadlines"]
    assert set(plan["redfish_deadlines"]["nodes"]) == set(NAMES)
    assert all(len(value) == 64 for value in plan["redfish_deadlines"]["nodes"].values())
    runtime["redfish_deadlines"]["nodes"][NAMES[0]] = "0" * 64
    write(bundle / "runtime-plan.yml", {"fleet_wipe_runtime_plan": runtime})
    with pytest.raises(ValueError, match="deadline selection binding"):
        wipe.verify_sources(wipe_source[0], "2-8-5-200", "test01", bundle)


def test_disabled_adaptive_reads_do_not_publish_conductor_deadlines(wipe_source):
    write(wipe_source[0] / "input/2-8-5-200/test01/ironic-redfish.yml", {"ironic_deploy_redfish_retry_enabled": False})
    bundle = prepare(wipe_source)
    plan = gate.read_yaml(bundle / "plan.yml")["fleet_native_disk_clean_plan"]
    assert plan["redfish_deadlines"] == {}


def test_wipe_adapter_target_output_without_hardware(wipe_source, tmp_path):
    """Execute adapter tasks with synthetic routes, secrets and a no-service preflight role."""
    bundle = seal_bundle(wipe_source, prepare(wipe_source))
    runtime = gate.read_yaml(bundle / 'runtime-plan.yml')['fleet_wipe_runtime_plan']
    harness = tmp_path / 'adapter-output'
    playbooks = harness / 'playbooks'
    playbooks.mkdir(parents=True)
    shutil.copyfile(ROOT / 'playbooks/_fleet-wipe-credentials.yml',
                    playbooks / '_fleet-wipe-credentials.yml')
    scripts = harness / 'scripts'
    scripts.mkdir()
    capture = harness / 'calls.jsonl'
    (scripts / 'prepare-fleet-wipe.py').write_text(textwrap.dedent('''\
        import json, os, sys
        from pathlib import Path
        assert sys.argv[1] in ('seal', 'verify'), sys.argv
        with Path(os.environ['WIPE_OUTPUT_CALLS']).open('a') as stream:
            stream.write(json.dumps(sys.argv[1:]) + '\\n')
        '''))
    secrets = harness / '.era-secrets'
    secrets.mkdir()
    write(secrets / 'air-secrets.yml', {'ocp_pull_secret': 'synthetic-output-secret'})
    settings = harness / 'cluster-vars.yml'
    write(settings, {})
    runtime['cluster_vars'] = str(settings)
    # This fixture exercises real task output; existing adapter tests verify real seals.
    write(bundle / 'runtime-plan.yml', {'fleet_wipe_runtime_plan': runtime})
    write(bundle / 'plan.yml', {'fleet_native_disk_clean_plan':
                              gate.read_yaml(bundle / 'plan.yml')['fleet_native_disk_clean_plan']})
    binary = harness / 'bin'
    binary.mkdir()
    ip = binary / 'ip'
    ip.write_text('#!/usr/bin/env python3\n' + textwrap.dedent('''\
        import json, sys
        assert sys.argv[1:4] == ['-j', 'route', 'get']
        print(json.dumps([{'dst': sys.argv[4], 'dev': 'fixture0', 'prefsrc': '192.0.2.10'}]))
        '''))
    ip.chmod(0o700)
    namespace = harness / 'collections/ansible_collections/rhvp/baremetal_ocp/roles/inspection_preflight_prepare/tasks'
    namespace.mkdir(parents=True)
    write(namespace / 'main.yml', [{'name': 'Mock release resolution without container execution',
                                   'ansible.builtin.assert': {'that': 'true'}}])
    private_home = harness / 'home'
    private_home.mkdir()
    environment = {**os.environ, 'HOME': str(private_home), 'WIPE_OUTPUT_CALLS': str(capture),
                   'ANSIBLE_NOCOLOR': '1', 'ANSIBLE_STDOUT_CALLBACK': 'default',
                   'ANSIBLE_COLLECTIONS_SCAN_SYS_PATH': 'false',
                   'ANSIBLE_COLLECTIONS_PATH': str(harness / 'collections') + ':' + os.environ.get('ANSIBLE_COLLECTIONS_PATH', ''),
                   'PATH': str(binary) + ':' + os.environ['PATH']}
    values = {'fleet_wipe_runtime_plan': runtime, 'fleet_wipe_bundle': str(bundle),
              'fleet_wipe_arch': '2-8-5-200', 'fleet_wipe_site': 'test01',
              'fleet_native_disk_clean_plan': gate.read_yaml(bundle / 'plan.yml')['fleet_native_disk_clean_plan'],
              'fleet_wipe_authorization': {'fixture': True},
              'bmc_credentials': {n['name']: {'username': 'fixture', 'password': 'synthetic-output-secret'}
                                  for n in runtime['authorized_nodes']}}
    values['bmc_credentials']['unselected-node'] = {
        'username': 'unselected-fixture', 'password': 'unselected-output-secret'}
    projection = {'name': 'Verify only selected credentials were projected',
                  'ansible.builtin.assert': {'that': [
                      'fleet_native_disk_clean_driver_credentials.keys() | list | sort == '
                      '(fleet_wipe_runtime_plan.authorized_nodes | map(attribute="name") | list | sort)',
                      'fleet_native_disk_clean_driver_credentials == '
                      '(bmc_credentials | dict2items | selectattr("key", "in", '
                      'fleet_wipe_runtime_plan.authorized_nodes | map(attribute="name") | list) | items2dict)']},
                  'no_log': True}
    output = ''
    for filename in ('prepare-fleet-wipe.yml', 'wipe-fleet-install-disks.yml'):
        # The second production play imports the physical driver. Only the adapter play
        # belongs to this fixture; native driver execution has its own loopback harness.
        play = yaml.safe_load((ROOT / 'playbooks' / filename).read_text())[0]
        play['vars'] = values
        if filename == 'wipe-fleet-install-disks.yml':
            play['tasks'].append(projection)
        path = playbooks / filename
        write(path, [play])
        result = subprocess.run(['ansible-playbook', '-i', 'localhost,', str(path)],
                                env=environment, capture_output=True, text=True, timeout=120)
        (harness / (filename + '.log')).write_text(result.stdout + result.stderr)
        assert result.returncode == 0, result.stdout + result.stderr
        output += result.stdout + result.stderr
    for node in runtime['authorized_nodes']:
        assert node['name'] + ' | callback=' + node['devices']['bond_ip'] in output
        assert 'Require credentials for exactly the selected nodes | run=' + WIPE + ' | node=' + node['name'] in output
        assert 'Adapt only the selected vaulted credentials | run=' + WIPE + ' | node=' + node['name'] in output
    candidate = runtime['authorized_nodes'][0]['name']
    assert 'Prepare the same immutable release with fresh route evidence | run=' + WIPE + ' | candidate=' + candidate in output
    assert 'synthetic-output-secret' not in output
    assert 'unselected-output-secret' not in output
    assert "'item' is undefined" not in output and 'template error' not in output.lower()
    assert [json.loads(line)[0] for line in capture.read_text().splitlines()] == ['seal', 'verify']
    assert not (harness / 'runtime-started').exists()

    # A missing credential on the second node must fail while the projection is
    # still empty: validating and adapting each node in one pass would violate this.
    missing = runtime['authorized_nodes'][-1]['name']
    failure_values = {**values, 'bmc_credentials': {
        key: value for key, value in values['bmc_credentials'].items() if key != missing}}
    original = yaml.safe_load((ROOT / 'playbooks/wipe-fleet-install-disks.yml').read_text())[0]
    failure_receipt = harness / 'missing-credential.json'
    failure_play = {**original, 'vars': failure_values, 'tasks': [{
        'name': 'Exercise missing selected credentials before adaptation', 'block': original['tasks'],
        'rescue': [
            {'name': 'Require the credential projection to remain empty',
             'ansible.builtin.assert': {'that': 'fleet_native_disk_clean_driver_credentials == {}'},
             'no_log': True},
            {'name': 'Record sanitized missing-credential rejection',
             'ansible.builtin.copy': {'dest': str(failure_receipt),
                 'content': '{{ {"status": "rejected_before_adaptation", "credential_keys": '
                            'fleet_native_disk_clean_driver_credentials.keys() | list} | to_json }}',
                 'mode': '0600'}, 'no_log': True}]}]}
    failure_path = playbooks / 'missing-credential.yml'
    write(failure_path, [failure_play])
    result = subprocess.run(['ansible-playbook', '-i', 'localhost,', str(failure_path)],
                            env=environment, capture_output=True, text=True, timeout=120)
    failure_output = result.stdout + result.stderr
    (harness / 'missing-credential.log').write_text(failure_output)
    assert result.returncode == 0, failure_output
    assert json.loads(failure_receipt.read_text()) == {
        'status': 'rejected_before_adaptation', 'credential_keys': []}
    assert 'Require credentials for exactly the selected nodes | run=' + WIPE + ' | node=' + missing in failure_output
    assert 'synthetic-output-secret' not in failure_output and 'unselected-output-secret' not in failure_output
    assert "'item' is undefined" not in failure_output and 'template error' not in failure_output.lower()
    assert not (harness / 'runtime-started').exists()
