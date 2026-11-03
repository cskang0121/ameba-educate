#!/usr/bin/env python3
"""Download the Ameba toolchain archive and extract it into the SDK tools dir.

Run under WSL/Linux so that tar preserves symlinks and executable bits.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

URL = (
    "https://github.com/Ameba-AIoT/ameba-toolchain/releases/download/"
    "V10.4.1-ameba-rtos-pro2/asdk-10.4.1-linux-newlib-build-3768-x86_64.tar.bz2"
)
ROOT = Path(__file__).resolve().parent
DEFAULT_DEST = ROOT / "sdk" / "ameba-rtos-pro2" / "tools"
DEFAULT_CACHE = ROOT / "tool_chain"


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")

    def progress(blocks: int, block_size: int, total: int) -> None:
        done = blocks * block_size
        if total > 0:
            pct = min(100, done * 100 // total)
            print(f"\r  {done / 1e6:7.1f} / {total / 1e6:.1f} MB ({pct}%)", end="", flush=True)
        else:
            print(f"\r  {done / 1e6:7.1f} MB", end="", flush=True)

    print(f"Downloading {url}")
    urllib.request.urlretrieve(url, tmp, progress)
    print()
    tmp.replace(dest)


def archive_top_dir(archive: Path) -> str:
    """Name of the top-level folder in the archive (first entry's first path component)."""
    proc = subprocess.Popen(["tar", "-jtf", str(archive)], stdout=subprocess.PIPE, text=True)
    first = proc.stdout.readline().strip()
    proc.kill()
    proc.wait()
    name = first.lstrip("./").split("/", 1)[0]
    if not name:
        raise OSError(f"could not read archive contents: {archive}")
    return name


def find_gcc(toolchain_dir: Path) -> Path | None:
    return next((toolchain_dir / "linux" / "newlib" / "bin").glob("*-gcc"), None)


def extract(archive: Path, dest: Path, verbose: bool) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    print(f"  {archive.name} -> {dest}")
    flags = "-jxvf" if verbose else "-jxf"
    subprocess.run(["tar", flags, str(archive), "-C", str(dest)], check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=URL)
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST, help="extraction directory")
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE, help="download directory")
    parser.add_argument("--force", action="store_true", help="reinstall even if this version is already installed")
    parser.add_argument("-v", "--verbose", action="store_true", help="list extracted files")
    args = parser.parse_args()

    if sys.platform == "win32" or not shutil.which("tar"):
        print("Error: run this under WSL/Linux with tar installed.", file=sys.stderr)
        return 1

    archive = args.cache / args.url.rsplit("/", 1)[-1]

    # The archive's top-level folder is named after the version in the file name,
    # so we can tell whether it is already installed before downloading anything.
    match = re.match(r"(asdk-\d+(?:\.\d+)*)-", archive.name)
    top_dir = match.group(1) if match else None
    if top_dir and not args.force and find_gcc(args.dest / top_dir) is not None:
        print(f"[1/3] Download ... SKIPPED ({top_dir} already installed in {args.dest})")
        print("[2/3] Extract ... SKIPPED (use --force to reinstall)")
    else:
        try:
            if archive.exists() and not args.force:
                print(f"[1/3] Download: using cached {archive} ... OK")
            else:
                print("[1/3] Download")
                download(args.url, archive)
                print(f"[1/3] Download ... OK ({archive.stat().st_size / 1e6:.1f} MB)")

            print("[2/3] Extract")
            top_dir = archive_top_dir(archive)
            extract(archive, args.dest, args.verbose)
            print("[2/3] Extract ... OK")
        except (OSError, subprocess.CalledProcessError) as exc:
            print(f"\nError: {exc}", file=sys.stderr)
            return 1

    print("[3/3] Verify toolchain")
    new_dir = args.dest / top_dir
    bin_dir = new_dir / "linux" / "newlib" / "bin"
    gcc = find_gcc(new_dir)
    if gcc is None:
        print(f"Error: no compiler (*-gcc) found in {bin_dir}", file=sys.stderr)
        return 1
    print(f"[3/3] Verify ... OK: {gcc.name} in {bin_dir}")

    # Only clean up once the toolchain is verified.
    if archive.exists():
        archive.unlink()
        print(f"Removed archive {archive}")
    for old in sorted(args.dest.glob("asdk-*")):
        if old.is_dir() and old != new_dir:
            shutil.rmtree(old)
            print(f"Removed old toolchain {old}")

    print("\nDone.\n\nNEXT: Add the toolchain to PATH:")
    print(f"  export PATH={bin_dir}:$PATH")
    return 0


if __name__ == "__main__":
    sys.exit(main())
