# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""Unit tests for --server-image parameter in topology_generator.py."""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))

spec = importlib.util.spec_from_file_location("topology_generator", scripts_dir / "topology_generator.py")
topo_gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(topo_gen)


def test_topology_generator_server_image_default():
    with patch("openpyxl.load_workbook") as mock_load:
        wb = MagicMock()
        wb.sheetnames = ["Wire Map"]
        mock_load.return_value = wb
        with patch.object(topo_gen, "parse_wiremap_excel", return_value=[]):
            tg = topo_gen.TopologyGenerator(Path("dummy.xlsx"), "2-8-5-200")
            assert tg.server_image == topo_gen.SERVER_OS
            assert tg._resolve_os("su-1-node-1") == topo_gen.SERVER_OS


def test_topology_generator_server_image_override():
    custom_image = "rhcos-422-openstack"
    with patch("openpyxl.load_workbook") as mock_load:
        wb = MagicMock()
        wb.sheetnames = ["Wire Map"]
        mock_load.return_value = wb
        with patch.object(topo_gen, "parse_wiremap_excel", return_value=[]):
            tg = topo_gen.TopologyGenerator(Path("dummy.xlsx"), "2-8-5-200", server_image=custom_image)
            assert tg.server_image == custom_image
            assert tg._resolve_os("su-1-node-1") == custom_image
