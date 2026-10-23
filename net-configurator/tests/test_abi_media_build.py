# SPDX-License-Identifier: MIT
"""Exercise derived builds and serving bindings with mocked native image tooling."""
import importlib.util
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from tests.test_abi_eligibility_handoff import candidate_workspace, eligible_run, RUN, ROOT, IMAGE
from tests.test_fleet_inspection_adapter import workspace
from tests.test_abi_install_preparation import oem_workspace, staged_install, install

spec = importlib.util.spec_from_file_location('media_builder', ROOT / 'scripts/build-abi-media.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)
URL = 'http://192.0.2.10:6180/redfish'


COLLECTION = ROOT.parent.parent / 'rhvp.baremetal_ocp'
fixture_spec = importlib.util.spec_from_file_location('media_native_fixtures', COLLECTION / 'tests/scripts/test_abi_media.py')
fixtures = importlib.util.module_from_spec(fixture_spec)
fixture_spec.loader.exec_module(fixtures)


def native(root, mode, calls, fault=''):
    image, _, _ = builder.gate.validate_handoff(root, '2-8-5-200', 'test01', RUN)
    base = root / 'output/2-8-5-200/test01/ocp/inspection/fleet' / RUN
    release = builder.gate.read_yaml(base / 'preflight-vars.yaml')['inspection_preflight_provenance']['release_image']
    mock = fixtures.FakeNative(mode=mode, image=image, release=release, fault=fault)
    def run(argv, **kwargs):
        calls.append(argv)
        return mock(argv, **kwargs)
    return run


@pytest.fixture(autouse=True)
def host_inspector(tmp_path, monkeypatch):
    host = tmp_path / 'host-lsinitrd'
    host.write_text('#!/bin/sh\n')
    host.chmod(0o700)
    original = builder.build
    def build(*args, **kwargs):
        kwargs.setdefault('lsinitrd', str(host))
        kwargs.setdefault('collection_root', COLLECTION)
        return original(*args, **kwargs)
    monkeypatch.setattr(builder, 'build', build)


def policy(root, mode):
    value = {'mode': mode, 'boot_artifacts_base_url': URL if mode == 'minimal' else ''}
    (root / 'input/2-8-5-200/test01/abi-media.yml').write_text(yaml.safe_dump(value))


@pytest.mark.parametrize('mode', ['full', 'full-ram', 'minimal', 'manual'])
def test_build_isolated_variants_preserve_sources_and_bind_exact_artifacts(eligible_run, mode):
    root, bundle, _ = eligible_run
    original = root / 'output/2-8-5-200/test01/ocp'
    before = {p: p.read_bytes() for p in original.iterdir() if p.is_file()}
    policy(root, mode)
    calls = []
    stage = builder.build(root, '2-8-5-200', 'test01', RUN, 'variant-a', run=native(root, mode, calls))
    assert all(p.read_bytes() == content for p, content in before.items())
    assert IMAGE in calls[0] and calls[0][calls[0].index('--entrypoint') + 1] == 'openshift-install'
    image, candidates, _ = builder.gate.validate_handoff(root, '2-8-5-200', 'test01', RUN)
    release = builder.gate.read_yaml(bundle / 'preflight-vars.yaml')['inspection_preflight_provenance']['release_image']
    receipt = install.media_build(stage, RUN, 'variant-a', image, release, candidates)
    assert len(receipt['artifacts']) == (2 if mode == 'minimal' else 1)
    assert receipt['policy']['mode'] == ('full' if mode == 'manual' else mode)
    if mode == 'full-ram':
        assert any('--append' in c and 'coreos.liveiso.fromram=1' in c for c in calls)
    with pytest.raises(ValueError, match='destination exists'):
        builder.build(root, '2-8-5-200', 'test01', RUN, 'variant-a', run=native(root, mode, calls))


@pytest.mark.parametrize('mode,fault', [('minimal', 'missing-rootfs'), ('full-ram', 'wrong-kargs')])
def test_failed_variant_has_no_successful_build_receipt(eligible_run, mode, fault):
    root, _, _ = eligible_run
    policy(root, mode)
    with pytest.raises(ValueError):
        builder.build(root, '2-8-5-200', 'test01', RUN, 'failed-build', run=native(root, mode, [], fault))
    assert not (root / f'output/2-8-5-200/test01/ocp-iso/{RUN}/builds/failed-build/media-build.yaml').exists()


def test_variant_requires_explicit_build_identity(eligible_run):
    root, _, _ = eligible_run
    policy(root, 'full-ram')
    with pytest.raises(ValueError, match='BUILD_ID'):
        builder.build(root, '2-8-5-200', 'test01', RUN)


@pytest.mark.parametrize('changed', ['rootfs', 'derived', 'url', 'source'])
def test_preparation_rejects_artifact_or_derived_manifest_drift(eligible_run, changed):
    root, bundle, _ = eligible_run
    policy(root, 'minimal')
    stage = builder.build(root, '2-8-5-200', 'test01', RUN, 'minimal-a', run=native(root, 'minimal', []))
    image, candidates, _ = builder.gate.validate_handoff(root, '2-8-5-200', 'test01', RUN)
    release = builder.gate.read_yaml(bundle / 'preflight-vars.yaml')['inspection_preflight_provenance']['release_image']
    if changed in ('rootfs', 'derived'):
        (stage / ('boot-artifacts/agent.x86_64-rootfs.img' if changed == 'rootfs' else 'media-agent-config.yaml')).write_bytes(b'changed')
    else:
        path = stage / 'media-build.yaml'
        receipt = yaml.safe_load(path.read_text())
        if changed == 'url': receipt['policy']['boot_artifacts_base_url'] = 'http://192.0.2.11:6180/redfish'
        else: receipt['source_manifest_sha256']['agent-config.yaml'] = '0' * 64
        path.write_text(yaml.safe_dump(receipt))
    with pytest.raises(ValueError):
        install.media_build(stage, RUN, 'minimal-a', image, release, candidates)


def test_minimal_preparation_adds_node_sources_and_seals_variant(staged_install, monkeypatch):
    root, collection, old_stage = staged_install
    shutil.copytree(COLLECTION / 'plugins', collection / 'plugins', dirs_exist_ok=True)
    old_bytes = (old_stage / 'agent.x86_64.iso').read_bytes()
    policy(root, 'minimal')
    stage = builder.build(root, '2-8-5-200', 'test01', RUN, 'minimal-a', run=native(root, 'minimal', []))
    for name in ('rendezvousIP', '.openshift_install_state.json', 'auth/kubeconfig', 'auth/kubeadmin-password'):
        target = stage / name
        target.parent.mkdir(exist_ok=True)
        target.write_bytes((old_stage / name).read_bytes())
    monkeypatch.setenv('ABI_MEDIA_BUILD_ID', 'minimal-a')
    install.prepare(root, '2-8-5-200', 'test01', RUN, collection)
    monkeypatch.setenv('ABI_MEDIA_BUILD_ID', 'unrelated-runtime-selection')
    _, plan = install.verify(root, '2-8-5-200', 'test01', RUN, collection)
    assert plan['media']['mode'] == 'minimal'
    assert {n['callback_ip'] for n in plan['nodes']} <= {r['source'] for r in plan['firewall_rules']}
    assert plan['iso_path'] == str(stage / 'agent.x86_64.iso')
    assert (old_stage / 'agent.x86_64.iso').read_bytes() == old_bytes


@pytest.mark.parametrize('changed', ['schema', 'validation', 'tool', 'verifier', 'legacy-ram'])
def test_preparation_rejects_obsolete_or_modified_validation(eligible_run, changed):
    root, bundle, _ = eligible_run
    policy(root, 'full-ram')
    stage = builder.build(root, '2-8-5-200', 'test01', RUN, 'ram-a', run=native(root, 'full-ram', []))
    image, candidates, _ = builder.gate.validate_handoff(root, '2-8-5-200', 'test01', RUN)
    release = builder.gate.read_yaml(bundle / 'preflight-vars.yaml')['inspection_preflight_provenance']['release_image']
    path = stage / 'media-build.yaml'
    build = yaml.safe_load(path.read_text())
    evidence = stage / build['validation']['source']
    record = json.loads(evidence.read_text())
    if changed == 'schema':
        build['schema_version'] = 1
    elif changed == 'verifier':
        build['verifier_sha256'] = '0' * 64
    elif changed == 'legacy-ram':
        build['kernel_arguments'] = 'rd.live.ram=1'
    else:
        if changed == 'tool':
            record['coreos_installer']['image'] = IMAGE
        else:
            record['cleanup_verified'] = False
        evidence.write_text(json.dumps(record))
        build['validation']['sha256'] = builder.digest(evidence)
    path.write_text(yaml.safe_dump(build))
    with pytest.raises(ValueError):
        install.media_build(stage, RUN, 'ram-a', image, release, candidates)
