#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""OpenStack metadata spoof for RHCOS Ignition delivery in NVIDIA Air.

Runs on utility with 169.254.169.254/32 aliased to eth1 (same L2 as RHCOS
nodes on oob-mgmt-network 192.168.200.0/24). RHCOS openstack Ignition
provider fetches:
  GET /openstack/latest/meta_data.json  (probe)
  GET /openstack/latest/user_data       (Ignition JSON payload)

Dispatch map at /opt/era/ignition/ip-map.json:
  {"192.168.200.10": "/opt/era/ignition/ocp-worker-01.ign", ...}
"""

import http.server
import json
import logging
import sys
from pathlib import Path

DISPATCH_MAP = Path("/opt/era/ignition/ip-map.json")
LOG_FILE = "/var/log/era-metadata.log"
PORT = 80
BIND = "0.0.0.0"


class _MetadataHandler(http.server.BaseHTTPRequestHandler):
    _ip_map: dict | None = None

    @classmethod
    def _load_map(cls) -> dict:
        if cls._ip_map is None:
            try:
                cls._ip_map = json.loads(DISPATCH_MAP.read_text())
            except Exception as exc:
                logging.error("Cannot load dispatch map %s: %s", DISPATCH_MAP, exc)
                cls._ip_map = {}
        return cls._ip_map

    def _send(self, data: bytes, content_type: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        client_ip = self.client_address[0]
        ip_map = self._load_map()
        path = self.path.split("?")[0].rstrip("/") or "/"

        if path == "/openstack/latest/user_data":
            ign_path = ip_map.get(client_ip)
            if not ign_path:
                logging.warning("No Ignition mapping for client %s", client_ip)
                self.send_error(404)
                return
            try:
                data = Path(ign_path).read_bytes()
            except OSError as exc:
                logging.error("Cannot read %s: %s", ign_path, exc)
                self.send_error(500)
                return
            self._send(data, "application/json")
            logging.info("Served user_data → %s (%d B) to %s", ign_path, len(data), client_ip)

        elif path == "/openstack/latest/meta_data.json":
            ign_path = ip_map.get(client_ip, "")
            node_name = Path(ign_path).stem if ign_path else client_ip
            meta = json.dumps({"uuid": client_ip, "hostname": node_name}).encode()
            self._send(meta, "application/json")

        elif path in ("/", "/openstack", "/openstack/latest"):
            self._send(b"meta_data.json\nuser_data\n", "text/plain")

        else:
            self.send_error(404)

    def log_message(self, fmt, *args) -> None:
        logging.info(fmt, *args)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOG_FILE),
        ],
    )
    server = http.server.HTTPServer((BIND, PORT), _MetadataHandler)
    logging.info("ERA RHCOS metadata server listening on %s:%d", BIND, PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
