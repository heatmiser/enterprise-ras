# SPDX-License-Identifier: MIT
"""Named media policy validates modes and restricts managed rootfs endpoints."""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('media_settings', ROOT / 'scripts/abi-media-settings.py')
media = importlib.util.module_from_spec(spec)
spec.loader.exec_module(media)


def test_default_and_nonexistent_policy_are_full(tmp_path):
    assert media.site(tmp_path, 'arch', 'site') == {'mode': 'full', 'boot_artifacts_base_url': ''}


@pytest.mark.parametrize('value', [None, [], {'mode': True}, {'mode': 'unknown'},
                                  {'kargs': 'anything'}, {'mode': 'minimal'},
                                  {'mode': 'full', 'boot_artifacts_base_url': 'http://192.0.2.1:6180/redfish'}])
def test_rejects_invalid_policy(value):
    with pytest.raises(ValueError):
        media.normalize(value)


@pytest.mark.parametrize('url', ['https://192.0.2.1:6180/redfish', 'http://u:p@host:6180/redfish',
                               'http://host:6180/redfish/', 'http://host:6180/redfish?a=b',
                               'http://host:6180/redfish#x', 'http://host/redfish',
                               'http://host:6180/../redfish', 'http://host:invalid/redfish'])
def test_rejects_ambiguous_or_unmanaged_rootfs_url(url):
    with pytest.raises(ValueError):
        media.normalize({'mode': 'minimal', 'boot_artifacts_base_url': url})


def test_derivation_keeps_all_nonmedia_configuration():
    source = {'hosts': [{'hostname': 'node-a.example.test'}], 'additionalNTPSources': ['time.example.test']}
    policy = media.normalize({'mode': 'minimal', 'boot_artifacts_base_url': 'http://192.0.2.10:6180/redfish'})
    derived = media.derive(source, policy)
    assert {k: v for k, v in derived.items() if k not in ('minimalISO', 'bootArtifactsBaseURL')} == source
    assert 'minimalISO' not in source


@pytest.mark.parametrize('identity', ['../old', '/tmp/old', '.', 'x/y', 'x '])
def test_build_paths_reject_unsafe_identity(tmp_path, identity):
    with pytest.raises(ValueError):
        media.build_path(tmp_path, '20000101T000000Z', identity)


def test_manual_policy_uses_full_content_without_becoming_an_artifact_format():
    policy = media.normalize({'mode': 'manual'})
    assert policy['mode'] == 'manual'
    assert media.content(policy) == media.DEFAULTS
    assert media.derive({'hosts': []}, policy) == {'hosts': []}
