# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""Regression checks for ping-matrix collection diagnostics."""

from pathlib import Path


PLAYBOOK = (
    Path(__file__).resolve().parent.parent / "playbooks" / "validate-ping-matrix.yml"
)


def test_collector_retries_incomplete_or_failed_reads():
    """A transient SSH failure or an unfinished result file must not be NODATA."""
    text = PLAYBOOK.read_text()

    assert "collect_retries: 18" in text
    assert "collect_delay_seconds: 5" in text
    assert 'STATUS="SSH_FAILED"' in text
    assert 'STATUS="EMPTY"' in text
    assert 'STATUS="INCOMPLETE"' in text
    assert "grep -q '^DONE$'" in text
    assert 'echo "COLLECT|$NAME|COMPLETE|attempt=$ATTEMPT"' in text


def test_report_distinguishes_collection_state_from_connectivity_failure():
    """The report must not present an incomplete collection as a clean ping run."""
    text = PLAYBOOK.read_text()

    assert "collect_status != 'COMPLETE'" in text
    assert "collection {{ collect_status | lower | replace('_', ' ') }}" in text
    assert "Connectivity validation incomplete:" in text
