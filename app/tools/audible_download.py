"""
Download a purchased Audible book via audible-cli and convert it to m4b.

Used by auto_acquire to make acquisition fully hands-off: the top-50 audit
finds a missing purchase, this module pulls it with the owning account's
audible-cli profile and drops a tagged m4b into the container books dir,
where the next sync ingests it into the library.

Profiles are registered once per account with scripts/audible_cli_auth.py
(skylar + samantha). AAX files decrypt with the account's activation bytes;
AAXC files decrypt with the per-download voucher key/iv. Both are lossless
remuxes (-c copy) via ffmpeg.
"""

import glob
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from app.config import SITE_DIR

PROJECT_ROOT = SITE_DIR.parent
DEFAULT_OUT = PROJECT_ROOT / "runtime" / "openaudible" / "books"

# Audible account user_id suffix -> audible-cli profile
PROFILE_BY_USER_SUFFIX = {
    "4I7OE4OQ": "skylar",
    "7QF7NMAA": "samantha",
}
ALL_PROFILES = ["skylar", "samantha"]


def find_ffmpeg():
    p = shutil.which("ffmpeg")
    if p:
        return p
    hits = glob.glob(str(Path.home() / "AppData/Local/Microsoft/WinGet/Packages/Gyan.FFmpeg*/ffmpeg-*/bin/ffmpeg.exe"))
    if hits:
        return hits[0]
    raise RuntimeError("ffmpeg not found (winget install Gyan.FFmpeg)")


def _run(args, timeout=7200):
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", timeout=timeout)


def _audible(profile, *args, timeout=7200):
    return _run([sys.executable, "-m", "audible_cli", "-P", profile, *args], timeout=timeout)


def profile_for(user_id):
    for suffix, profile in PROFILE_BY_USER_SUFFIX.items():
        if (user_id or "").endswith(suffix):
            return profile
    return None


def activation_bytes(profile):
    r = _audible(profile, "activation-bytes", timeout=120)
    for line in reversed((r.stdout or "").strip().splitlines()):
        line = line.strip()
        if re.fullmatch(r"[0-9a-fA-F]{8}", line):
            return line
    raise RuntimeError(f"activation bytes not found for {profile}: {r.stdout} {r.stderr}")


def download_and_convert(asin, title, profile=None, out_dir=DEFAULT_OUT):
    """Download one book and place <safe title>.m4b in out_dir.
    Returns the m4b Path. Raises on failure."""
    ffmpeg = find_ffmpeg()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / f"_dl_{asin}"
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir()

    profiles = [profile] if profile else ALL_PROFILES
    last_err = ""
    got = False
    for prof in profiles:
        r = _audible(prof, "download", "--asin", asin, "--aax-fallback",
                     "--output-dir", str(tmp), "--timeout", "0")
        audio = list(tmp.glob("*.aax")) + list(tmp.glob("*.aaxc"))
        if audio:
            profile = prof
            got = True
            break
        last_err = (r.stderr or r.stdout or "")[-400:]
    if not got:
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError(f"download failed for {asin} ({title}): {last_err}")

    src = audio[0]
    safe_title = re.sub(r'[<>:"/\\|?*]', "-", title).strip() or asin
    dest = out_dir / f"{safe_title}.m4b"

    if src.suffix == ".aax":
        ab = activation_bytes(profile)
        cmd = [ffmpeg, "-y", "-activation_bytes", ab, "-i", str(src), "-c", "copy", str(dest)]
    else:  # .aaxc — voucher json sits next to it
        voucher = json.loads(src.with_suffix(".voucher").read_text(encoding="utf-8"))
        lic = voucher["content_license"]["license_response"]
        cmd = [ffmpeg, "-y", "-audible_key", lic["key"], "-audible_iv", lic["iv"],
               "-i", str(src), "-c", "copy", str(dest)]
    r = _run(cmd, timeout=3600)
    if r.returncode != 0 or not dest.exists():
        raise RuntimeError(f"ffmpeg convert failed: {(r.stderr or '')[-400:]}")
    shutil.rmtree(tmp, ignore_errors=True)
    return dest


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--asin", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--profile", choices=ALL_PROFILES)
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT))
    args = parser.parse_args()
    dest = download_and_convert(args.asin, args.title, args.profile, Path(args.out_dir))
    print(f"OK: {dest} ({dest.stat().st_size/1e6:.0f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
