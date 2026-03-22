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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Flash a left/right UF2 from firmware/ onto a mounted nice!nano UF2 drive"
    )
    parser.add_argument(
        "side",
        nargs="?",
        default="left",
        choices=["left", "right"],
        help="Which firmware side to flash (default: left)",
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
    seen: set[str] = set()
    with Path("/proc/mounts").open("r", encoding="utf-8") as handle:
        for line in handle:
            parts = line.split()
            if len(parts) < 2:
                continue
            mount = Path(parts[1].replace("\\040", " "))
            try:
                has_info = (mount / "INFO_UF2.TXT").exists()
                has_current = (mount / "CURRENT.UF2").exists()
            except OSError:
                continue

            if has_info or has_current:
                key = str(mount)
                if key not in seen:
                    mounts.append(mount)
                    seen.add(key)
    return mounts


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


def wait_until_mount_removed(mount: Path, wait_seconds: int) -> bool:
    deadline = time.time() + max(0, wait_seconds)
    while time.time() <= deadline:
        if not mount.exists():
            return True
        time.sleep(0.25)
    return not mount.exists()


def flash_side(side: str, firmware_dir: Path, mount: Path, dry_run: bool) -> None:
    files = firmware_candidates(firmware_dir)
    firmware = choose_firmware(files, side)
    destination = mount / firmware.name

    print(f"Mount:      {mount}")
    print(f"Side:       {side}")
    print(f"Firmware:   {firmware}")
    print(f"Copy to:    {destination}")

    if dry_run:
        return

    # Use copyfile instead of copy2: UF2 drives can disappear immediately after
    # flashing, and copy2's metadata step may fail with ENOENT even when flashing
    # actually succeeded.
    shutil.copyfile(firmware, destination)
    os.sync()
    print("Flash copy complete.")


def main() -> int:
    args = parse_args()

    firmware_dir = Path(args.firmware_dir).expanduser().resolve()
    mount = resolve_mount(args.mount, args.wait)

    flash_side(args.side, firmware_dir, mount, args.dry_run)

    if args.side == "left":
        if not args.dry_run:
            print("\nLeft flashed. Waiting for that UF2 mount to disconnect...")
            wait_until_mount_removed(mount, args.wait)
        print(
            "\nNow flash RIGHT: put right half in bootloader "
            f"(waiting up to {max(0, args.wait)}s)..."
        )
        right_mount = resolve_mount(args.mount, args.wait)
        flash_side("right", firmware_dir, right_mount, args.dry_run)

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
