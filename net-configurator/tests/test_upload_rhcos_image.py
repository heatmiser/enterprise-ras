# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""Unit tests for scripts/upload_rhcos_image.py."""

import importlib.util
import io
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))

spec = importlib.util.spec_from_file_location("upload_rhcos_image", scripts_dir / "upload_rhcos_image.py")
uri = importlib.util.module_from_spec(spec)
spec.loader.exec_module(uri)


def test_find_rhcos_openstack_info_sha256sum():
    mock_sha_content = (
        "1111111111111111111111111111111111111111111111111111111111111111  rhcos-4.22.0-x86_64-live.x86_64.iso\n"
        "2222222222222222222222222222222222222222222222222222222222222222  rhcos-4.22.0-x86_64-openstack.x86_64.qcow2.gz\n"
    ).encode("utf-8")

    mock_resp = MagicMock()
    mock_resp.read.return_value = mock_sha_content
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        filename, sha256 = uri.find_rhcos_openstack_info("4.22")
        assert filename == "rhcos-4.22.0-x86_64-openstack.x86_64.qcow2.gz"
        assert sha256 == "2222222222222222222222222222222222222222222222222222222222222222"


def test_find_rhcos_openstack_info_fallback_on_error():
    with patch("urllib.request.urlopen", side_effect=Exception("HTTP 404")):
        filename, sha256 = uri.find_rhcos_openstack_info("4.22")
        assert filename == "rhcos-openstack.x86_64.qcow2.gz"
        assert sha256 is None


def test_verify_sha256(tmp_path):
    dummy_file = tmp_path / "test.bin"
    dummy_file.write_bytes(b"hello world\n")
    # sha256 of "hello world\n" is d2a842...
    import hashlib
    expected = hashlib.sha256(b"hello world\n").hexdigest()

    assert uri.verify_sha256(dummy_file, expected) is True
    assert uri.verify_sha256(dummy_file, "0000000000000000000000000000000000000000000000000000000000000000") is False


def test_image_name_default_derivation():
    # Test 2-part version ("4.22") -> "rhcos-422-openstack"
    parts_422 = "4.22".split(".")
    ver_422 = "".join(parts_422[:2])
    assert f"rhcos-{ver_422}-openstack" == "rhcos-422-openstack"

    # Test 3-part version ("4.22.0") -> "rhcos-422-openstack"
    parts_4220 = "4.22.0".split(".")
    ver_4220 = "".join(parts_4220[:2])
    assert f"rhcos-{ver_4220}-openstack" == "rhcos-422-openstack"

