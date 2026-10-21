"""Operator retirement binds historical ownership independently of current deployment code."""
import importlib.util
import json
from pathlib import Path
import shutil

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
COLLECTION = ROOT.parent.parent / 'rhvp.baremetal_ocp'
spec = importlib.util.spec_from_file_location('retirement_adapter', ROOT / 'scripts/prepare-abi-retirement.py')
retirement = importlib.util.module_from_spec(spec)
spec.loader.exec_module(retirement)
RUN = '20000101T000000Z'
ATTEMPT = '20000102T000000Z-aabbccdd'
RELEASE = 'registry.example.test/release@sha256:' + 'e' * 64


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value, sort_keys=False))


def build_history(tmp_path, attempt=ATTEMPT, healthy=False, cleaned=False):
    root = tmp_path / 'workspace'
    base = root / 'output/2-8-5-200/test01'
    bundle, reports = retirement.paths(base, RUN, attempt)
    owner = reports / 'serving/attempt-fixture'
    serving = {'schema_version': 1, 'operation': 'serve_existing_abi_iso', 'inspection_run_id': RUN,
               'node_names': ['node-a', 'node-b'], 'iso_sha256': 'a' * 64, 'media_dir': str(bundle / 'media'),
               'bundle': str(bundle), 'reports_dir': str(reports / 'serving'), 'api_port': 6385,
               'firewall_zone': 'public', 'collection_sha256': {'old-executor.yml': 'f' * 64}}
    receipt = {'schema_version': 1, 'status': 'SERVING', 'attempt': str(owner),
               'inspection_run_id': RUN, 'node_names': serving['node_names'], 'iso_sha256': serving['iso_sha256'],
               'ownership_confirmed': True, 'lock_owned': True, 'runtime_retained': True,
               'ironic_image': 'registry.example.test/ironic@sha256:' + 'b' * 64,
               'firewall_rules': [{'source': '192.0.2.1', 'port': 6180}]}
    boot = {'schema_version': 1, 'inspection_run_id': RUN, 'serving_attempt': str(owner),
            'node_names': serving['node_names'], 'status': 'SUCCEEDED' if healthy else 'FAILED',
            'bootstrap_complete': True, 'installation_complete': True, 'fleet_ready': healthy,
            'cleanup_status': 'complete' if cleaned else 'pending', 'runtime_retained': not cleaned}
    if attempt != RUN:
        for document in (serving, receipt, boot):
            document['installation_attempt_id'] = attempt
    write(bundle / 'plan.yml', {'abi_install_plan': serving})
    write(bundle / 'prepared.yaml', {'plan_sha256': retirement.state.fingerprint(serving)})
    write(owner / 'plan.yml', {'abi_install_plan': serving})
    write(owner / 'runtime.yaml', receipt)
    write(reports / 'boot/runtime.yaml', boot)
    lock = root / 'runtime.lock'
    if not cleaned:
        lock.mkdir()
        (lock / 'owner').write_text(str(owner) + '\n')
    return root, base, bundle, reports, owner, lock


def prepare(history, attempt=ATTEMPT, collection=COLLECTION):
    root, _, _, _, _, lock = history
    return retirement.prepare(root, '2-8-5-200', 'test01', RUN, attempt, collection,
                              'Voluntary replacement', RELEASE, lock)


def make_completed(history):
    root, base, bundle, reports, owner, lock = history
    plan = retirement.state.read(bundle / 'retirement/plan.yml')['abi_runtime_retirement_plan']
    archive = reports / 'retirement/service-logs'
    archive.mkdir(parents=True)
    files = {}
    for name in ('metal3-ironic-ironic.fixture.log', 'metal3-ironic-httpd.fixture.log'):
        path = archive / name
        path.write_text('complete private fixture log\n')
        files[name] = retirement.digest(path)
    seal = retirement.state.fingerprint(plan)
    write(reports / 'retirement/archive.yaml', {'plan_sha256': seal, 'source_sha256': plan['source_sha256'], 'files': files})
    receipt = {'schema_version': 1, 'operation': 'retire_abi_runtime', 'status': 'RETIRED',
               'installation_attempt_id': plan['installation_attempt_id'], 'inspection_run_id': RUN,
               'serving_attempt': str(owner), 'plan_sha256': seal, 'source_sha256': plan['source_sha256'],
               'original_status': plan['original_status'], 'node_names': plan['node_names'],
               'cleanup_verified': True, 'resources_absent': True, 'lock_released': True,
               'archive_verified': True, 'node_mutations': False}
    write(reports / 'retirement/receipt.yaml', receipt)
    if lock.exists():
        (lock / 'owner').unlink()
        lock.rmdir()
    return receipt


@pytest.mark.parametrize('attempt', [RUN, ATTEMPT])
@pytest.mark.parametrize('healthy,cleaned', [(False, False), (True, False), (True, True)])
def test_explicit_historical_target_preserved_without_current_code_or_cluster_calls(tmp_path, attempt, healthy, cleaned, monkeypatch):
    history = build_history(tmp_path, attempt, healthy, cleaned)
    before = {p: p.read_bytes() for p in history[3].rglob('*') if p.is_file()}
    monkeypatch.setattr(retirement.subprocess, 'run', lambda *a, **kw: pytest.fail('Preparation must not run services/API'))
    target = prepare(history, attempt)
    plan = retirement.verify(history[0], '2-8-5-200', 'test01', RUN, attempt, COLLECTION)
    assert plan['mode'] == ('already_cleaned' if cleaned else 'retained')
    assert plan['original_status']['status'] == ('SUCCEEDED' if healthy else 'FAILED')
    assert plan['node_mutations'] is False
    assert all(p.read_bytes() == data for p, data in before.items())
    assert 'No node power' in (target / 'review.txt').read_text()


@pytest.mark.parametrize('change', ['owner', 'seal', 'missing_lock', 'monitor', 'symlink'])
def test_preparation_rejects_unbound_history(tmp_path, change):
    h = build_history(tmp_path)
    if change == 'owner':
        (h[5] / 'owner').write_text('/unrelated/attempt-foreign')
    elif change == 'seal':
        write(h[2] / 'prepared.yaml', {'plan_sha256': 'f' * 64})
    elif change == 'missing_lock':
        (h[5] / 'owner').unlink()
        h[5].rmdir()
    elif change == 'monitor':
        (h[3] / 'boot/monitor.claim').mkdir()
    else:
        p = h[4] / 'runtime.yaml'
        saved = p.with_suffix('.saved')
        p.rename(saved)
        p.symlink_to(saved)
    with pytest.raises((ValueError, FileNotFoundError)):
        prepare(h)
    assert not (h[2] / 'retirement').exists()


@pytest.mark.parametrize('change', ['plan', 'source', 'executor'])
def test_execution_binding_rejects_drift(tmp_path, change):
    h = build_history(tmp_path)
    collection = tmp_path / 'collection'
    for name in retirement.executor_hashes(COLLECTION):
        target = collection / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(COLLECTION / name, target)
    prepare(h, collection=collection)
    if change == 'plan':
        p = h[2] / 'retirement/plan.yml'
        data = retirement.state.read(p)
        data['abi_runtime_retirement_plan']['node_names'] = ['foreign']
        write(p, data)
    elif change == 'source':
        (h[4] / 'runtime.yaml').write_text('changed: true')
    else:
        (collection / 'roles/abi_runtime_retirement/tasks/main.yml').write_text('[]')
    with pytest.raises(ValueError):
        retirement.verify(h[0], '2-8-5-200', 'test01', RUN, ATTEMPT, collection)


def test_exact_authorization_before_any_cleanup(tmp_path, monkeypatch):
    h = build_history(tmp_path)
    prepare(h)
    monkeypatch.setattr(retirement.subprocess, 'run', lambda *a, **kw: pytest.fail('No authorized execution'))
    with pytest.raises(ValueError, match='Exact'):
        retirement.execute(h[0], '2-8-5-200', 'test01', RUN, ATTEMPT, COLLECTION, 'wrong', None)


def test_receipt_is_independent_and_archive_required(tmp_path):
    h = build_history(tmp_path)
    prepare(h)
    before = (h[3] / 'boot/runtime.yaml').read_bytes()
    make_completed(h)
    assert retirement.completed(h[1], RUN, ATTEMPT)['status'] == 'RETIRED'
    assert (h[3] / 'boot/runtime.yaml').read_bytes() == before
    next((h[3] / 'retirement/service-logs').iterdir()).write_text('changed')
    with pytest.raises(ValueError, match='archive changed'):
        retirement.completed(h[1], RUN, ATTEMPT)


def test_intended_release_and_reason_are_explicit(tmp_path):
    h = build_history(tmp_path)
    with pytest.raises(ValueError):
        retirement.prepare(h[0], '2-8-5-200', 'test01', RUN, ATTEMPT, COLLECTION, '', RELEASE, h[5])
    with pytest.raises(ValueError):
        retirement.prepare(h[0], '2-8-5-200', 'test01', RUN, ATTEMPT, COLLECTION, 'rebuild', 'latest', h[5])


@pytest.mark.parametrize('field,value', [('status', 'SUCCEEDED'), ('cleanup_verified', False),
    ('resources_absent', False), ('lock_released', False), ('archive_verified', False),
    ('node_mutations', True), ('serving_attempt', '/foreign/attempt-other'), ('node_names', ['foreign'])])
def test_incomplete_or_foreign_receipt_cannot_authorize_replacement(tmp_path, field, value):
    h = build_history(tmp_path)
    prepare(h)
    receipt = make_completed(h)
    receipt[field] = value
    write(h[3] / 'retirement/receipt.yaml', receipt)
    with pytest.raises(ValueError, match='retirement receipt'):
        retirement.completed(h[1], RUN, ATTEMPT)
