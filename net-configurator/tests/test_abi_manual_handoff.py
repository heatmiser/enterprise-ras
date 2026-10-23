# SPDX-License-Identifier: MIT
"""Manual gates require terminal attestation and never load BMC credentials."""
import json
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from tests.test_abi_install_preparation import (
    install, served_install, staged_install, oem_workspace, RUN,
    candidate_workspace, eligible_run, workspace,
)


@pytest.fixture
def manual_site(staged_install):
    root, _, _ = staged_install
    (root / 'input/2-8-5-200/test01/abi-media.yml').write_text('mode: manual\n')


@pytest.fixture
def manual_plan(manual_site, request):
    root, collection, _, _, lock = request.getfixturevalue('served_install')
    target = install.prepare_boot(root, '2-8-5-200', 'test01', RUN, collection, lock)
    _, plan = install.verify_boot(root, '2-8-5-200', 'test01', RUN, collection, lock)
    return root, collection, target, plan


def invoke(data, monkeypatch, **kwargs):
    root, collection, target, plan = data
    monkeypatch.setattr(install, 'verify_boot', lambda *args: (target, plan))
    return install.boot(root, '2-8-5-200', 'test01', RUN, collection, SimpleNamespace(fd=123), RUN, **kwargs)


def record(plan):
    return install.state.read(Path(plan['artifact_root']) / 'operator-handoff.yaml')


def test_manual_preparation_has_no_bmc_boot_targets(manual_plan):
    _, collection, target, plan = manual_plan
    assert plan['profile'] == {'method': 'operator_manual', 'media_owner': 'operator'}
    assert all(set(node) == {'name'} for node in plan['nodes'])
    assert not (collection / 'inventories/test01/secrets.yaml').exists()
    assert 'Operator inserts reviewed media' in (target / 'review.txt').read_text()
    assert 'manual_handoff.yml' in ' '.join(plan['collection_sha256'])


def test_noninteractive_rejection_persists_waiting_and_never_runs_ansible(manual_plan, monkeypatch):
    monkeypatch.setattr(install.sys.stdin, 'isatty', lambda: False)
    monkeypatch.setattr(install.subprocess, 'run', lambda *a, **k: pytest.fail('Observation started before confirmation'))
    with pytest.raises(ValueError, match='interactive terminal'):
        invoke(manual_plan, monkeypatch)
    assert record(manual_plan[3])['status'] == 'WAITING_FOR_OPERATOR'
    assert 'confirmed_at' not in record(manual_plan[3])


@pytest.mark.parametrize('interruption', [KeyboardInterrupt, EOFError])
def test_interrupted_gate_continues_once_without_vault_or_reconfirmation(manual_plan, monkeypatch, interruption):
    monkeypatch.setattr(install.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr('builtins.input', lambda *a: (_ for _ in ()).throw(interruption()))
    monkeypatch.setattr(install.subprocess, 'run', lambda *a, **k: pytest.fail('Observation started before confirmation'))
    with pytest.raises(ValueError, match='remains waiting'):
        invoke(manual_plan, monkeypatch)
    assert record(manual_plan[3])['status'] == 'WAITING_FOR_OPERATOR'
    calls = []

    def launched(command, **kwargs):
        assert record(manual_plan[3])['status'] == 'OPERATOR_CONFIRMED'
        assert record(manual_plan[3])['confirmed_at']
        calls.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr('builtins.input', lambda *a: '')
    monkeypatch.setattr(install.subprocess, 'run', launched)
    invoke(manual_plan, monkeypatch, continuing=True)
    assert 'abi_install_driver_action=install' in calls[0]
    assert not any('vault' in token or 'secrets.yaml' in token for token in calls[0])
    before = record(manual_plan[3])
    monkeypatch.setattr('builtins.input', lambda *a: pytest.fail('Repeated confirmation'))
    invoke(manual_plan, monkeypatch, continuing=True)
    assert record(manual_plan[3]) == before


@pytest.mark.parametrize('field,value', [
    ('installation_attempt_id', 'wrong-attempt'), ('node_names', ['another-node']),
    ('plan_sha256', '0' * 64), ('iso_sha256', '0' * 64), ('iso_url', 'http://other/media.iso'),
])
def test_gate_rejects_altered_confirmation_binding(manual_plan, monkeypatch, field, value):
    plan = manual_plan[3]
    monkeypatch.setattr(install.sys.stdin, 'isatty', lambda: False)
    with pytest.raises(ValueError):
        invoke(manual_plan, monkeypatch)
    path = Path(plan['artifact_root']) / 'operator-handoff.yaml'
    altered = record(plan)
    altered[field] = value
    install.state.write(path, altered)
    monkeypatch.setattr(install.subprocess, 'run', lambda *a, **k: pytest.fail('Altered confirmation launched observation'))
    with pytest.raises(ValueError, match='binding changed'):
        invoke(manual_plan, monkeypatch, continuing=True)


def test_confirmed_resume_preserves_attestation_and_does_not_repeat_prompt(manual_plan, monkeypatch):
    plan = manual_plan[3]
    monkeypatch.setattr(install.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr('builtins.input', lambda *a: '')
    calls = []
    monkeypatch.setattr(install.subprocess, 'run', lambda command, **kw: calls.append(command) or SimpleNamespace(returncode=0))
    invoke(manual_plan, monkeypatch)
    artifact = Path(plan['artifact_root'])
    before = (artifact / 'operator-handoff.yaml').read_bytes()
    install.state.write(artifact / 'runtime.yaml', {'operator_confirmed': True, 'runtime_retained': True, 'boot_complete': False})
    monkeypatch.setattr('builtins.input', lambda *a: pytest.fail('Resume requested another boot confirmation'))
    monkeypatch.setattr(install.sys.stdin, 'isatty', lambda: False)
    invoke(manual_plan, monkeypatch, resume=True)
    assert 'abi_install_driver_action=resume' in calls[-1]
    assert (artifact / 'operator-handoff.yaml').read_bytes() == before


def test_invalid_authorization_cannot_create_manual_gate(manual_plan, monkeypatch):
    root, collection, target, plan = manual_plan
    monkeypatch.setattr(install, 'verify_boot', lambda *args: (target, plan))
    with pytest.raises(ValueError, match='must match'):
        install.boot(root, '2-8-5-200', 'test01', RUN, collection, SimpleNamespace(fd=123), 'other')
    assert not Path(plan['artifact_root']).exists()


def test_actual_terminal_enter_releases_gate_only_after_confirmation(manual_plan, tmp_path):
    plan = manual_plan[3]
    path = tmp_path / 'plan.json'
    path.write_text(json.dumps(plan))
    script = (
        'import importlib.util,json,sys; '
        's=importlib.util.spec_from_file_location("gate",sys.argv[1]); '
        'm=importlib.util.module_from_spec(s); s.loader.exec_module(m); '
        'm.manual_handoff(json.load(open(sys.argv[2])))'
    )
    master, slave = pty.openpty()
    child = subprocess.Popen([sys.executable, '-c', script, str(install.ROOT / 'scripts/prepare-abi-install.py'), str(path)],
                             stdin=slave, stdout=slave, stderr=slave)
    os.close(slave)
    output = b''
    try:
        deadline = time.monotonic() + 10
        while b'Press Enter' not in output and time.monotonic() < deadline:
            if select.select([master], [], [], .1)[0]:
                output += os.read(master, 8192)
        assert b'Press Enter' in output, output
        assert child.poll() is None
        assert record(plan)['status'] == 'WAITING_FOR_OPERATOR'
        os.write(master, b'\n')
        assert child.wait(timeout=10) == 0
        assert record(plan)['status'] == 'OPERATOR_CONFIRMED'
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        os.close(master)
