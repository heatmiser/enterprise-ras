# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for scripts/_era_context.py.

This script parses ``.era-context`` and emits shell-safe
``_ARCH=<value>`` / ``_SITE=<value>`` assignments that the Makefile
``eval`` to avoid the old injection hazard of interpolating raw
``grep | cut`` output into an inline Python one-liner. The invariants
locked in here:

- Valid context → predictable, shell-safe output, exit 0.
- Missing context → exit 1 with a helpful diagnostic.
- Invalid arch (not in the known set) → exit 1.
- Path-traversal attempts in site (``../evil``) → exit 1.
- Shell-metacharacter injection in arch/site → regex refuses to match,
  validator treats the line as absent.
- Site defaults to ``default`` when omitted.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from openpyxl import Workbook


SCRIPT = Path(__file__).parent.parent / "scripts" / "_era_context.py"


def _run(cwd: Path) -> subprocess.CompletedProcess:
    """Run the validator with ``cwd`` as the process working directory."""
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )


@pytest.fixture
def ctx_dir(tmp_path):
    """Isolated working directory for a .era-context file."""
    return tmp_path


def _write_ctx(ctx_dir: Path, body: str) -> None:
    (ctx_dir / ".era-context").write_text(body)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_valid_context_emits_shell_safe_assignments(ctx_dir):
    _write_ctx(ctx_dir, "arch: 2-8-5-200\nsite: customer-a\n")
    result = _run(ctx_dir)
    assert result.returncode == 0, result.stderr
    assert "_ARCH=2-8-5-200" in result.stdout
    assert "_SITE=customer-a" in result.stdout


def test_site_defaults_to_default_when_absent(ctx_dir):
    _write_ctx(ctx_dir, "arch: 2-4-3-200\n")
    result = _run(ctx_dir)
    assert result.returncode == 0
    assert "_SITE=default" in result.stdout


def test_comments_and_blank_lines_ignored(ctx_dir):
    _write_ctx(
        ctx_dir,
        "# ERA Deployment Context\n"
        "\n"
        "arch: 2-8-9-400\n"
        "# trailing comment\n"
        "site: lab\n",
    )
    result = _run(ctx_dir)
    assert result.returncode == 0
    assert "_ARCH=2-8-9-400" in result.stdout
    assert "_SITE=lab" in result.stdout


def test_all_three_valid_arches_accepted(ctx_dir):
    for arch in ("2-4-3-200", "2-8-5-200", "2-8-9-400"):
        _write_ctx(ctx_dir, f"arch: {arch}\nsite: default\n")
        result = _run(ctx_dir)
        assert result.returncode == 0, f"{arch}: {result.stderr}"
        assert f"_ARCH={arch}" in result.stdout


# ---------------------------------------------------------------------------
# Missing / malformed
# ---------------------------------------------------------------------------

def test_missing_context_file_fails_with_hint(ctx_dir):
    # Intentionally no .era-context in ctx_dir.
    result = _run(ctx_dir)
    assert result.returncode == 1
    assert "no .era-context" in result.stderr
    assert "make use" in result.stderr


def test_missing_arch_line_fails(ctx_dir):
    _write_ctx(ctx_dir, "site: default\n")
    result = _run(ctx_dir)
    assert result.returncode == 1
    assert "missing an 'arch:' line" in result.stderr


def test_empty_file_fails(ctx_dir):
    _write_ctx(ctx_dir, "")
    result = _run(ctx_dir)
    assert result.returncode == 1


# ---------------------------------------------------------------------------
# Input validation — security-critical cases
# ---------------------------------------------------------------------------

def test_unknown_arch_rejected(ctx_dir):
    _write_ctx(ctx_dir, "arch: 1-2-3-456\nsite: default\n")
    result = _run(ctx_dir)
    assert result.returncode == 1
    assert "invalid arch" in result.stderr
    # Error should mention the allowlist so the user can self-correct.
    assert "2-8-5-200" in result.stderr


def test_path_traversal_in_arch_rejected(ctx_dir):
    _write_ctx(ctx_dir, "arch: ../../../etc/passwd\nsite: default\n")
    result = _run(ctx_dir)
    assert result.returncode == 1
    # Either the regex doesn't match (missing arch) or the allowlist
    # rejects it — both are acceptable, neither leaks the value into
    # anything executable.
    assert "arch" in result.stderr.lower()


def test_path_traversal_in_site_rejected(ctx_dir):
    _write_ctx(ctx_dir, "arch: 2-8-5-200\nsite: ../evil\n")
    result = _run(ctx_dir)
    assert result.returncode == 1
    assert "invalid site" in result.stderr


def test_slash_in_site_rejected(ctx_dir):
    _write_ctx(ctx_dir, "arch: 2-8-5-200\nsite: a/b\n")
    result = _run(ctx_dir)
    assert result.returncode == 1


def test_dots_only_site_rejected(ctx_dir):
    _write_ctx(ctx_dir, "arch: 2-8-5-200\nsite: ..\n")
    result = _run(ctx_dir)
    assert result.returncode == 1


def test_quote_escape_injection_in_arch_rejected(ctx_dir):
    # Classic shell-quote breakout attempt. The `\S+` capture stops at
    # the first space; the remainder fails `\s*$`, so the arch line
    # never matches, and we fall through to "missing arch:".
    _write_ctx(
        ctx_dir,
        "arch: 2-8-5-200'; touch /tmp/should-not-exist; echo '\nsite: default\n",
    )
    result = _run(ctx_dir)
    assert result.returncode == 1
    # Whatever the reason, it must not exit 0 (success).
    assert "_ARCH=" not in result.stdout


def test_command_substitution_in_site_rejected(ctx_dir):
    # Same shape as above — spaces after `$(` break the strict regex
    # and site silently falls back to 'default'. Either is safe; what
    # matters is that `$(rm ...)` is never emitted unquoted.
    _write_ctx(
        ctx_dir,
        "arch: 2-8-5-200\nsite: $(rm -rf /tmp/should-not-exist)\n",
    )
    result = _run(ctx_dir)
    # The line as written has spaces, so regex fails and site defaults.
    # Defaults are safe — and critically, the raw command-substitution
    # string never appears in stdout.
    assert "$(rm" not in result.stdout
    assert "rm -rf" not in result.stdout


def test_newline_in_site_cannot_inject_extra_assignment(ctx_dir):
    # If regex were lax, a multi-line site value could emit an extra
    # `_SITE=...` line. Strict `\S+` + `\s*$` anchors prevent that.
    _write_ctx(ctx_dir, "arch: 2-8-5-200\nsite: default\n_EXTRA=pwned\n")
    result = _run(ctx_dir)
    assert result.returncode == 0
    assert "_EXTRA" not in result.stdout


# ---------------------------------------------------------------------------
# Output shape
# ---------------------------------------------------------------------------

def test_output_is_evaluable_by_shell(ctx_dir):
    """End-to-end: the Makefile consumes this via `eval` — simulate."""
    _write_ctx(ctx_dir, "arch: 2-8-5-200\nsite: customer-a\n")
    result = _run(ctx_dir)
    assert result.returncode == 0

    # Parse as sh would.
    shell_check = subprocess.run(
        ["bash", "-c", f'eval "{result.stdout}"; echo "$_ARCH|$_SITE"'],
        capture_output=True,
        text=True,
    )
    assert shell_check.returncode == 0
    assert shell_check.stdout.strip() == "2-8-5-200|customer-a"


@pytest.fixture
def make_import_workspace(tmp_path):
    """Exercise real Make/import/context code with controlled validator outcomes."""
    root = SCRIPT.resolve().parent.parent
    shutil.copyfile(root / 'Makefile', tmp_path / 'Makefile')
    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    for name in ['_era_context.py', 'import-excel.py', 'excel_parser.py',
                 'utils.py', 'oob_reserved.py']:
        shutil.copyfile(root / 'scripts' / name, scripts / name)
    (scripts / 'validate_excel.py').write_text(
        'import os,sys\nfrom pathlib import Path\n'
        'Path("validated-path.txt").write_text(sys.argv[1])\n'
        'sys.exit(int(os.environ.get("VALIDATOR_EXIT", "0")))\n'
    )
    return tmp_path


def _workbook(path, site='customer-a'):
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = 'Settings'
    sheet.append(['architecture', '2-8-5-200'])
    sheet.append(['site_name', site])
    workbook.save(path)
    workbook.close()
    return path


def _make_import(root, *arguments, **overrides):
    env = {key: value for key, value in os.environ.items()
           if key not in {'ARCH', 'SITE', 'SERVER_OS', 'EXCEL', 'MAKEFLAGS',
                          'MFLAGS', 'MAKEOVERRIDES', 'IMPORT_SKIP_VALIDATE',
                          'IMPORT_SITE', 'VALIDATOR_EXIT'}}
    env.update(overrides)
    return subprocess.run(['make', '--no-print-directory', *arguments],
                          cwd=root, env=env, capture_output=True, text=True, timeout=15)


def test_make_import_uses_context_and_preserves_rhcos(make_import_workspace):
    root = make_import_workspace
    _write_ctx(root, 'arch: 2-8-5-200\nsite: customer-a\nserver_os: rhcos\n')
    workbook = _workbook(root / 'input/2-8-5-200/customer-a/2-8-5-200.xlsx')
    before = workbook.read_bytes()

    result = _make_import(root, 'import')

    assert result.returncode == 0, result.stdout + result.stderr
    assert (root / 'validated-path.txt').read_text() == 'input/2-8-5-200/customer-a/2-8-5-200.xlsx'
    assert 'Already at:' in result.stdout
    assert 'server_os: rhcos\n' in (root / '.era-context').read_text()
    assert workbook.read_bytes() == before


@pytest.mark.parametrize('origin', ['command-line', 'environment'])
def test_make_explicit_workbook_overrides_context(make_import_workspace, origin):
    root = make_import_workspace
    _write_ctx(root, 'arch: 2-8-5-200\nsite: customer-a\nserver_os: rhcos\n')
    workbook = _workbook(root / 'downloads/selected.xlsx', site='customer-b')
    args = ['import']
    env = {}
    if origin == 'command-line':
        args.append(f'EXCEL={workbook}')
    else:
        env['EXCEL'] = str(workbook)

    result = _make_import(root, *args, **env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (root / 'validated-path.txt').read_text() == str(workbook)
    assert (root / 'input/2-8-5-200/customer-b/2-8-5-200.xlsx').read_bytes() == workbook.read_bytes()
    assert (root / '.era-context').read_text().endswith('arch: 2-8-5-200\nsite: customer-b\n')


def test_make_explicit_workbook_without_context(make_import_workspace):
    root = make_import_workspace
    workbook = _workbook(root / 'downloads/selected.xlsx')

    result = _make_import(root, 'import', f'EXCEL={workbook}')

    assert result.returncode == 0, result.stdout + result.stderr
    assert (root / 'input/2-8-5-200/customer-a/2-8-5-200.xlsx').exists()


@pytest.mark.parametrize('context', [None, 'arch: invalid\nsite: customer-a\n'])
def test_make_import_requires_usable_context_or_source(make_import_workspace, context):
    root = make_import_workspace
    if context is not None:
        _write_ctx(root, context)

    result = _make_import(root, 'import')

    assert result.returncode != 0
    assert 'no workbook selected' in result.stdout
    assert not (root / 'validated-path.txt').exists()
    if context is not None:
        assert (root / '.era-context').read_text() == context
    else:
        assert not (root / '.era-context').exists()


def test_make_import_missing_canonical_workbook_preserves_context(make_import_workspace):
    root = make_import_workspace
    context = 'arch: 2-8-5-200\nsite: customer-a\nserver_os: rhcos\n'
    _write_ctx(root, context)

    result = _make_import(root, 'import')

    assert result.returncode != 0
    assert 'File not found' in result.stdout
    assert (root / '.era-context').read_text() == context


def test_make_import_validation_failure_preserves_inputs(make_import_workspace):
    root = make_import_workspace
    context = 'arch: 2-8-5-200\nsite: customer-a\nserver_os: rhcos\n'
    _write_ctx(root, context)
    workbook = _workbook(root / 'input/2-8-5-200/customer-a/2-8-5-200.xlsx')
    before = workbook.read_bytes()

    result = _make_import(root, 'import', VALIDATOR_EXIT='1')

    assert result.returncode != 0
    assert 'Excel validation failed' in result.stdout
    assert (root / '.era-context').read_text() == context
    assert workbook.read_bytes() == before


def test_context_import_default_does_not_change_deploy_contract(make_import_workspace):
    root = make_import_workspace
    _write_ctx(root, 'arch: 2-8-5-200\nsite: customer-a\n')
    _workbook(root / 'input/2-8-5-200/customer-a/2-8-5-200.xlsx')

    result = _make_import(root, 'deploy')

    assert result.returncode != 0
    assert 'EXCEL not specified' in result.stdout
    assert not (root / 'validated-path.txt').exists()


def test_valid_inspection_selection_is_emitted_with_os(ctx_dir):
    _write_ctx(ctx_dir, 'arch: 2-8-5-200\nsite: customer-a\nserver_os: rhcos\n'
                       'fleet_run_id: 20000101T120000Z\n')
    result = _run(ctx_dir)
    assert result.returncode == 0, result.stderr
    assert '_SERVER_OS=rhcos' in result.stdout
    assert '_FLEET_RUN_ID=20000101T120000Z' in result.stdout


@pytest.mark.parametrize('selection', ['20000101T130000Z', '', '../run', '$(touch marker)',
                                      '20000230T120000Z',
                                      '20000101T130000Z\nfleet_wipe_run_id: 20000101T140000Z'])
def test_wipe_context_selection_is_validated(ctx_dir, selection):
    _write_ctx(ctx_dir, 'arch: 2-8-5-200\nsite: customer-a\n'
                       f'fleet_wipe_run_id: {selection}\n')
    result = _run(ctx_dir)
    if selection == '20000101T130000Z':
        assert result.returncode == 0
        assert '_FLEET_WIPE_RUN_ID=20000101T130000Z' in result.stdout
    else:
        assert result.returncode != 0 and not result.stdout
    assert not (ctx_dir / 'marker').exists()


@pytest.mark.parametrize('arguments,expected', [([], '20000101T130000Z'),
                                              (['SITE=customer-b'], ''),
                                              (['ARCH=2-4-3-200'], ''),
                                              (['FLEET_WIPE_RUN_ID=20000101T140000Z'], '20000101T140000Z')])
def test_make_binds_selected_wipe_to_its_deployment(make_import_workspace, arguments, expected):
    root = make_import_workspace
    _write_ctx(root, 'arch: 2-8-5-200\nsite: customer-a\nfleet_wipe_run_id: 20000101T130000Z\n')
    with (root / 'Makefile').open('a') as stream:
        stream.write("\nprint-selected-wipe:\n\t@printf '%s\\n' '$(FLEET_WIPE_RUN_ID)'\n")
    result = _make_import(root, 'print-selected-wipe', *arguments)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == expected


@pytest.mark.parametrize('selection', [
    '', '../another-run', '20000101T120000Z;id', '$(touch marker)',
    '20000230T120000Z', '20000101T250000Z',
    '20000101T120000Z\nfleet_run_id: 20000101T130000Z',
])
def test_invalid_inspection_selection_emits_no_context(ctx_dir, selection):
    _write_ctx(ctx_dir, f'arch: 2-8-5-200\nsite: customer-a\nfleet_run_id: {selection}\n')
    result = _run(ctx_dir)
    assert result.returncode != 0
    assert not result.stdout
    assert not (ctx_dir / 'marker').exists()


@pytest.mark.parametrize('arguments,expected', [
    ([], '20000101T120000Z'),
    (['ARCH=2-8-5-200', 'SITE=customer-a'], '20000101T120000Z'),
    (['SITE=customer-b'], ''),
    (['ARCH=2-4-3-200'], ''),
    (['FLEET_RUN_ID=20000101T130000Z'], '20000101T130000Z'),
])
def test_make_binds_selected_run_to_its_deployment(make_import_workspace, arguments, expected):
    root = make_import_workspace
    _write_ctx(root, 'arch: 2-8-5-200\nsite: customer-a\nfleet_run_id: 20000101T120000Z\n')
    with (root / 'Makefile').open('a') as stream:
        stream.write("\nprint-selected-run:\n\t@printf '%s\\n' '$(FLEET_RUN_ID)'\n")
    result = _make_import(root, 'print-selected-run', *arguments)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == expected
