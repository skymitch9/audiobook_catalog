#!/usr/bin/env python3
"""
Generate a test audiobook file (.m4b) for verifying the pipeline.

Creates a small, valid M4B file with proper MP4/iTunes metadata tags so you can
test the full flow: sort -> upload -> catalog rebuild -> Discord notification.

The file is placed in the OpenAudible export directory (or a custom path) so it
gets picked up by the sync pipeline on the next run.

Usage:
    python scripts/generate_test_book.py                     # Default test book
    python scripts/generate_test_book.py --title "My Book"   # Custom title
    python scripts/generate_test_book.py --author "Test Author" --series "Test Series" --index 1
    python scripts/generate_test_book.py --output ./test.m4b # Custom output path
    python scripts/generate_test_book.py --clean             # Remove previously generated test books

The generated file is ~50KB (silent AAC audio) with full metadata tags matching
what OpenAudible produces, including: title, author, narrator, year, genre,
series name, series index, and a cover image.
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
import tempfile
from datetime import datetime
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS_DIR.parent

sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

OPENAUDIBLE_DIR = Path(os.getenv("ROOT_DIR", r"C:\Users\nbasl\OpenAudible\books"))

# Tag identifiers (matches what metadata.py reads)
K_TITLE = "\xa9nam"
K_ARTIST = "\xa9ART"
K_WRITER = "\xa9wrt"
K_DAY = "\xa9day"
K_GENRE = "\xa9gen"
K_COMMENT = "\xa9cmt"
K_SERIES_VENDOR = "SRNM"
K_INDEX_VENDOR = "SRSQ"

# Unique prefix so test files are easy to identify and clean up
TEST_PREFIX = "_TEST_PIPELINE_"


def _generate_silent_m4b(output_path: Path, duration_seconds: int = 5) -> None:
    """
    Generate a minimal valid M4B file with silent audio.
    Uses mutagen to create the container if available, otherwise creates
    a raw MP4 with minimal moov/mdat atoms.
    """
    try:
        # Try using mutagen + a minimal AAC frame approach
        from mutagen.mp4 import MP4

        # Create a minimal valid MP4 file structure
        # ftyp + moov + mdat with a single silent AAC-LC frame
        _write_minimal_mp4(output_path, duration_seconds)
    except ImportError:
        # Fallback: write raw MP4 atoms
        _write_minimal_mp4(output_path, duration_seconds)


def _write_minimal_mp4(output_path: Path, duration_seconds: int) -> None:
    """
    Write a minimal but valid MP4/M4B file that mutagen can then tag.
    Uses ffmpeg if available, otherwise creates a bare-minimum structure.
    """
    import subprocess

    # Try ffmpeg first (produces a fully valid file)
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-f", "lavfi",
                "-i", f"anullsrc=r=44100:cl=mono",
                "-t", str(duration_seconds),
                "-c:a", "aac", "-b:a", "16k",
                "-f", "ipod",  # M4B-compatible container
                str(output_path),
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            return
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Fallback: try with a pre-built silent M4B template
    # Generate a minimal valid MP4 using raw bytes
    # This creates the smallest valid ftyp+moov+mdat that mutagen accepts
    _write_raw_mp4_fallback(output_path)


def _write_raw_mp4_fallback(output_path: Path) -> None:
    """
    Write a bare-minimum MP4 file structure.
    This won't play audio but is valid enough for mutagen to add tags.
    """
    # Minimal ftyp box
    ftyp_data = b"M4B "  # major brand
    ftyp_data += struct.pack(">I", 0)  # minor version
    ftyp_data += b"isom" + b"M4B " + b"mp42"  # compatible brands
    ftyp_box = struct.pack(">I", 8 + len(ftyp_data)) + b"ftyp" + ftyp_data

    # Minimal mdat box (empty media data)
    mdat_box = struct.pack(">I", 8) + b"mdat"

    # Minimal moov box with mvhd
    mvhd_data = struct.pack(">I", 0)  # version + flags
    mvhd_data += struct.pack(">I", 0)  # creation time
    mvhd_data += struct.pack(">I", 0)  # modification time
    mvhd_data += struct.pack(">I", 44100)  # timescale
    mvhd_data += struct.pack(">I", 44100 * 5)  # duration (5 seconds)
    mvhd_data += struct.pack(">I", 0x00010000)  # rate (1.0)
    mvhd_data += struct.pack(">H", 0x0100)  # volume (1.0)
    mvhd_data += b"\x00" * 10  # reserved
    # Matrix (identity)
    mvhd_data += struct.pack(">9I",
        0x00010000, 0, 0,
        0, 0x00010000, 0,
        0, 0, 0x40000000,
    )
    mvhd_data += b"\x00" * 24  # pre-defined
    mvhd_data += struct.pack(">I", 2)  # next track ID

    mvhd_box = struct.pack(">I", 8 + len(mvhd_data)) + b"mvhd" + mvhd_data

    # Minimal trak box with tkhd
    tkhd_data = struct.pack(">I", 0x00000001)  # version + flags (track enabled)
    tkhd_data += struct.pack(">I", 0)  # creation time
    tkhd_data += struct.pack(">I", 0)  # modification time
    tkhd_data += struct.pack(">I", 1)  # track ID
    tkhd_data += struct.pack(">I", 0)  # reserved
    tkhd_data += struct.pack(">I", 44100 * 5)  # duration
    tkhd_data += b"\x00" * 8  # reserved
    tkhd_data += struct.pack(">H", 0)  # layer
    tkhd_data += struct.pack(">H", 0)  # alternate group
    tkhd_data += struct.pack(">H", 0x0100)  # volume
    tkhd_data += struct.pack(">H", 0)  # reserved
    # Matrix (identity)
    tkhd_data += struct.pack(">9I",
        0x00010000, 0, 0,
        0, 0x00010000, 0,
        0, 0, 0x40000000,
    )
    tkhd_data += struct.pack(">I", 0)  # width
    tkhd_data += struct.pack(">I", 0)  # height

    tkhd_box = struct.pack(">I", 8 + len(tkhd_data)) + b"tkhd" + tkhd_data

    # Minimal mdia with mdhd + hdlr + minf/stbl
    mdhd_data = struct.pack(">I", 0)  # version + flags
    mdhd_data += struct.pack(">I", 0)  # creation
    mdhd_data += struct.pack(">I", 0)  # modification
    mdhd_data += struct.pack(">I", 44100)  # timescale
    mdhd_data += struct.pack(">I", 44100 * 5)  # duration
    mdhd_data += struct.pack(">H", 0x55C4)  # language (und)
    mdhd_data += struct.pack(">H", 0)  # quality
    mdhd_box = struct.pack(">I", 8 + len(mdhd_data)) + b"mdhd" + mdhd_data

    hdlr_data = struct.pack(">I", 0)  # version + flags
    hdlr_data += struct.pack(">I", 0)  # pre-defined
    hdlr_data += b"soun"  # handler type
    hdlr_data += b"\x00" * 12  # reserved
    hdlr_data += b"SoundHandler\x00"  # name
    hdlr_box = struct.pack(">I", 8 + len(hdlr_data)) + b"hdlr" + hdlr_data

    # Empty stbl with required sub-boxes
    stsd_box = struct.pack(">I", 16) + b"stsd" + struct.pack(">I", 0) + struct.pack(">I", 0)
    stts_box = struct.pack(">I", 16) + b"stts" + struct.pack(">I", 0) + struct.pack(">I", 0)
    stsc_box = struct.pack(">I", 16) + b"stsc" + struct.pack(">I", 0) + struct.pack(">I", 0)
    stsz_box = struct.pack(">I", 20) + b"stsz" + struct.pack(">I", 0) + struct.pack(">I", 0) + struct.pack(">I", 0)
    stco_box = struct.pack(">I", 16) + b"stco" + struct.pack(">I", 0) + struct.pack(">I", 0)

    stbl_content = stsd_box + stts_box + stsc_box + stsz_box + stco_box
    stbl_box = struct.pack(">I", 8 + len(stbl_content)) + b"stbl" + stbl_content

    # smhd + dinf
    smhd_box = struct.pack(">I", 16) + b"smhd" + b"\x00" * 8
    dref_box = struct.pack(">I", 20) + b"dref" + struct.pack(">I", 0) + struct.pack(">I", 0)
    dinf_box = struct.pack(">I", 8 + len(dref_box)) + b"dinf" + dref_box

    minf_content = smhd_box + dinf_box + stbl_box
    minf_box = struct.pack(">I", 8 + len(minf_content)) + b"minf" + minf_content

    mdia_content = mdhd_box + hdlr_box + minf_box
    mdia_box = struct.pack(">I", 8 + len(mdia_content)) + b"mdia" + mdia_content

    trak_content = tkhd_box + mdia_box
    trak_box = struct.pack(">I", 8 + len(trak_content)) + b"trak" + trak_content

    moov_content = mvhd_box + trak_box
    moov_box = struct.pack(">I", 8 + len(moov_content)) + b"moov" + moov_content

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "wb") as f:
        f.write(ftyp_box + moov_box + mdat_box)


def _generate_test_cover() -> bytes:
    """Generate a simple 100x100 JPEG-like placeholder cover image."""
    try:
        # Try PIL/Pillow for a proper image
        from PIL import Image
        import io

        img = Image.new("RGB", (200, 200), color=(70, 130, 180))
        # Draw a simple "T" for "Test"
        from PIL import ImageDraw
        draw = ImageDraw.Draw(img)
        draw.rectangle([20, 20, 180, 180], outline=(255, 255, 255), width=3)
        draw.text((60, 70), "TEST", fill=(255, 255, 255))

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=60)
        return buf.getvalue()
    except ImportError:
        # Minimal valid 1x1 red JPEG (generated, avoids escape issues)
        import base64
        # A tiny valid JPEG (1x1 pixel, red)
        b64 = (
            "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkS"
            "Ew8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJ"
            "CQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIy"
            "MjIyMjIyMjIyMjIyMjL/wAARCAABAAEDASIAAhEBAxEB/8QAFAABAAAAAAAAAAAAAAAAAAAACf"
            "/EABQQAQAAAAAAAAAAAAAAAAAAAAD/xAAUAQEAAAAAAAAAAAAAAAAAAAAA/8QAFBEBAAAA"
            "AAAAAAAAAAAAAAAAAP/aAAwDAQACEQMRAD8AKwA//9k="
        )
        return base64.b64decode(b64)


def apply_tags(
    file_path: Path,
    title: str,
    author: str,
    narrator: str,
    year: str,
    genre: str,
    series: str | None = None,
    series_index: str | None = None,
    description: str | None = None,
) -> bool:
    """Apply MP4/iTunes metadata tags to the file using mutagen."""
    try:
        from mutagen.mp4 import MP4, MP4Cover

        audio = MP4(str(file_path))
        if audio.tags is None:
            audio.add_tags()

        audio.tags[K_TITLE] = [title]
        audio.tags[K_ARTIST] = [author]
        audio.tags[K_WRITER] = [narrator]
        audio.tags[K_DAY] = [year]
        audio.tags[K_GENRE] = [genre]

        if description:
            audio.tags["\xa9cmt"] = [description]

        if series:
            # Use the vendor atoms that the pipeline prefers
            audio.tags[K_SERIES_VENDOR] = [series]
        if series_index:
            audio.tags[K_INDEX_VENDOR] = [series_index]

        # Add cover art
        cover_data = _generate_test_cover()
        audio.tags["covr"] = [MP4Cover(cover_data, imageformat=MP4Cover.FORMAT_JPEG)]

        audio.save()
        return True
    except Exception as e:
        print(f"  [WARN] Could not apply tags: {e}")
        print("  File was created but may lack metadata.")
        return False


def generate_test_book(
    title: str = "Test Book - Pipeline Verification",
    author: str = "Test Author",
    narrator: str = "Test Narrator",
    year: str | None = None,
    genre: str = "Test/Verification",
    series: str | None = "Test Series",
    series_index: str | None = "1",
    output: Path | None = None,
) -> Path:
    """Generate a test audiobook file and return its path."""
    if year is None:
        year = str(datetime.now().year)

    # Build filename
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_title = title.replace(" ", "_").replace("/", "-")[:50]
    filename = f"{TEST_PREFIX}{safe_title}_{timestamp}.m4b"

    if output:
        out_path = output
    else:
        # Place in OpenAudible dir (where sync pipeline picks it up)
        # Put in an author subfolder so sort step can find it
        author_dir = OPENAUDIBLE_DIR / author
        author_dir.mkdir(parents=True, exist_ok=True)
        out_path = author_dir / filename

    print(f"Generating test book: {out_path.name}")
    print(f"  Title: {title}")
    print(f"  Author: {author}")
    print(f"  Narrator: {narrator}")
    print(f"  Series: {series or 'N/A'} #{series_index or 'N/A'}")
    print(f"  Genre: {genre}")

    # Generate the audio container
    _generate_silent_m4b(out_path)

    # Apply metadata tags
    tagged = apply_tags(
        out_path,
        title=title,
        author=author,
        narrator=narrator,
        year=year,
        genre=genre,
        series=series,
        series_index=series_index,
        description=(
            f"This is a test file generated by generate_test_book.py at {timestamp}. "
            f"It verifies the audiobook pipeline: sort, upload, catalog, and Discord notification. "
            f"Safe to delete after testing."
        ),
    )

    size_kb = out_path.stat().st_size / 1024
    print(f"\n  Output: {out_path}")
    print(f"  Size: {size_kb:.1f} KB")
    if tagged:
        print("  Tags: Applied successfully")
    print(f"\n  To run the pipeline: python scripts/sync_to_drive.py --dry-run")
    print(f"  To clean up test files: python scripts/generate_test_book.py --clean")

    return out_path


def clean_test_books() -> None:
    """Remove all previously generated test books from the library."""
    print("Scanning for test books...")
    removed = 0

    if OPENAUDIBLE_DIR.exists():
        for f in OPENAUDIBLE_DIR.rglob(f"{TEST_PREFIX}*"):
            if f.is_file():
                print(f"  Removing: {f}")
                f.unlink()
                removed += 1

    # Also check the main library
    from app.config import ROOT_DIR
    if ROOT_DIR.exists() and ROOT_DIR != OPENAUDIBLE_DIR:
        for f in ROOT_DIR.rglob(f"{TEST_PREFIX}*"):
            if f.is_file():
                print(f"  Removing: {f}")
                f.unlink()
                removed += 1

    if removed:
        print(f"\n  Cleaned up {removed} test file(s).")
    else:
        print("  No test files found.")


def main():
    parser = argparse.ArgumentParser(
        description="Generate a test audiobook file for pipeline verification"
    )
    parser.add_argument("--title", default="Test Book - Pipeline Verification",
                        help="Book title")
    parser.add_argument("--author", default="Test Author",
                        help="Author name")
    parser.add_argument("--narrator", default="Test Narrator",
                        help="Narrator name")
    parser.add_argument("--year", default=None,
                        help="Year (defaults to current year)")
    parser.add_argument("--genre", default="Test/Verification",
                        help="Genre")
    parser.add_argument("--series", default="Test Series",
                        help="Series name (use '' for no series)")
    parser.add_argument("--index", default="1",
                        help="Series index")
    parser.add_argument("--output", type=Path, default=None,
                        help="Custom output path (otherwise placed in OpenAudible dir)")
    parser.add_argument("--clean", action="store_true",
                        help="Remove all previously generated test books")

    args = parser.parse_args()

    if args.clean:
        clean_test_books()
        return

    generate_test_book(
        title=args.title,
        author=args.author,
        narrator=args.narrator,
        year=args.year,
        genre=args.genre,
        series=args.series if args.series else None,
        series_index=args.index if args.series else None,
        output=args.output,
    )


if __name__ == "__main__":
    main()
