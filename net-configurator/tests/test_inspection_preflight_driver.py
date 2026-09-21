# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""Static contract tests for the controlled inspection-preflight driver."""

import importlib.util
from pathlib import Path

import yaml


NET_CONFIGURATOR = Path(__file__).resolve().parent.parent


def _load_init_settings_module():
    spec = importlib.util.spec_from_file_location(
        "init_ocp_settings", NET_CONFIGURATOR / "scripts" / "init-ocp-settings.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generated_ocp_settings_do_not_require_manual_inspection_contract(tmp_path):
    output = tmp_path / "ocp-settings.yml"
    _load_init_settings_module().write_settings(
        output, "2-8-5-200", "example", "  control_plane:\n    - node-01"
    )

    settings = yaml.safe_load(output.read_text())

    assert "inspection" not in settings


def test_workbook_ocp_version_adapter_requires_exact_three_part_version(monkeypatch):
    module = _load_init_settings_module()
    monkeypatch.setattr(module, "read_ocp_settings", lambda _path: {"ocp_version": "4.22.8"})

    assert module.read_required_ocp_version(Path("ignored.xlsx")) == "4.22.8"

    monkeypatch.setattr(module, "read_ocp_settings", lambda _path: {"ocp_version": "4.22"})
    try:
        module.read_required_ocp_version(Path("ignored.xlsx"))
    except ValueError as exc:
        assert "exact three-part" in str(exc)
    else:
        raise AssertionError("an incomplete workbook version must be rejected")


def test_driver_uses_shared_vault_and_collection_role_without_bmc_credentials():
    playbook = yaml.safe_load(
        (NET_CONFIGURATOR / "playbooks" / "prepare-inspection-preflight.yml").read_text()
    )[0]
    task_names = [task["name"] for task in playbook["tasks"]]
    rendered = (NET_CONFIGURATOR / "playbooks" / "prepare-inspection-preflight.yml").read_text()

    assert "../.era-secrets/air-secrets.yml" in rendered
    assert "Require shared-vault OCP pull secret" in task_names
    assert "Materialize local pull secret for the collection role" in task_names
    assert "rhvp.baremetal_ocp.inspection_preflight_prepare" in rendered
    assert "ip\n          - -j\n          - route\n          - get" in rendered
    assert "inspection_preflight_prepare_expected_ocp_version" in rendered
    assert "inspection_preflight_expected_ocp_version" in rendered
    assert "inspection_preflight_collection_cluster_vars_path is match('^/')" in rendered
    assert "inspection_preflight_nmstate_dir is match('^/')" in rendered
    assert "inspection_preflight_early_network_path is match('^/')" in rendered
    assert "inspection_preflight_output_path is match('^/')" in rendered
    assert "inspection_preflight_ocp_settings_path" not in rendered
    assert "secrets.yaml" not in rendered
    assert "Redfish" not in rendered


def test_make_target_wires_operational_driver_and_collection_location():
    makefile = (NET_CONFIGURATOR / "Makefile").read_text()

    assert "prepare-inspection-preflight:" in makefile
    assert "RHV_BAREMETAL_OCP_COLLECTIONS_PATH" in makefile
    assert "playbooks/prepare-inspection-preflight.yml" in makefile
    assert "preflight-vars.yaml" in makefile
    assert "--print-ocp-version" in makefile
    assert "inspection_preflight_expected_ocp_version=$$OCP_VERSION" in makefile
    assert 'NMSTATE="$(CURDIR)/output/$(ARCH)/$(SITE)/ocp/inspection/nmstate/' in makefile
    assert 'EARLY_NETWORK="$(CURDIR)/output/$(ARCH)/$(SITE)/ocp/inspection/early-network/' in makefile
    assert "inspection_preflight_early_network_path=$$EARLY_NETWORK" in makefile
    assert "ocp-settings.yml\";" not in makefile
    assert "else VAULT_ARGS+=(--ask-vault-pass); fi;" in makefile


def test_gate6_driver_requires_matching_authorization_and_new_durable_report():
    driver_path = NET_CONFIGURATOR / "playbooks" / "inspect-controlled-candidate.yml"
    driver = yaml.safe_load(driver_path.read_text())
    rendered = driver_path.read_text()
    task_names = [task["name"] for task in driver[0]["tasks"]]

    assert "controlled_inspection_authorize_physical_boot == controlled_inspection_candidate" in rendered
    assert "controlled_inspection_report_path is match('^/')" in rendered
    assert "controlled_inspection_report_path is match('^/tmp(?:/|$)')" in rendered
    assert "preflight_inspection_nodes | default([]) | length == 1" in rendered
    assert "Refuse to overwrite an existing inspection report" in task_names
    assert driver[1]["import_playbook"] == "{{ controlled_inspection_collection_playbook }}"
    assert driver[1]["vars"]["inspect_cluster_nodes"] == "{{ preflight_inspection_nodes }}"


def test_make_target_wires_gate6_driver_and_explicit_operator_contract():
    makefile = (NET_CONFIGURATOR / "Makefile").read_text()

    assert "inspect-controlled-candidate:" in makefile
    assert "playbooks/inspect-controlled-candidate.yml" in makefile
    assert "INSPECTION_AUTHORIZE_PHYSICAL_BOOT" in makefile
    assert "INSPECTION_REPORT_PATH must be absolute" in makefile
    assert "INSPECTION_REPORT_PATH must not be under /tmp" in makefile
    assert "--ask-vault-pass" in makefile
