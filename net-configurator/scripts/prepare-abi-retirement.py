#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Prepare and authorize retirement of one recorded ABI runtime, preserving cluster outcomes."""
import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import yaml

ROOT = Path(__file__).resolve().parent.parent


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = load(ROOT / 'scripts/validate-physical-inspection.py', 'retirement_gate')
state = load(ROOT / 'scripts/fleet-cleaning-state.py', 'retirement_cleaning_state')
require = gate.require
LOCK = Path('/tmp/rhvp-fleet-inspection-runtime.lock')


def digest(path):
    state.safe_path(path)
    require(path.is_file(), 'Missing ordinary retirement evidence')
    return hashlib.sha256(path.read_bytes()).hexdigest()


def paths(base, run, attempt):
    require(bool(re.fullmatch(r'[0-9]{8}T[0-9]{6}Z', run)), 'Invalid inspection identity')
    bundle = base / 'ocp/install/fleet' / run
    reports = base / 'reports/installation' / run
    if attempt != run:
        require(bool(re.fullmatch(r'[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}', attempt)), 'Explicit installation attempt required')
        bundle = bundle / 'attempts' / attempt
        reports = reports / 'attempts' / attempt
    return state.safe_path(bundle), state.safe_path(reports)


def historical(base, run, attempt, lock):
    bundle, reports = paths(base, run, attempt)
    serving = state.read(bundle / 'plan.yml')['abi_install_plan']
    prepared = state.read(bundle / 'prepared.yaml')
    require(prepared['plan_sha256'] == state.fingerprint(serving), 'Historical serving plan changed')
    require(serving['schema_version'] == 1 and serving['operation'] == 'serve_existing_abi_iso'
            and serving['inspection_run_id'] == run and serving.get('installation_attempt_id', run) == attempt
            and serving['bundle'] == str(bundle) and serving['reports_dir'] == str(reports / 'serving'),
            'Historical installation identity differs')
    boot_file = reports / 'boot/runtime.yaml'
    boot = state.read(boot_file) if boot_file.exists() else None
    if lock.exists():
        owner = state.safe_path(Path((state.safe_path(lock / 'owner')).read_text().strip()))
    elif boot is not None:
        owner = state.safe_path(Path(boot['serving_attempt']))
    else:
        owners = list((reports / 'serving').glob('attempt-*'))
        require(len(owners) == 1, 'No unique historical serving owner')
        owner = state.safe_path(owners[0])
    require(owner.is_absolute() and owner.parent == reports / 'serving'
            and bool(re.fullmatch(r'attempt-[A-Za-z0-9_-]+', owner.name)), 'Serving owner outside selected attempt')
    require(state.read(owner / 'plan.yml') == {'abi_install_plan': serving}, 'Serving owner plan differs')
    receipt = state.read(owner / 'runtime.yaml')
    require(receipt['schema_version'] == 1 and receipt['attempt'] == str(owner)
            and receipt['inspection_run_id'] == run
            and receipt.get('installation_attempt_id', run) == attempt
            and receipt['node_names'] == serving['node_names'] and receipt['iso_sha256'] == serving['iso_sha256']
            and receipt.get('ownership_confirmed') is True, 'Serving receipt does not establish ownership')
    files = [bundle / 'plan.yml', bundle / 'prepared.yaml', owner / 'plan.yml', owner / 'runtime.yaml']
    original = {'serving_status': receipt['status']}
    if boot is not None:
        require(boot['schema_version'] == 1 and boot['inspection_run_id'] == run
                and boot.get('installation_attempt_id', run) == attempt and boot['serving_attempt'] == str(owner)
                and boot['node_names'] == serving['node_names'], 'Boot outcome differs from selected owner')
        files.append(boot_file)
        original.update({k: boot.get(k) for k in ('status', 'installation_complete', 'bootstrap_complete', 'fleet_ready')})
        if (reports / 'boot/plan.yml').exists():
            boot_plan = state.read(reports / 'boot/plan.yml')['abi_boot_plan']
            require(state.read(bundle / 'boot/prepared.yaml')['plan_sha256'] == state.fingerprint(boot_plan),
                    'Historical boot preparation changed')
            require(boot_plan['serving_attempt'] == str(owner) and boot_plan['serving_sha256'] == digest(owner / 'runtime.yaml'),
                    'Historical boot serving binding changed')
            require(state.read(bundle / 'boot/plan.yml') == {'abi_boot_plan': boot_plan}, 'Executed boot plan changed')
            files.extend([reports / 'boot/plan.yml', bundle / 'boot/plan.yml', bundle / 'boot/prepared.yaml'])
    cleaned = (boot or receipt).get('cleanup_status') in ('complete', 'not_needed') \
        and (boot or receipt).get('runtime_retained') is False
    require(lock.exists() or cleaned, 'Absent lock without verified historical cleanup')
    if lock.exists():
        require(not cleaned and receipt.get('lock_owned') is True and receipt.get('runtime_retained') is True,
                'Serving lock conflicts with historical cleanup')
    require('firewall_rules' in receipt, 'Missing recorded firewall ownership')
    mode = 'already_cleaned' if cleaned else 'retained'
    return bundle, reports, owner, serving, receipt, original, files, mode


def executor_hashes(collection):
    names = ['playbooks/retire_abi_runtime.yml', 'plugins/filter/abi_runtime_retirement.py']
    for role in ('abi_runtime_retirement', 'ironic_cleanup'):
        folder = collection / 'roles' / role
        require(folder.is_dir(), 'Retirement collection implementation missing')
        names.extend(str(p.relative_to(collection)) for p in sorted(folder.rglob('*.yml')))
    return {name: digest(collection / name) for name in names}


def prepare(root, arch, site, run, attempt, collection, reason, target_release, lock=LOCK):
    require(isinstance(reason, str) and 1 <= len(reason.strip()) <= 1024, 'Retirement reason required')
    require(isinstance(target_release, str) and bool(re.fullmatch(r'[^\s]+@sha256:[a-f0-9]{64}', target_release)),
            'Explicit immutable intended target release required')
    base = state.site_root(root, arch, site)
    bundle, reports, owner, serving, receipt, original, files, mode = historical(base, run, attempt, lock)
    target = state.safe_path(bundle / 'retirement')
    require(not target.exists(), 'Retirement review already exists; use the reviewed preparation')
    plugin = load(collection / 'plugins/filter/abi_runtime_retirement.py', 'retirement_contract')
    plugin.rules(receipt['firewall_rules'], serving['firewall_zone'])
    require(not (reports / 'boot/monitor.claim').exists(), 'Installer monitor claim requires review')
    plan = {'schema_version': 1, 'operation': 'retire_abi_runtime', 'mode': mode,
            'site_root': str(base), 'inspection_run_id': run, 'installation_attempt_id': attempt,
            'serving_attempt': str(owner), 'runtime_lock': str(lock), 'serving_plan': serving,
            'serving_receipt': receipt, 'original_status': original, 'node_names': serving['node_names'],
            'reason': reason.strip(), 'target_release': target_release,
            'artifact_root': str(reports / 'retirement'), 'boot_root': str(reports / 'boot'),
            'source_sha256': {str(p): digest(p) for p in files}, 'collection_root': str(collection),
            'collection_sha256': executor_hashes(collection),
            'adapter_sha256': {str(p): digest(p) for p in (Path(__file__), ROOT / 'playbooks/retire-ocp-runtime.yml')},
            'node_mutations': False}
    target.mkdir(mode=0o700)
    state.write(target / 'plan.yml', {'abi_runtime_retirement_plan': plan})
    state.write(target / 'prepared.yaml', {'plan_sha256': state.fingerprint(plan)})
    review = [f'ABI runtime retirement: {attempt}', f'Inspection source: {run}', f'Serving owner: {owner}',
              'Nodes: ' + ', '.join(plan['node_names']), 'Original outcome: ' + json.dumps(original),
              f'Mode: {mode}; reason: {reason.strip()}', f'Intended target release: {target_release}',
              'Scope: retire owned bastion runtime only. No node power, media, drain, disk or cluster mutations.',
              'Preserve original receipts, ISO, auth and installer state. No installation success is inferred.',
              'Archive complete private service logs; remove only recorded owned resources/firewall rules.',
              'Wipe, target-release media generation and installation require separate preparation/authorization.',
              f'After review: make retire-ocp-runtime FLEET_AUTHORIZE_RETIREMENT_RUN={attempt}']
    (target / 'review.txt').write_text('\n'.join(review) + '\n')
    (target / 'review.txt').chmod(0o600)
    return target


def verify(root, arch, site, run, attempt, collection):
    base = state.site_root(root, arch, site)
    bundle, reports = paths(base, run, attempt)
    plan = state.read(bundle / 'retirement/plan.yml')['abi_runtime_retirement_plan']
    require(state.read(bundle / 'retirement/prepared.yaml')['plan_sha256'] == state.fingerprint(plan),
            'Retirement plan changed')
    require(plan['inspection_run_id'] == run and plan['installation_attempt_id'] == attempt
            and plan['site_root'] == str(base) and plan['artifact_root'] == str(reports / 'retirement')
            and plan['collection_root'] == str(collection) and plan['node_mutations'] is False,
            'Retirement target changed')
    for group in ('source_sha256', 'adapter_sha256'):
        require(all(digest(Path(p)) == value for p, value in plan[group].items()), 'Retirement evidence/source changed')
    require(plan['collection_sha256'] == executor_hashes(collection), 'Retirement executor changed')
    return plan


def completed(base, run, attempt):
    """Consume only verified retirement proof; leave original outcomes immutable."""
    bundle, reports = paths(base, run, attempt)
    receipt_path = reports / 'retirement/receipt.yaml'
    if not receipt_path.exists():
        return None
    plan = state.read(bundle / 'retirement/plan.yml')['abi_runtime_retirement_plan']
    seal = state.read(bundle / 'retirement/prepared.yaml')['plan_sha256']
    receipt = state.read(receipt_path)
    require(seal == state.fingerprint(plan) == receipt.get('plan_sha256')
            and receipt.get('schema_version') == 1 and receipt.get('operation') == 'retire_abi_runtime'
            and receipt.get('status') == 'RETIRED' and receipt.get('cleanup_verified') is True
            and receipt.get('resources_absent') is True and receipt.get('lock_released') is True
            and receipt.get('archive_verified') is True and receipt.get('node_mutations') is False
            and receipt.get('installation_attempt_id') == attempt and receipt.get('inspection_run_id') == run
            and receipt.get('serving_attempt') == plan['serving_attempt']
            and receipt.get('source_sha256') == plan['source_sha256']
            and receipt.get('original_status') == plan['original_status']
            and receipt.get('node_names') == plan['node_names'], 'Incomplete or mismatched retirement receipt')
    require(plan['site_root'] == str(base) and plan['artifact_root'] == str(reports / 'retirement'),
            'Retirement receipt outside selected site')
    for path, expected in plan['source_sha256'].items():
        require(digest(Path(path)) == expected, 'Historical retirement evidence changed')
    manifest = state.read(reports / 'retirement/archive.yaml')
    require(manifest.get('source_sha256') == plan['source_sha256'] and manifest.get('plan_sha256') == seal,
            'Retirement archive binding changed')
    require(len(manifest['files']) == 2 and
            all(sum(name.startswith(container + '.') and name.endswith('.log') for name in manifest['files']) == 1
                for container in ('metal3-ironic-ironic', 'metal3-ironic-httpd')), 'Incomplete retirement service archive')
    for name, expected in manifest['files'].items():
        require('/' not in name and name not in ('.', '..'), 'Invalid archived log name')
        require(digest(reports / 'retirement/service-logs' / name) == expected, 'Retirement archive changed')
    return receipt


def execute(root, arch, site, run, attempt, collection, authorize, lease):
    require(authorize == attempt, 'Exact FLEET_AUTHORIZE_RETIREMENT_RUN required')
    plan = verify(root, arch, site, run, attempt, collection)
    artifact = state.safe_path(Path(plan['artifact_root']))
    artifact.mkdir(parents=True, exist_ok=True, mode=0o700)
    claim = artifact / 'execution.lock'
    fd = os.open(state.safe_path(claim), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        command = ['ansible-playbook', str(ROOT / 'playbooks/retire-ocp-runtime.yml'), '-i', 'localhost,',
                   '-e', '@' + str(paths(state.site_root(root, arch, site), run, attempt)[0] / 'retirement/plan.yml'),
                   '-e', 'abi_runtime_retirement_authorize_run=' + authorize]
        env = {**os.environ, 'RHV_ABI_RETIREMENT_PLAN_SHA256': state.fingerprint(plan)}
        result = subprocess.run(command, cwd=root, pass_fds=(lease.fd, fd), env=env, check=False)
        require(result.returncode == 0, 'Runtime retirement incomplete; review retained retirement reports')
        receipt = completed(state.site_root(root, arch, site), run, attempt)
        require(receipt is not None, 'Completed retirement receipt missing')
        require(not state.safe_path(Path(plan['runtime_lock'])).exists(), 'Runtime ownership remains')
        return receipt
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'execute'))
    parser.add_argument('--root', type=Path, default=ROOT)
    for name in ('arch', 'site', 'run-id', 'attempt'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--collection-root', type=Path, required=True)
    parser.add_argument('--reason', default=os.environ.get('ABI_RETIREMENT_REASON', ''))
    parser.add_argument('--target-release', default=os.environ.get('ABI_RETIREMENT_TARGET_RELEASE', ''))
    parser.add_argument('--authorize-run', default='')
    args = parser.parse_args()
    try:
        root, collection = args.root.absolute(), args.collection_root.absolute()
        with state.Lease(state.site_root(root, args.arch, args.site)) as lease:
            state.check(state.site_root(root, args.arch, args.site))
            if args.action == 'prepare':
                print('Retirement review: ' + str(prepare(root, args.arch, args.site, args.run_id, args.attempt,
                                                        collection, args.reason, args.target_release)))
                print('No cleanup authority; no services or nodes changed.')
            else:
                execute(root, args.arch, args.site, args.run_id, args.attempt, collection, args.authorize_run, lease)
                print('Runtime retired; installation outcome unchanged. Wipe requires separate authorization.')
    except (ValueError, KeyError, TypeError, OSError, yaml.YAMLError, subprocess.SubprocessError) as error:
        reason = str(error) if isinstance(error, ValueError) else type(error).__name__
        parser.exit(1, 'ERROR: ABI retirement rejected: ' + reason + '. Review retirement evidence.\n')


if __name__ == '__main__':
    main()
