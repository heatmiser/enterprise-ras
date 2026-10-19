# SPDX-License-Identifier: MIT
"""Existing-ISO preparation rejects drift without generating or booting anything."""

import importlib.util
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import openpyxl
import pytest
import yaml

from tests.test_abi_eligibility_handoff import candidate_workspace, eligible_run
from tests.test_fleet_inspection_adapter import workspace, ROOT, RUN

COLLECTION = ROOT.parent.parent / "rhvp.baremetal_ocp"
spec = importlib.util.spec_from_file_location("abi_install", ROOT / "scripts/prepare-abi-install.py")
install = importlib.util.module_from_spec(spec)
spec.loader.exec_module(install)


@pytest.fixture
def oem_workspace(candidate_workspace):
    root, collection = candidate_workspace
    path = root / "input/2-8-5-200/test01/ocp-settings.yml"
    settings = yaml.safe_load(path.read_text())
    settings["oem"] = "dell"
    path.write_text(yaml.safe_dump(settings))
    book_path = path.parent / "2-8-5-200.xlsx"
    book = openpyxl.load_workbook(book_path)
    book["Settings"].append(["ocp_oem", "dell"])
    book.save(book_path)
    book.close()
    for role in ("ironic_deploy", "ironic_cleanup", "ironic_image_resolve"):
        shutil.copytree(COLLECTION / "roles" / role, collection / "roles" / role)
    for name in ("serve_abi_iso.yml", "_abi_iso_runtime.yml"):
        target = collection / "playbooks" / name
        target.parent.mkdir(exist_ok=True)
        shutil.copyfile(COLLECTION / "playbooks" / name, target)
    target = collection / "plugins/filter/runtime_diagnostics.py"
    target.parent.mkdir(parents=True)
    shutil.copyfile(COLLECTION / "plugins/filter/runtime_diagnostics.py", target)
    return root, collection


@pytest.fixture
def staged_install(oem_workspace, eligible_run, monkeypatch):
    root, collection = oem_workspace
    _, bundle, evidence = eligible_run
    stage = root / "output/2-8-5-200/test01/ocp-iso" / RUN
    stage.mkdir(parents=True)
    shutil.copyfile(evidence / "eligibility.yaml", stage / "eligibility.yaml")
    shutil.copyfile(root / "output/2-8-5-200/test01/ocp/endpoint-map.yaml", stage / "endpoint-map.yaml")
    plan = yaml.safe_load((bundle / "plan.yml").read_text())["inspect_fleet_plan"]
    master = next(node for node in plan["authorized_nodes"] if node["role"] == "master")
    (stage / "rendezvousIP").write_text(master["devices"]["bond_ip"] + "\n")
    (stage / "agent.x86_64.iso").write_bytes(b"fixture ISO payload\n")
    (stage / ".openshift_install_state.json").write_text('{"fixture":true}')
    (stage / "auth").mkdir()
    (stage / "auth/kubeconfig").write_text("PRIVATE_FIXTURE_CONFIG")
    (stage / "auth/kubeadmin-password").write_text("PRIVATE_FIXTURE_PASSWORD")
    monkeypatch.setattr(install.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        stdout=json.dumps([{"dev": "eno1", "prefsrc": "192.0.2.10"}])))
    return root, collection, stage


def prepare(staged_install):
    root, collection, _ = staged_install
    return install.prepare(root, "2-8-5-200", "test01", RUN, collection)


def verify(staged_install):
    root, collection, _ = staged_install
    return install.verify(root, "2-8-5-200", "test01", RUN, collection)


def test_existing_iso_and_private_state_remain_unchanged(staged_install):
    _, _, stage = staged_install
    before = {str(path): path.read_bytes() for path in stage.rglob("*") if path.is_file()}
    bundle = prepare(staged_install)
    _, plan = verify(staged_install)
    assert plan["physical_authority"] is False
    assert plan["publisher_driver"] == "idrac"
    assert plan["iso_sha256"] == install.digest(stage / "agent.x86_64.iso")
    assert not (bundle / "media").exists()
    assert not any("PRIVATE_FIXTURE" in path.read_text() for path in bundle.iterdir())
    assert before == {str(path): path.read_bytes() for path in stage.rglob("*") if path.is_file()}


@pytest.mark.parametrize("filename", ["agent.x86_64.iso", "auth/kubeconfig", "endpoint-map.yaml"])
def test_serving_rejects_changed_generated_assets(staged_install, filename):
    prepare(staged_install)
    path = staged_install[2] / filename
    path.write_bytes(path.read_bytes() + b"changed")
    with pytest.raises(ValueError):
        verify(staged_install)


def test_serving_rejects_changed_membership(staged_install):
    bundle = prepare(staged_install)
    document = yaml.safe_load((bundle / "plan.yml").read_text())
    document["abi_install_plan"]["node_names"].pop()
    (bundle / "plan.yml").write_text(yaml.safe_dump(document))
    with pytest.raises(ValueError, match="Prepared plan changed"):
        verify(staged_install)


def test_serving_rejects_collection_drift(staged_install):
    prepare(staged_install)
    path = staged_install[1] / "playbooks/_abi_iso_runtime.yml"
    path.write_text(path.read_text() + "\n# changed\n")
    with pytest.raises(ValueError, match="Collection serving implementation changed"):
        verify(staged_install)


def test_preparation_cannot_overwrite_a_reviewed_plan(staged_install):
    prepare(staged_install)
    with pytest.raises(ValueError, match="preparation exists"):
        prepare(staged_install)


def test_changed_route_blocks_serving(staged_install, monkeypatch):
    prepare(staged_install)
    monkeypatch.setattr(install.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        stdout=json.dumps([{"dev": "eno2", "prefsrc": "192.0.2.11"}])))
    with pytest.raises(ValueError, match="Routes or BMC identities changed"):
        verify(staged_install)


@pytest.fixture
def served_install(staged_install):
    root, collection, stage = staged_install
    for path in (COLLECTION / 'roles/abi_install_driver').rglob('*.yml'):
        target = collection / path.relative_to(COLLECTION)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    shutil.copyfile(COLLECTION / 'playbooks/install_abi.yml', collection / 'playbooks/install_abi.yml')
    bundle = prepare(staged_install)
    _, plan = verify(staged_install)
    attempt = Path(plan['reports_dir']) / 'attempt-fixture'
    attempt.mkdir(parents=True)
    (attempt / 'plan.yml').write_text(yaml.safe_dump({'abi_install_plan': plan}))
    receipt = {'schema_version': 1, 'status': 'SERVING', 'attempt': str(attempt),
               'inspection_run_id': RUN, 'node_names': plan['node_names'],
               'iso_sha256': plan['iso_sha256'], 'runtime_retained': True, 'lock_owned': True,
               'ownership_confirmed': True, 'physical_execution': False,
               'url': 'http://192.0.2.10:6180/redfish/agent.x86_64.iso',
               'ironic_image': 'registry.example.test/ironic@sha256:' + 'd' * 64}
    (attempt / 'runtime.yaml').write_text(yaml.safe_dump(receipt))
    lock = root / 'runtime.lock'
    lock.mkdir()
    (lock / 'owner').write_text(str(attempt) + '\n')
    return root, collection, bundle, attempt, lock


def test_boot_preparation_adopts_serving_without_mutating_it(served_install):
    root, collection, bundle, attempt, lock = served_install
    before = {p: p.read_bytes() for p in (attempt / 'runtime.yaml', lock / 'owner', bundle / 'plan.yml')}
    target = install.prepare_boot(root, '2-8-5-200', 'test01', RUN, collection, lock)
    _, plan = install.verify_boot(root, '2-8-5-200', 'test01', RUN, collection, lock)
    assert target == bundle / 'boot'
    assert plan['node_names'] == plan['serving_plan']['node_names']
    mapping = yaml.safe_load((Path(plan['installer_dir']) / 'endpoint-map.yaml').read_text())['rhcos_to_physical']
    assert plan['expected_cluster_nodes'] == sorted(
        hostname for hostname, physical in mapping.items() if physical in plan['node_names'])
    defaults = yaml.safe_load((COLLECTION / 'roles/abi_install_driver/defaults/main.yml').read_text())
    assert plan['serving_plan']['oem'] == 'dell'
    assert plan['profile'] == install.OEM_BOOT_PROFILES['dell']
    assert plan['profile'] == defaults['abi_install_driver_oem_boot_profiles']['dell']
    assert plan['profile']['enabled'] == 'once' and plan['profile']['reset_type'] == 'none'
    assert plan['automated_clean'] is False and plan['workflow_ready'] is True
    assert plan['installer_dir'] == str(Path(plan['serving_plan']['iso_path']).parent)
    assert plan['physical_authority'] is False
    assert all(node['system_id'] == 'System.Embedded.1' for node in plan['nodes'])
    assert before == {p: p.read_bytes() for p in before}
    assert not Path(plan['artifact_root']).exists()


def test_readiness_membership_cannot_be_removed_from_a_resealed_plan(served_install):
    root, collection, bundle, _, lock = served_install
    target = install.prepare_boot(root, '2-8-5-200', 'test01', RUN, collection, lock)
    document = yaml.safe_load((target / 'plan.yml').read_text())
    document['abi_boot_plan']['expected_cluster_nodes'].pop()
    (target / 'plan.yml').write_text(yaml.safe_dump(document))
    (target / 'prepared.yaml').write_text(yaml.safe_dump({
        'plan_sha256': install.state.fingerprint(document['abi_boot_plan'])}))
    with pytest.raises(ValueError, match='Expected Kubernetes membership changed'):
        install.verify_boot(root, '2-8-5-200', 'test01', RUN, collection, lock)


@pytest.mark.parametrize('change', ['owner', 'receipt', 'implementation', 'membership', 'oem_profile'])
def test_boot_verification_rejects_changed_handoff(served_install, change):
    root, collection, bundle, attempt, lock = served_install
    install.prepare_boot(root, '2-8-5-200', 'test01', RUN, collection, lock)
    if change == 'owner':
        (lock / 'owner').write_text(str(attempt.parent / 'attempt-other'))
    elif change == 'receipt':
        path = attempt / 'runtime.yaml'
        receipt = yaml.safe_load(path.read_text())
        receipt['status'] = 'FAILED'
        path.write_text(yaml.safe_dump(receipt))
    elif change == 'implementation':
        path = collection / 'roles/abi_install_driver/tasks/boot.yml'
        path.write_text(path.read_text() + '\n# changed\n')
    else:
        path = bundle / 'boot/plan.yml'
        value = yaml.safe_load(path.read_text())
        if change == 'membership':
            value['abi_boot_plan']['node_names'].pop()
        else:
            value['abi_boot_plan']['profile']['enabled'] = 'continuous'
        path.write_text(yaml.safe_dump(value))
    with pytest.raises(ValueError):
        install.verify_boot(root, '2-8-5-200', 'test01', RUN, collection, lock)


@pytest.mark.parametrize('authorization', ['', 'another-run', RUN])
def test_operator_entrypoint_cannot_boot_before_monitoring_is_ready(served_install, monkeypatch, authorization):
    root, collection, _, _, lock = served_install
    install.prepare_boot(root, '2-8-5-200', 'test01', RUN, collection, lock)
    _, plan = install.verify_boot(root, '2-8-5-200', 'test01', RUN, collection, lock)
    plan['workflow_ready'] = False
    monkeypatch.setattr(install, 'verify_boot', lambda *args: (root, plan))
    monkeypatch.setattr(install.subprocess, 'run', lambda *args, **kwargs: pytest.fail('Unexpected execution'))
    with pytest.raises(ValueError, match='Explicit FLEET_AUTHORIZE_BOOT_RUN|Boot execution disabled'):
        install.boot(root, '2-8-5-200', 'test01', RUN, collection, SimpleNamespace(fd=123), authorization)
    assert not Path(plan['artifact_root']).exists()


def test_boot_preparation_refuses_replacement(served_install):
    root, collection, _, _, lock = served_install
    install.prepare_boot(root, '2-8-5-200', 'test01', RUN, collection, lock)
    with pytest.raises(ValueError, match='Boot preparation exists'):
        install.prepare_boot(root, '2-8-5-200', 'test01', RUN, collection, lock)


def test_boot_rejects_unimplemented_workbook_oem_before_artifacts(served_install, monkeypatch):
    root, collection, bundle, _, lock = served_install
    serving = {'oem': 'unsupported-oem'}
    monkeypatch.setattr(install, 'verify', lambda *args: (bundle, serving))
    with pytest.raises(ValueError, match='unavailable for workbook OEM'):
        install.prepare_boot(root, '2-8-5-200', 'test01', RUN, collection, lock)
    assert not (bundle / 'boot').exists()


def test_monitoring_implementation_drift_blocks_execution(served_install):
    root, collection, _, _, lock = served_install
    install.prepare_boot(root, '2-8-5-200', 'test01', RUN, collection, lock)
    path = collection / 'roles/abi_install_driver/tasks/monitor.yml'
    path.write_text(path.read_text() + '\n# drift\n')
    with pytest.raises(ValueError, match='Boot implementation changed'):
        install.verify_boot(root, '2-8-5-200', 'test01', RUN, collection, lock)


@pytest.mark.parametrize('receipt', [
    {'boot_complete': False, 'runtime_retained': True},
    {'boot_complete': True, 'runtime_retained': False},
    {'boot_complete': True, 'runtime_retained': True, 'cleanup_started': True},
])
def test_resume_cannot_replay_boot_or_interrupt_shared_cleanup(served_install, monkeypatch, receipt):
    root, collection, _, _, lock = served_install
    install.prepare_boot(root, '2-8-5-200', 'test01', RUN, collection, lock)
    _, plan = install.verify_boot(root, '2-8-5-200', 'test01', RUN, collection, lock)
    artifact = Path(plan['artifact_root'])
    artifact.mkdir(parents=True)
    (artifact / 'runtime.yaml').write_text(yaml.safe_dump(receipt))
    secrets = collection / 'inventories/test01/secrets.yaml'
    secrets.write_text('fixture: vaulted')
    monkeypatch.setattr(install, 'verify_boot', lambda *args: (root, plan))
    monkeypatch.setattr(install.subprocess, 'run', lambda *a, **k: pytest.fail('Unexpected boot replay'))
    with pytest.raises(ValueError, match='Resume requires completed boot'):
        install.boot(root, '2-8-5-200', 'test01', RUN, collection, SimpleNamespace(fd=123), RUN, resume=True)


def test_resume_passes_observation_action_without_replacing_original_plan(served_install, monkeypatch):
    root, collection, _, _, lock = served_install
    target = install.prepare_boot(root, '2-8-5-200', 'test01', RUN, collection, lock)
    _, plan = install.verify_boot(root, '2-8-5-200', 'test01', RUN, collection, lock)
    artifact = Path(plan['artifact_root'])
    artifact.mkdir(parents=True)
    (artifact / 'runtime.yaml').write_text(yaml.safe_dump({'boot_complete': True, 'runtime_retained': True}))
    (artifact / 'plan.yml').write_bytes((target / 'plan.yml').read_bytes())
    secrets = collection / 'inventories/test01/secrets.yaml'
    secrets.parent.mkdir(parents=True, exist_ok=True)
    secrets.write_text('fixture: vaulted')
    calls = []
    monkeypatch.setattr(install, 'verify_boot', lambda *args: (root, plan))
    monkeypatch.setattr(install.subprocess, 'run', lambda *a, **k: calls.append((a, k)) or SimpleNamespace(returncode=0))
    install.boot(root, '2-8-5-200', 'test01', RUN, collection, SimpleNamespace(fd=123), RUN, resume=True)
    assert 'abi_install_driver_action=resume' in calls[0][0][0]
    assert (artifact / 'plan.yml').read_bytes() == (target / 'plan.yml').read_bytes()
