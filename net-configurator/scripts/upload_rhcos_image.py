#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""
Download RHCOS OpenStack qcow2 image from mirror.openshift.com and upload to NVIDIA Air.

Downloads the official RHCOS OpenStack qcow2 image for a target OCP version
(e.g., 4.22), decompresses it locally, and uploads it to the NVIDIA Air org image
catalog with `default_username="core"`.

Usage:
    python3 scripts/upload_rhcos_image.py --ocp-version 4.22
    python3 scripts/upload_rhcos_image.py --ocp-version 4.22 --skip-upload
"""

import argparse
import gzip
import hashlib
import os
import sys
import urllib.request
from pathlib import Path

MIRROR_BASE_URL = "https://mirror.openshift.com/pub/openshift-v4/dependencies/rhcos"


def find_rhcos_openstack_info(ocp_version: str) -> tuple[str, str | None]:
    """Find the exact RHCOS OpenStack qcow2.gz filename and sha256 for a given OCP version.

    Returns:
        tuple[filename, expected_sha256]
    """
    sha_url = f"{MIRROR_BASE_URL}/{ocp_version}/latest/sha256sum.txt"
    try:
        req = urllib.request.Request(sha_url, headers={"User-Agent": "net-configurator"})
        with urllib.request.urlopen(req) as resp:
            content = resp.read().decode("utf-8")
            for line in content.splitlines():
                parts = line.strip().split()
                if len(parts) >= 2:
                    sha256 = parts[0]
                    filename = parts[1].lstrip("*")
                    if "openstack" in filename and filename.endswith(".qcow2.gz"):
                        return filename, sha256
    except Exception as err:
        print(f"  NOTE: Could not fetch sha256sum.txt ({err}); falling back to default filename", file=sys.stderr)

    return "rhcos-openstack.x86_64.qcow2.gz", None


def verify_sha256(filepath: Path, expected_sha256: str) -> bool:
    """Verify SHA256 checksum of a file."""
    print(f"Verifying SHA256 checksum for {filepath.name} ...")
    hasher = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(1024 * 1024):
            hasher.update(chunk)
    actual_sha256 = hasher.hexdigest()
    if actual_sha256.lower() != expected_sha256.lower():
        print(
            f"❌ SHA256 checksum mismatch for {filepath.name}!\n"
            f"   Expected: {expected_sha256}\n"
            f"   Actual:   {actual_sha256}",
            file=sys.stderr,
        )
        return False
    print(f"✓ SHA256 checksum verified ({actual_sha256[:12]}...)")
    return True


def download_and_decompress_rhcos(ocp_version: str, cache_dir: Path, force: bool = False) -> Path:
    """Download, verify SHA256, and decompress RHCOS OpenStack qcow2 image locally."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    uncompressed_path = cache_dir / f"rhcos-{ocp_version}-openstack.x86_64.qcow2"

    if uncompressed_path.exists() and uncompressed_path.stat().st_size > 0 and not force:
        print(f"Reusing cached RHCOS image: {uncompressed_path}")
        return uncompressed_path

    gz_filename, expected_sha256 = find_rhcos_openstack_info(ocp_version)
    download_url = f"{MIRROR_BASE_URL}/{ocp_version}/latest/{gz_filename}"
    gz_path = cache_dir / gz_filename

    print(f"Downloading RHCOS OpenStack image from {download_url} ...")
    req = urllib.request.Request(download_url, headers={"User-Agent": "net-configurator"})
    with urllib.request.urlopen(req) as resp, open(gz_path, "wb") as out_file:
        total = int(resp.headers.get("Content-Length", 0))
        downloaded = 0
        chunk_size = 1024 * 1024  # 1MB
        while True:
            chunk = resp.read(chunk_size)
            if not chunk:
                break
            out_file.write(chunk)
            downloaded += len(chunk)
            if total:
                pct = (downloaded / total) * 100
                print(f"\r  Downloaded {downloaded / (1024**2):.1f}/{total / (1024**2):.1f} MB ({pct:.1f}%)", end="", flush=True)
        print()

    if expected_sha256:
        if not verify_sha256(gz_path, expected_sha256):
            gz_path.unlink(missing_ok=True)
            sys.exit("ERROR: Downloaded file corrupt (SHA256 mismatch).")

    print(f"Decompressing {gz_path} -> {uncompressed_path} ...")
    with gzip.open(gz_path, "rb") as gz_in, open(uncompressed_path, "wb") as qcow_out:
        chunk_size = 1024 * 1024  # 1MB
        while True:
            chunk = gz_in.read(chunk_size)
            if not chunk:
                break
            qcow_out.write(chunk)

    if gz_path.exists():
        gz_path.unlink()  # Remove temporary .gz to save disk space

    print(f"✓ RHCOS qcow2 ready at {uncompressed_path}")
    return uncompressed_path


def upload_to_air(local_qcow2: Path, image_name: str) -> None:
    """Upload qcow2 image to NVIDIA Air catalog if not already present."""
    try:
        from air_sdk import AirApi
        from air_sdk.utils import wait_for_state
    except ImportError:
        sys.exit(
            "ERROR: 'air_sdk' module not found.\n"
            "  Install nv-air-sdk package or run in a virtualenv with nv-air-sdk installed."
        )

    # Resolve API Key
    api_key = os.environ.get("AIR_API_KEY")
    if not api_key:
        key_file = os.environ.get("AIR_API_KEY_FILE", os.path.expanduser("~/.era-secrets/air-api-key"))
        if os.path.exists(key_file):
            api_key = Path(key_file).read_text().strip()

    if not api_key:
        sys.exit("ERROR: AIR_API_KEY env var or ~/.era-secrets/air-api-key required for Air upload.")

    api = AirApi.with_api_key(api_key=api_key)

    existing = next((img for img in api.images.list(search=image_name) if img.name == image_name), None)
    if existing:
        print(f"Air image {image_name!r} already exists (id={existing.id}, upload_status={existing.upload_status!r}). Skipping upload.")
        return

    size_gb = local_qcow2.stat().st_size / (1024**3)
    print(f"Uploading {local_qcow2} ({size_gb:.2f} GB) to Air as image {image_name!r} ...")

    image = api.images.create(
        name=image_name,
        version="1.0.0",
        default_username="core",
        default_password="not-used-rhcos",
        cpu_arch="x86",
        provider="VM",
        filepath=str(local_qcow2),
        max_workers=4,
    )
    wait_for_state(image, "COMPLETE", state_field="upload_status", error_states="READY")
    print(f"✓ Upload complete: image id={image.id}, name={image.name!r}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ocp-version", default="4.22", help="OCP version (default: 4.22)")
    parser.add_argument("--image-name", default=None, help="Air image name (default: rhcos-<ver>-openstack)")
    parser.add_argument("--cache-dir", default=".cache", help="Cache directory (default: .cache)")
    parser.add_argument("--force-download", action="store_true", help="Force re-downloading local image")
    parser.add_argument("--skip-upload", action="store_true", help="Download and decompress locally only")
    args = parser.parse_args()

    ver_nodots = args.ocp_version.replace(".", "")
    image_name = args.image_name or f"rhcos-{ver_nodots}-openstack"
    cache_dir = Path(args.cache_dir)

    qcow2_path = download_and_decompress_rhcos(args.ocp_version, cache_dir, force=args.force_download)

    if args.skip_upload:
        print(f"--skip-upload specified; image cached at {qcow2_path}")
        return

    upload_to_air(qcow2_path, image_name)


if __name__ == "__main__":
    main()
