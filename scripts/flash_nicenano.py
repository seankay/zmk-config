#!/usr/bin/env python3

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path


SIDE_TOKENS = {
    "left": ("left", "_left", "+left", "-left"),
    "right": ("right", "_right", "+right", "-right"),
}

LEFT_MARKERS = (
    b"corne_left",
    b"corne-left",
)

RIGHT_MARKERS = (
    b"corne_right",
    b"corne-right",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Flash a left/right UF2 from firmware/ onto a mounted nice!nano UF2 drive"
    )
    parser.add_argument(
        "side",
        nargs="?",
        default="auto",
        choices=["left", "right", "auto"],
        help="Which firmware side to flash (default: auto)",
    )
    parser.add_argument(
        "--firmware-dir",
        default="firmware",
        help="Directory containing UF2 artifacts (default: firmware)",
    )
    parser.add_argument(
        "--mount",
        default="",
        help="UF2 mount path. If omitted, auto-detect from mounted filesystems.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be flashed without copying.",
    )
    parser.add_argument(
        "--wait",
        type=int,
        default=30,
        help="Wait up to N seconds for UF2 mount to appear (default: 30).",
    )
    return parser.parse_args()


def find_uf2_mounts() -> list[Path]:
    mounts: list[Path] = []
    with Path("/proc/mounts").open("r", encoding="utf-8") as handle:
        for line in handle:
            parts = line.split()
            if len(parts) < 2:
                continue
            mount = Path(parts[1])
            try:
                has_info = (mount / "INFO_UF2.TXT").exists()
                has_current = (mount / "CURRENT.UF2").exists()
            except OSError:
                continue

            if has_info or has_current:
                mounts.append(mount)
    return mounts


def read_binary(path: Path, max_bytes: int = 4 * 1024 * 1024) -> bytes:
    try:
        with path.open("rb") as handle:
            return handle.read(max_bytes).lower()
    except OSError:
        return b""


def detect_side_from_mount(mount: Path) -> str | None:
    info_text = mount / "INFO_UF2.TXT"
    info_blob = read_binary(info_text, max_bytes=32 * 1024)
    current_blob = read_binary(mount / "CURRENT.UF2")
    blob = info_blob + b"\n" + current_blob

    has_left = any(marker in blob for marker in LEFT_MARKERS)
    has_right = any(marker in blob for marker in RIGHT_MARKERS)

    if has_left and not has_right:
        return "left"
    if has_right and not has_left:
        return "right"
    return None


def firmware_candidates(firmware_dir: Path) -> list[Path]:
    if not firmware_dir.exists():
        raise FileNotFoundError(f"Firmware directory not found: {firmware_dir}")
    files = sorted(firmware_dir.glob("*.uf2"))
    if not files:
        raise FileNotFoundError(f"No UF2 files found in: {firmware_dir}")
    return files


def choose_firmware(files: list[Path], side: str) -> Path:
    tokens = SIDE_TOKENS[side]

    def score(path: Path) -> int:
        name = path.name.lower()
        token_hit = any(token in name for token in tokens)
        return (100 if token_hit else 0) + len(name)

    matching = [
        path for path in files if any(token in path.name.lower() for token in tokens)
    ]
    if matching:
        return sorted(matching, key=score, reverse=True)[0]

    if len(files) == 1:
        return files[0]

    names = "\n  - ".join(path.name for path in files)
    raise RuntimeError(
        f"Could not pick a {side} UF2 from firmware directory. Available files:\n  - {names}"
    )


def resolve_mount(explicit_mount: str, wait_seconds: int) -> Path:
    wait_seconds = max(0, wait_seconds)

    if explicit_mount:
        mount = Path(explicit_mount).expanduser().resolve()
        deadline = time.time() + wait_seconds
        while not mount.exists() and time.time() <= deadline:
            time.sleep(0.5)
        if not mount.exists():
            raise FileNotFoundError(f"Mount path does not exist: {mount}")
        return mount

    deadline = time.time() + wait_seconds
    while True:
        mounts = find_uf2_mounts()
        if mounts:
            if len(mounts) > 1:
                listed = "\n  - ".join(str(path) for path in mounts)
                raise RuntimeError(
                    f"Multiple UF2 mounts found. Pass --mount to choose one:\n  - {listed}"
                )
            return mounts[0]

        if time.time() > deadline:
            raise RuntimeError(
                "No UF2 mount detected. Put the nice!nano in bootloader mode first."
            )
        time.sleep(0.5)


def main() -> int:
    args = parse_args()

    firmware_dir = Path(args.firmware_dir).expanduser().resolve()
    mount = resolve_mount(args.mount, args.wait)
    files = firmware_candidates(firmware_dir)

    side = args.side
    if side == "auto":
        detected_side = detect_side_from_mount(mount)
        if not detected_side:
            raise RuntimeError(
                "Could not auto-detect left/right from mounted device. "
                "Use 'left' or 'right' explicitly."
            )
        side = detected_side

    firmware = choose_firmware(files, side)
    destination = mount / firmware.name

    print(f"Mount:      {mount}")
    print(f"Side:       {side}")
    print(f"Firmware:   {firmware}")
    print(f"Copy to:    {destination}")

    if args.dry_run:
        return 0

    # Use copyfile instead of copy2: UF2 drives can disappear immediately after
    # flashing, and copy2's metadata step may fail with ENOENT even when flashing
    # actually succeeded.
    shutil.copyfile(firmware, destination)
    os.sync()
    print("Flash copy complete.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
