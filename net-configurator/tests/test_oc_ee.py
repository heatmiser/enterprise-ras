# SPDX-License-Identifier: MIT
"""Context-selected container access; no Podman or cluster operations in tests."""

import importlib.util
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("oc_ee", ROOT / "scripts/oc-ee.py")
access = importlib.util.module_from_spec(spec)
spec.loader.exec_module(access)
RUN = "20261103T235340Z"
IMAGE = "quay.io/example/installer@sha256:" + "a" * 64


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data))


def completed(root, attempt="", site="test01"):
    (root / ".era-context").write_text(f"arch: 2-8-5-200\nsite: {site}\nfleet_run_id: {RUN}\n")
    base = root / "output/2-8-5-200" / site
    bundle, reports = access.INSTALL.installation_paths(base, RUN, attempt)
    kube = reports / "boot/installer/auth/kubeconfig"
    kube.parent.mkdir(parents=True, exist_ok=True)
    config = {"apiVersion": "v1", "kind": "Config", "current-context": "admin",
              "contexts": [{"name": "admin", "context": {"cluster": "cluster", "user": "admin"}}],
              "clusters": [{"name": "cluster", "cluster": {
                  "server": "https://api.fixture.example:6443", "certificate-authority-data": "fixture-source-ca"}}],
              "users": [{"name": "admin", "user": {"token": "fixture-source-token"}}]}
    write(kube, config)
    source = base / "ocp-iso" / RUN / "auth/kubeconfig"
    write(source, config)
    serving = {"inspection_run_id": RUN, "reports_dir": str(reports / "serving"),
               "iso_path": str(source.parent.parent / "agent.x86_64.iso"),
               "installer_ee": IMAGE, "node_names": ["node01"], "iso_sha256": "b" * 64,
               "source_sha256": {"auth/kubeconfig": access.INSTALL.state.digest(kube)}}
    if attempt:
        serving["installation_attempt_id"] = attempt
    write(bundle / "plan.yml", {"abi_install_plan": serving})
    fingerprint = access.INSTALL.state.fingerprint
    write(bundle / "prepared.yaml", {"plan_sha256": fingerprint(serving)})
    if attempt:
        write(base / "ocp/install/fleet" / RUN / "current-attempt.yaml",
              {"schema_version": 1, "inspection_run_id": RUN,
               "installation_attempt_id": attempt, "plan_sha256": fingerprint(serving)})
    boot = {"inspection_run_id": RUN, "serving_plan": serving, "artifact_root": str(reports / "boot"),
            "installer_dir": str(source.parent.parent)}
    if attempt:
        boot["installation_attempt_id"] = attempt
    write(bundle / "boot/plan.yml", {"abi_boot_plan": boot})
    write(bundle / "boot/prepared.yaml", {"plan_sha256": fingerprint(boot)})
    outcome = {"status": "SUCCEEDED", "bootstrap_complete": True, "installation_complete": True,
               "fleet_ready": True, "cleanup_status": "complete", "runtime_retained": False,
               "inspection_run_id": RUN, "node_names": ["node01"], "iso_sha256": "b" * 64}
    if attempt:
        outcome["installation_attempt_id"] = attempt
    write(reports / "boot/runtime.yaml", outcome)
    return bundle, reports, kube


def test_original_then_ten_reinstalls_follow_selection(tmp_path, monkeypatch):
    monkeypatch.setenv("ABI_REPORT", "obsolete/report")
    monkeypatch.setenv("KUBECONFIG", "obsolete/kubeconfig")
    _, _, kube = completed(tmp_path)
    assert access.selected_access(tmp_path) == (IMAGE, kube)
    for index in range(10):
        _, _, kube = completed(tmp_path, f"20261110T233043Z-{index:08x}")
        assert access.selected_access(tmp_path) == (IMAGE, kube)


def test_context_switch_uses_other_site(tmp_path):
    completed(tmp_path)
    _, _, kube = completed(tmp_path, site="test02")
    assert access.selected_access(tmp_path) == (IMAGE, kube)


@pytest.mark.parametrize("change", [
    {"status": "FAILED"}, {"status": "BOOTED"}, {"fleet_ready": False},
    {"installation_complete": False}, {"cleanup_status": "pending"},
    {"runtime_retained": True}, {"inspection_run_id": "20261101T000000Z"},
    {"installation_attempt_id": "20261110T233043Z-ffffffff"},
    {"node_names": ["foreign"]}, {"iso_sha256": "c" * 64},
])
def test_unusable_repeat_never_falls_back(tmp_path, change):
    completed(tmp_path)
    _, reports, _ = completed(tmp_path, "20261110T233043Z-00000001")
    path = reports / "boot/runtime.yaml"
    outcome = yaml.safe_load(path.read_text())
    write(path, {**outcome, **change})
    with pytest.raises(ValueError):
        access.selected_access(tmp_path)


@pytest.mark.parametrize("fault", ["missing", "changed", "symlink"])
def test_kubeconfig_faults_rejected(tmp_path, fault):
    _, _, kube = completed(tmp_path)
    if fault == "missing":
        kube.unlink()
    elif fault == "changed":
        kube.write_text("foreign credential fixture")
    else:
        target = tmp_path / "foreign"
        target.write_bytes(kube.read_bytes())
        kube.unlink()
        kube.symlink_to(target)
    with pytest.raises(ValueError):
        access.selected_access(tmp_path)


@pytest.mark.parametrize("omitted", [("apiVersion",), ("kind",), ("apiVersion", "kind")])
def test_headerless_sealed_source_is_supported(tmp_path, omitted):
    bundle, _, kube = completed(tmp_path, "20261110T233043Z-00000001")
    source = tmp_path / "output/2-8-5-200/test01/ocp-iso" / RUN / "auth/kubeconfig"
    config = yaml.safe_load(source.read_text())
    for key in omitted:
        del config[key]
    write(source, config)
    serving = yaml.safe_load((bundle / "plan.yml").read_text())["abi_install_plan"]
    serving["source_sha256"]["auth/kubeconfig"] = access.INSTALL.state.digest(source)
    fingerprint = access.INSTALL.state.fingerprint
    write(bundle / "plan.yml", {"abi_install_plan": serving})
    write(bundle / "prepared.yaml", {"plan_sha256": fingerprint(serving)})
    current = bundle.parent.parent / "current-attempt.yaml"
    marker = yaml.safe_load(current.read_text())
    marker["plan_sha256"] = fingerprint(serving)
    write(current, marker)
    boot = yaml.safe_load((bundle / "boot/plan.yml").read_text())["abi_boot_plan"]
    boot["serving_plan"] = serving
    write(bundle / "boot/plan.yml", {"abi_boot_plan": boot})
    write(bundle / "boot/prepared.yaml", {"plan_sha256": fingerprint(boot)})
    assert access.selected_access(tmp_path) == (IMAGE, kube)


@pytest.mark.parametrize("change", [{"apiVersion": "invalid"}, {"kind": "Invalid"}, {"kind": None}])
def test_explicit_invalid_source_headers_are_rejected(tmp_path, change):
    _, _, kube = completed(tmp_path)
    config = yaml.safe_load(kube.read_text())
    config.update(change)
    write(kube, config)
    with pytest.raises(ValueError, match="Invalid kubeconfig structure"):
        access.kube_identity(kube, sealed_source=True)


@pytest.mark.parametrize("header", ["apiVersion", "kind"])
def test_completed_working_config_still_requires_headers(tmp_path, header):
    _, _, kube = completed(tmp_path)
    config = yaml.safe_load(kube.read_text())
    del config[header]
    write(kube, config)
    with pytest.raises(ValueError, match="Invalid kubeconfig structure"):
        access.selected_access(tmp_path)


def test_completed_working_credentials_can_differ_from_sealed_source(tmp_path):
    _, _, kube = completed(tmp_path, "20261110T233043Z-00000001")
    config = yaml.safe_load(kube.read_text())
    config["clusters"][0]["cluster"]["certificate-authority-data"] = "fixture-final-ca"
    config["users"][0]["user"] = {"client-certificate-data": "fixture-final-cert",
                                  "client-key-data": "fixture-final-key"}
    write(kube, config)
    assert access.selected_access(tmp_path) == (IMAGE, kube)


@pytest.mark.parametrize("change", [
    {"server": "https://foreign.example:6443"}, {"server": "http://api.fixture.example:6443"},
    {"insecure-skip-tls-verify": True}, {"certificate-authority-data": ""},
    {"certificate-authority": "/outside/ca"}, {"proxy-url": "http://foreign.example"},
])
def test_working_endpoint_and_tls_policy_rejected(tmp_path, change):
    _, _, kube = completed(tmp_path)
    config = yaml.safe_load(kube.read_text())
    config["clusters"][0]["cluster"].update(change)
    write(kube, config)
    with pytest.raises(ValueError):
        access.selected_access(tmp_path)


@pytest.mark.parametrize("fault", ["context", "credentials", "external_auth", "sealed_source"])
def test_working_structure_and_source_binding_rejected(tmp_path, fault):
    _, _, kube = completed(tmp_path)
    config = yaml.safe_load(kube.read_text())
    if fault == "context":
        config["current-context"] = "missing"
    elif fault == "credentials":
        config["users"][0]["user"] = {}
    elif fault == "external_auth":
        config["users"][0]["user"]["exec"] = {"command": "external"}
    else:
        source = tmp_path / "output/2-8-5-200/test01/ocp-iso" / RUN / "auth/kubeconfig"
        source.write_text("changed source fixture")
    write(kube, config)
    with pytest.raises(ValueError):
        access.selected_access(tmp_path)


def test_selection_and_sealed_plan_drift_rejected(tmp_path):
    bundle, _, _ = completed(tmp_path, "20261110T233043Z-00000001")
    path = bundle / "plan.yml"
    data = yaml.safe_load(path.read_text())
    data["abi_install_plan"]["installer_ee"] = "example:latest"
    write(path, data)
    with pytest.raises(ValueError, match="plan changed"):
        access.selected_access(tmp_path)


def test_new_attempt_without_completion_does_not_use_old_credentials(tmp_path):
    completed(tmp_path)
    _, reports, _ = completed(tmp_path, "20261110T233043Z-00000001")
    (reports / "boot/runtime.yaml").unlink()
    with pytest.raises(ValueError):
        access.selected_access(tmp_path)


def test_main_passes_arguments_stdin_exit_code_and_readonly_mount(tmp_path, monkeypatch):
    _, _, kube = completed(tmp_path)
    monkeypatch.setattr(access, "ROOT", tmp_path)
    monkeypatch.setattr(access.sys, "argv", ["oc-ee.py", "apply", "-f", "-", "--field-manager=operator name"])
    monkeypatch.setattr(access.sys.stdin, "isatty", lambda: False)
    observed = []
    monkeypatch.setattr(access.subprocess, "run", lambda argv, **kw: (
        observed.append((argv, kw)) or SimpleNamespace(returncode=17)))
    assert access.main() == 17
    argv, options = observed[0]
    assert argv[:4] == ["podman", "run", "--rm", "-i"]
    assert "-t" not in argv
    assert f"{kube}:/work/kubeconfig:ro,z" in argv
    assert argv[-5:] == ["--kubeconfig=/work/kubeconfig", "apply", "-f", "-", "--field-manager=operator name"]
    assert IMAGE in argv and "--network=host" in argv and options == {"check": False}


def test_interactive_terminal_flags(tmp_path, monkeypatch):
    monkeypatch.setattr(access.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(access.sys.stdout, "isatty", lambda: True)
    assert access.command(IMAGE, tmp_path / "kubeconfig", ["get", "nodes"])[:5] == [
        "podman", "run", "--rm", "-i", "-t"]


def test_failure_message_does_not_expose_parsed_data_or_dispatch(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(access, "ROOT", tmp_path)
    monkeypatch.setattr(access.subprocess, "run", lambda *a, **kw: pytest.fail("must not dispatch"))
    assert access.main() == 1
    assert "No older attempt" in capsys.readouterr().err


def test_sourced_function_works_from_other_directory_with_quoted_arguments(tmp_path):
    root = tmp_path / "project with spaces"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts/oc-ee.sh").write_bytes((ROOT / "scripts/oc-ee.sh").read_bytes())
    (root / "scripts/oc-ee.py").write_text("import json,sys\nprint(json.dumps(sys.argv[1:]))\n")
    result = subprocess.run(["bash", "-c", 'source "$1"; cd /; oc_ee get pods "a b"',
                             "bash", str(root / "scripts/oc-ee.sh")], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == ["get", "pods", "a b"]
