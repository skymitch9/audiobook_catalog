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
    # paige (2026-09-21): suffix read off her rows in runtime/openaudible/books.json
    "TNJ4QOQA": "paige",
}
ALL_PROFILES = ["skylar", "samantha", "paige"]


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


def natural_key(path):
    """Sort parts the way a person numbers them: 'Day 2' before 'Day 16',
    and 'Bonus …' parts after the numbered ones.

    Filename order is all there is: Audible's own relationship `sort` reads
    999999999 for every part (measured 2026-10-20 on B07NJ92WBN), so plain
    text order put 'Bonus Meditation' ahead of 'Day 1'."""
    name = Path(path).name  # parts are '<Program>_<Part_title>-LC_…'
    bonus = re.search(r"(^|_)bonus_", name.lower()) is not None
    return [bonus] + [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def _ffmeta_escape(value):
    """FFMETADATA1 needs '=', ';', '#', '\\' and newlines backslash-escaped."""
    return re.sub(r"([=;#\\\n])", r"\\\1", str(value))


def build_ffmetadata(global_tags, chapters):
    """An FFMETADATA1 document: global tags, then one chapter per
    (title, start_ms, end_ms). Pure, so it is unit-tested."""
    lines = [";FFMETADATA1"]
    lines += [f"{k}={_ffmeta_escape(v)}" for k, v in global_tags.items() if v]
    for ch_title, start, end in chapters:
        lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={int(start)}", f"END={int(end)}",
                  f"title={_ffmeta_escape(ch_title)}"]
    return "\n".join(lines) + "\n"


def _ffprobe(ffmpeg):
    p = Path(ffmpeg).with_name("ffprobe" + Path(ffmpeg).suffix)
    if p.exists():
        return str(p)
    found = shutil.which("ffprobe")
    if found:
        return found
    raise RuntimeError("ffprobe not found next to ffmpeg")


def _probe(ffprobe, path):
    r = _run([ffprobe, "-v", "error", "-show_entries",
              "format=duration:format_tags:stream=codec_type,codec_name,sample_rate,channels",
              "-of", "json", str(path)], timeout=300)
    return json.loads(r.stdout or "{}")


def _decrypt_input(src, profile, ab_cache):
    """ffmpeg input args that decrypt one .aax/.aaxc file."""
    if src.suffix == ".aax":
        if profile not in ab_cache:
            ab_cache[profile] = activation_bytes(profile)
        return ["-activation_bytes", ab_cache[profile], "-i", str(src)]
    voucher = json.loads(src.with_suffix(".voucher").read_text(encoding="utf-8"))
    lic = voucher["content_license"]["license_response"]
    return ["-audible_key", lic["key"], "-audible_iv", lic["iv"], "-i", str(src)]


def _join_parts(ffmpeg, parts, profile, title, narrator, tmp, dest):
    """Join a multi-part title into ONE m4b, losslessly (-c copy), with one
    chapter per part named from that part's own title tag. Global tags come
    from the first part, with title/album set to the program's title, so the
    catalog reads it exactly like a single-file book. The cover is the first
    part's. Refuses rather than guesses if the parts' audio formats differ,
    because a stream-copy concat of mismatched AAC produces broken audio."""
    ffprobe = _ffprobe(ffmpeg)
    work = tmp / "_join"
    work.mkdir(exist_ok=True)
    ab_cache = {}
    decrypted, chapters, formats = [], [], set()
    t = 0.0
    for i, part in enumerate(parts):
        out = work / f"{i:03d}.m4a"
        r = _run([ffmpeg, "-y", *_decrypt_input(part, profile, ab_cache),
                  "-map", "0:a", "-c", "copy", str(out)], timeout=3600)
        if r.returncode != 0 or not out.exists():
            raise RuntimeError(f"decrypt failed for part {part.name}: {(r.stderr or '')[-300:]}")
        info = _probe(ffprobe, out)
        dur = float(info.get("format", {}).get("duration") or 0)
        if dur <= 0:
            raise RuntimeError(f"part {part.name} has no duration after decrypt")
        a = next(s for s in info.get("streams", []) if s.get("codec_type") == "audio")
        formats.add((a.get("codec_name"), a.get("sample_rate"), a.get("channels")))
        part_tags = _probe(ffprobe, part).get("format", {}).get("tags", {})
        if i == 0:
            first_tags = part_tags
        chapters.append((part_tags.get("title") or part.stem, round(t * 1000), round((t + dur) * 1000)))
        t += dur
        decrypted.append(out)
    if len(formats) != 1:
        raise RuntimeError(f"parts have mixed audio formats {sorted(formats)}; not joining")

    keep = ("artist", "album_artist", "genre", "date", "copyright", "comment", "description")
    tags = {k: first_tags.get(k) for k in keep}
    tags.update(title=title, album=title, composer=narrator or first_tags.get("composer"))
    meta = work / "meta.txt"
    meta.write_text(build_ffmetadata(tags, chapters), encoding="utf-8")
    listing = work / "list.txt"
    listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in decrypted), encoding="utf-8")

    cover = work / "cover.jpg"
    _run([ffmpeg, "-y", *_decrypt_input(parts[0], profile, ab_cache),
          "-map", "0:v:0", "-c", "copy", "-frames:v", "1", str(cover)], timeout=300)
    cmd = [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-i", str(meta)]
    if cover.exists() and cover.stat().st_size > 0:
        cmd += ["-i", str(cover), "-map", "0:a", "-map", "2:v",
                "-c:v", "copy", "-disposition:v:0", "attached_pic"]
    else:
        cmd += ["-map", "0:a"]
    cmd += ["-map_metadata", "1", "-map_chapters", "1", "-c:a", "copy", str(dest)]
    r = _run(cmd, timeout=3600)
    if r.returncode != 0 or not dest.exists():
        raise RuntimeError(f"ffmpeg join failed: {(r.stderr or '')[-400:]}")


def download_and_convert(asin, title, profile=None, out_dir=DEFAULT_OUT, narrator=None):
    """Download one book and place <safe title>.m4b in out_dir.
    Returns the m4b Path. Raises on failure.

    Audible AAX/AAXC files don't tag the narrator, but the catalog reads it
    from the m4b's composer (\\xa9wrt) tag — so when we know the narrator (from
    the audible-cli library export) we write it in during the remux. Without
    this the promote guard's core-features audit rejects the book for a
    missing narrator."""
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
        # ⚠️ rglob, not glob: a MULTI-PART title (Audible's fitness/meditation
        # programs, Letters From Camp) lands as a SUBFOLDER of parts, and a
        # top-level glob found nothing, so all 8 such purchases failed
        # (2026-10-20) after downloading every part.
        audio = list(tmp.rglob("*.aax")) + list(tmp.rglob("*.aaxc"))
        if audio:
            profile = prof
            got = True
            break
        last_err = (r.stderr or r.stdout or "")[-400:]
    if not got:
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError(f"download failed for {asin} ({title}): {last_err}")

    safe_title = re.sub(r'[<>:"/\\|?*]', "-", title).strip() or asin
    dest = out_dir / f"{safe_title}.m4b"

    if len(audio) > 1:
        _join_parts(ffmpeg, sorted(audio, key=natural_key), profile, title, narrator, tmp, dest)
        shutil.rmtree(tmp, ignore_errors=True)
        return dest

    src = audio[0]
    meta = ["-metadata", f"composer={narrator}"] if narrator else []
    if src.suffix == ".aax":
        ab = activation_bytes(profile)
        cmd = [ffmpeg, "-y", "-activation_bytes", ab, "-i", str(src),
               "-c", "copy", *meta, str(dest)]
    else:  # .aaxc — voucher json sits next to it
        voucher = json.loads(src.with_suffix(".voucher").read_text(encoding="utf-8"))
        lic = voucher["content_license"]["license_response"]
        cmd = [ffmpeg, "-y", "-audible_key", lic["key"], "-audible_iv", lic["iv"],
               "-i", str(src), "-c", "copy", *meta, str(dest)]
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
    parser.add_argument("--narrator", default=None, help="write as composer/narrator tag")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT))
    args = parser.parse_args()
    dest = download_and_convert(args.asin, args.title, args.profile,
                                Path(args.out_dir), narrator=args.narrator)
    print(f"OK: {dest} ({dest.stat().st_size/1e6:.0f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
