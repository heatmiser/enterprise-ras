#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""
Prepare and upload a patched RHCOS OpenStack image for NVIDIA Air simulation.

Checks whether the target image already exists in the Air image catalog. If not,
downloads the base RHCOS OpenStack qcow2 from mirror.openshift.com, applies the
two patches required for Air, and uploads the result.

Patches applied
---------------
1. GRUB boot delay (default 30 s)
   Air powers on all VMs simultaneously. OOB switch bridges take ~131 s to become
   operational. A 30 s GRUB delay shifts kernel start so the bridge is ready at
   relative T ≈ 101 s — well inside ignition's default 2-minute window.

2. Ignition fetch-timeout (default 5 m)
   ignition-fetch.service ExecStart uses ${IGNITION_ARGS}, which the ignition
   generator never populates, so ignition silently uses its compiled-in 2 m timeout.
   This patch replaces ${IGNITION_ARGS} with --fetch-timeout 5m inside the
   zstd-compressed initramfs CPIO, giving ignition enough time to survive the
   switch boot delay.

Why the OpenStack flavor?
   RHCOS reads Ignition from http://169.254.169.254/openstack/latest/user_data.
   The utility node in the Air L3 OOB topology spoof-serves that endpoint.
   The qemu flavor reads Ignition from QEMU fw_cfg, which Air does not populate.

Requirements
------------
  guestfish  — libguestfs-tools  (dnf install libguestfs-tools  / apt install libguestfs-tools)
  zstd       — zstd              (dnf install zstd               / apt install zstd)
  air_sdk    — pip install nv-air-sdk  (only needed for --skip-upload=False)

Usage
-----
  # Full flow: check Air, download, patch, upload (idempotent)
  python3 scripts/prepare_rhcos_image.py

  # Prepare locally only (no upload)
  python3 scripts/prepare_rhcos_image.py --skip-upload

  # Force re-download and re-patch
  python3 scripts/prepare_rhcos_image.py --force-download --force-repatch

  # Custom version or name
  python3 scripts/prepare_rhcos_image.py --ocp-version 4.22 --image-name rhcos-422-openstack-gd
"""

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Shared download / upload utilities live in the sibling script.
sys.path.insert(0, str(Path(__file__).parent))
from upload_rhcos_image import (
    _resolve_api_key,           # noqa: F401  (re-exported implicitly via upload_to_air)
    air_image_exists,
    download_and_decompress_rhcos,
    upload_to_air,
)

DEFAULT_IMAGE_NAME = "rhcos-422-openstack-gd"
DEFAULT_GRUB_DELAY = 90         # seconds — Air OOB bridge ~131s wall; 90s GRUB shift ensures NM DHCP window covers bridge-ready event
DEFAULT_FETCH_TIMEOUT = "12m"   # ignition --fetch-timeout value
_IGN_SERVICE = "usr/lib/systemd/system/ignition-fetch.service"


# ---------------------------------------------------------------------------
# Dependency check
# ---------------------------------------------------------------------------

def _check_deps() -> None:
    missing = [t for t in ("guestfish", "zstd") if not shutil.which(t)]
    if missing:
        sys.exit(
            f"ERROR: Missing required tools: {', '.join(missing)}\n"
            "  RHEL/Fedora:    sudo dnf install libguestfs-tools zstd\n"
            "  Ubuntu/Debian:  sudo apt install libguestfs-tools zstd"
        )


# ---------------------------------------------------------------------------
# guestfish helpers
# ---------------------------------------------------------------------------

def _gf(image: Path, readonly: bool, *cmd_groups: list) -> str:
    """Run guestfish with one or more command groups separated by ':'.

    Each element of *cmd_groups is a list of tokens for one guestfish command.
    Returns stdout (stderr is captured and included in RuntimeError on failure).
    """
    mode = "--ro" if readonly else "--rw"
    argv = ["guestfish", "-a", str(image), mode, "run"]
    for group in cmd_groups:
        argv.append(":")
        argv.extend(group)
    r = subprocess.run(argv, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(
            f"guestfish failed (rc={r.returncode})\n"
            f"  stderr: {r.stderr[:600]}\n"
            f"  stdout: {r.stdout[:200]}"
        )
    return r.stdout


def _find_ext4_part(image: Path) -> str:
    """Return the device name of the ext4 boot partition (e.g. /dev/sda3)."""
    out = _gf(image, True, ["list-filesystems"])
    for line in out.splitlines():
        if "ext4" in line:
            return line.split(":")[0].strip()
    raise RuntimeError(f"No ext4 partition found in {image}")


def _gf_download(image: Path, boot_part: str, remote: str, local: Path) -> None:
    _gf(image, True, ["mount", boot_part, "/"], ["download", remote, str(local)])


def _gf_upload(image: Path, boot_part: str, local: Path, remote: str) -> None:
    _gf(image, False, ["mount", boot_part, "/"], ["upload", str(local), remote])


def _gf_ls(image: Path, boot_part: str, remote_dir: str) -> list:
    out = _gf(image, True, ["mount", boot_part, "/"], ["ls", remote_dir])
    return [f.strip() for f in out.splitlines() if f.strip()]


def _gf_read_file(image: Path, boot_part: str, remote: str) -> str:
    return _gf(image, True, ["mount", boot_part, "/"], ["read-file", remote])


def _find_initramfs_remote(image: Path, boot_part: str) -> str:
    """Return the in-guest path to the initramfs image from the BLS entry."""
    entries = _gf_ls(image, boot_part, "/boot/loader/entries/")
    entry = next(
        (e for e in entries if e.endswith(".conf") and "rescue" not in e), None
    )
    if not entry:
        raise RuntimeError("No non-rescue BLS loader entry found in /boot/loader/entries/")
    content = _gf_read_file(image, boot_part, f"/boot/loader/entries/{entry}")
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("initrd "):
            initrd = stripped.split(None, 1)[1].strip()
            if not initrd.startswith("/boot/"):
                initrd = "/boot" + initrd
            return initrd
    raise RuntimeError(f"No 'initrd' line found in loader entry {entry}")


# ---------------------------------------------------------------------------
# Patch 1: GRUB boot delay
# ---------------------------------------------------------------------------

def _apply_grub_patch(image: Path, grub_delay: int) -> None:
    """Set GRUB timeout in /boot/grub2/grub.cfg inside the qcow2."""
    print(f"  Setting GRUB timeout → {grub_delay} s ...")
    boot_part = _find_ext4_part(image)

    with tempfile.NamedTemporaryFile(suffix=".cfg", delete=False, mode="w") as tmp:
        tmp_path = Path(tmp.name)

    try:
        _gf_download(image, boot_part, "/boot/grub2/grub.cfg", tmp_path)
        content = tmp_path.read_text()

        # Replace existing 'set timeout=N' or insert after last 'set ' line
        if re.search(r"^set timeout=", content, re.MULTILINE):
            content = re.sub(
                r"^set timeout=.*", f"set timeout={grub_delay}",
                content, flags=re.MULTILINE,
            )
        else:
            lines = content.splitlines(keepends=True)
            last_set = max(
                (i for i, ln in enumerate(lines) if ln.strip().startswith("set ")),
                default=-1,
            )
            lines.insert(last_set + 1, f"set timeout={grub_delay}\n")
            content = "".join(lines)

        # Ensure timeout_style=menu so the delay actually fires (not hidden/countdown)
        if re.search(r"^set timeout_style=", content, re.MULTILINE):
            content = re.sub(
                r"^set timeout_style=.*", "set timeout_style=menu",
                content, flags=re.MULTILINE,
            )
        else:
            content = re.sub(
                r"^(set timeout=.*)$", r"\1\nset timeout_style=menu",
                content, flags=re.MULTILINE,
            )

        tmp_path.write_text(content)
        _gf_upload(image, boot_part, tmp_path, "/boot/grub2/grub.cfg")
    finally:
        tmp_path.unlink(missing_ok=True)

    print(f"  GRUB patch done (timeout={grub_delay}, timeout_style=menu)")


# ---------------------------------------------------------------------------
# Patch 2: Ignition fetch-timeout (initramfs CPIO)
# ---------------------------------------------------------------------------

def _patch_cpio_service(cpio_data: bytes, target: str, old: bytes, new: bytes) -> bytes:
    """Replace `old` with `new` inside `target` file in a newc CPIO archive.

    Updates the file-size field in the CPIO header. No re-archiving; the output
    CPIO is slightly larger than input but remains valid newc format.
    """
    def pad4(n: int) -> int:
        return (4 - n % 4) % 4

    out = bytearray()
    pos = 0
    patched = 0

    while pos < len(cpio_data):
        magic = cpio_data[pos:pos + 6]
        if magic not in (b"070701", b"070702"):
            out += cpio_data[pos:]
            break

        hdr = bytearray(cpio_data[pos:pos + 110])
        namesize = int(hdr[94:102], 16)
        filesize = int(hdr[54:62], 16)
        name_bytes = cpio_data[pos + 110: pos + 110 + namesize]
        name = name_bytes.rstrip(b"\x00").decode("utf-8", errors="replace")

        p1 = pad4(110 + namesize)
        data_start = pos + 110 + namesize + p1
        file_data = bytearray(cpio_data[data_start: data_start + filesize])
        next_pos = data_start + filesize + pad4(filesize)

        if name == target and old in file_data:
            file_data = bytearray(bytes(file_data).replace(old, new))
            hdr[54:62] = f"{len(file_data):08x}".encode()
            patched += 1
            print(f"    {target}: {old!r} → {new!r}")
            print(f"    size: {filesize} → {len(file_data)} bytes")

        out += bytes(hdr)
        out += name_bytes
        out += b"\x00" * p1
        out += bytes(file_data)
        out += b"\x00" * pad4(len(file_data))

        pos = next_pos
        if name == "TRAILER!!!":
            break

    if patched == 0:
        raise RuntimeError(
            f"Pattern {old!r} not found in {target}\n"
            "  The initramfs format may have changed between RHCOS versions."
        )
    return bytes(out)


def _apply_ignition_timeout_patch(image: Path, fetch_timeout: str) -> None:
    """Patch ignition-fetch.service inside the qcow2 initramfs."""
    print(f"  Patching ignition fetch timeout → {fetch_timeout} ...")
    boot_part = _find_ext4_part(image)
    initramfs_remote = _find_initramfs_remote(image, boot_part)
    print(f"  initramfs path: {initramfs_remote}")

    with tempfile.TemporaryDirectory(prefix="rhcos_patch_") as tmpd:
        tmpdir = Path(tmpd)
        initramfs_local = tmpdir / "initramfs.img"

        print("  Downloading initramfs (30–60 s) ...")
        _gf_download(image, boot_part, initramfs_remote, initramfs_local)
        size_mb = initramfs_local.stat().st_size / 1024 ** 2
        print(f"  Downloaded: {size_mb:.0f} MB")

        # Locate the zstd-compressed main CPIO section
        data = initramfs_local.read_bytes()
        zstd_magic = b"\x28\xb5\x2f\xfd"
        zstd_offset = data.find(zstd_magic)
        if zstd_offset < 0:
            raise RuntimeError(
                "zstd magic not found in initramfs — unexpected format.\n"
                "  Supported: RHCOS 4.17+ (zstd-compressed main CPIO section)."
            )
        print(f"  zstd section offset: {zstd_offset} bytes")

        # Decompress the zstd section
        main_cpio = tmpdir / "main.cpio"
        print("  Decompressing zstd CPIO ...")
        r = subprocess.run(
            ["zstd", "-d", "-", "-o", str(main_cpio)],
            input=data[zstd_offset:], capture_output=True,
        )
        if r.returncode != 0:
            raise RuntimeError(f"zstd decompress failed: {r.stderr[:300]}")
        print(f"  Decompressed: {main_cpio.stat().st_size / 1024**2:.0f} MB")

        # Patch the CPIO in pure Python
        old = b"${IGNITION_ARGS}"
        new = f"--fetch-timeout {fetch_timeout}".encode()
        cpio_patched = _patch_cpio_service(main_cpio.read_bytes(), _IGN_SERVICE, old, new)
        main_cpio_patched = tmpdir / "main_patched.cpio"
        main_cpio_patched.write_bytes(cpio_patched)

        # Recompress with zstd -19 (matches RHCOS compression level)
        main_zstd = tmpdir / "main_patched.zst"
        print("  Recompressing zstd -19 (2–5 min) ...")
        r = subprocess.run(
            ["zstd", "-19", str(main_cpio_patched), "-o", str(main_zstd)],
            capture_output=True,
        )
        if r.returncode != 0:
            raise RuntimeError(f"zstd compress failed: {r.stderr[:300]}")

        # Reassemble: early uncompressed CPIO prefix + recompressed zstd block
        initramfs_patched = tmpdir / "initramfs_patched.img"
        with open(initramfs_patched, "wb") as fout:
            fout.write(data[:zstd_offset])
            fout.write(main_zstd.read_bytes())
        print(f"  Assembled: {initramfs_patched.stat().st_size / 1024**2:.0f} MB")

        # Verify: decompress reassembled and confirm patch string is present
        verify_input = initramfs_patched.read_bytes()[zstd_offset:]
        r = subprocess.run(
            ["zstd", "-d", "--stdout", "-"],
            input=verify_input, capture_output=True,
        )
        if new not in r.stdout:
            raise RuntimeError(
                f"Verification failed: {new!r} not found in reassembled initramfs."
            )
        print(f"  Verified: {new!r} confirmed in patched initramfs")

        # Write patched initramfs back into the qcow2
        print("  Uploading patched initramfs to qcow2 (30–60 s) ...")
        _gf_upload(image, boot_part, initramfs_patched, initramfs_remote)

    print(f"  Ignition timeout patch done (--fetch-timeout {fetch_timeout})")


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def prepare_rhcos_image(
    ocp_version: str,
    image_name: str,
    cache_dir: Path,
    grub_delay: int,
    fetch_timeout: str,
    force_repatch: bool,
    force_download: bool,
    force_upload: bool,
    skip_upload: bool,
) -> None:
    """Full pipeline: Air check → download → patch → upload."""

    # --- Step 0: check whether the image is already in Air ---
    if not skip_upload and not force_repatch and not force_upload and air_image_exists(image_name):
        print(f"\n✓ '{image_name}' already in Air — nothing to do.\n")
        return

    # --- Step 1: download / reuse base image ---
    print("\n── Step 1/4: Download base RHCOS OpenStack image ──────────────────")
    base_image = download_and_decompress_rhcos(
        ocp_version, cache_dir, force=force_download
    )

    # Derive the patched image filename
    major, minor = ocp_version.split(".")[:2]
    patched_image = cache_dir / f"rhcos-{major}.{minor}-openstack-grubdelay.x86_64.qcow2"

    # --- Step 2 / 3: patch (skip if cached and not forced) ---
    if patched_image.exists() and patched_image.stat().st_size > 0 and not force_repatch:
        print(f"\n── Patches already applied — reusing {patched_image.name}")
        print("   Use --force-repatch to redo.\n")
    else:
        _check_deps()

        print(f"\n── Step 2/4: Sparse-copy base → {patched_image.name} ──────────────")
        if patched_image.exists():
            patched_image.unlink()
        subprocess.run(
            ["cp", "--sparse=always", str(base_image), str(patched_image)],
            check=True,
        )
        apparent = patched_image.stat().st_size / 1024 ** 3
        print(f"  Copied: {apparent:.1f} GB apparent")

        print("\n── Step 3a/4: Apply GRUB boot delay ───────────────────────────────")
        _apply_grub_patch(patched_image, grub_delay)

        print("\n── Step 3b/4: Apply ignition fetch-timeout ─────────────────────────")
        _apply_ignition_timeout_patch(patched_image, fetch_timeout)

        print(f"\n✓ Patched image: {patched_image}\n")

    # --- Step 4: upload ---
    if skip_upload:
        print(f"--skip-upload set. Patched image at:\n  {patched_image}\n")
        return

    print(f"\n── Step 4/4: Upload to Air as '{image_name}' ───────────────────────")
    upload_to_air(patched_image, image_name, force=force_upload)
    print(f"\n✓ '{image_name}' is ready in Air.\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--ocp-version", default="4.22", metavar="VER",
        help="OCP version to download (default: 4.22)",
    )
    parser.add_argument(
        "--image-name", default=DEFAULT_IMAGE_NAME, metavar="NAME",
        help=f"Air image name for the patched image (default: {DEFAULT_IMAGE_NAME})",
    )
    parser.add_argument(
        "--cache-dir", default=".cache", metavar="DIR",
        help="Local cache directory for downloaded/patched images (default: .cache)",
    )
    parser.add_argument(
        "--grub-delay", type=int, default=DEFAULT_GRUB_DELAY, metavar="SEC",
        help=f"GRUB boot delay in seconds (default: {DEFAULT_GRUB_DELAY})",
    )
    parser.add_argument(
        "--fetch-timeout", default=DEFAULT_FETCH_TIMEOUT, metavar="DUR",
        help=f"Ignition fetch timeout string (default: {DEFAULT_FETCH_TIMEOUT})",
    )
    parser.add_argument(
        "--force-repatch", action="store_true",
        help="Re-apply patches even if patched image exists in .cache",
    )
    parser.add_argument(
        "--force-download", action="store_true",
        help="Re-download the base RHCOS image even if cached",
    )
    parser.add_argument(
        "--skip-upload", action="store_true",
        help="Prepare image locally only; do not upload to Air",
    )
    parser.add_argument(
        "--force-upload", action="store_true",
        help="Delete existing Air image and re-upload (use with --force-repatch to replace a patched image)",
    )
    args = parser.parse_args()

    prepare_rhcos_image(
        ocp_version=args.ocp_version,
        image_name=args.image_name,
        cache_dir=Path(args.cache_dir),
        grub_delay=args.grub_delay,
        fetch_timeout=args.fetch_timeout,
        force_repatch=args.force_repatch,
        force_download=args.force_download,
        force_upload=args.force_upload,
        skip_upload=args.skip_upload,
    )


if __name__ == "__main__":
    main()
