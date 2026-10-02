#!/usr/bin/env python3
"""
Send Discord notification with new books and covers.
Reads new_books.json and sends rich embed to Discord webhook.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

from app.web.html_builder import cover_src


def create_embed(new_books_data, site_url, kind="audiobook"):
    """Create Discord embed with new books.

    ``kind="ebook"`` is the SAME message for the ebook shelf (owner, 2026-10-02:
    "use the exact same message and format and just specify ebook"): one
    builder, so the two posts cannot drift apart. Only the noun changes, plus a
    Format field on each card where an audiobook has Duration. Both posts are
    sent by deploy.yml on a promote; the ebook one is `--ebooks`, fed by
    app/tools/detect_new_ebooks.py."""
    noun = "ebook" if kind == "ebook" else "book"
    new_count = new_books_data.get("new_count", 0)
    total_count = new_books_data.get("total_count", 0)
    books = new_books_data.get("books", [])
    # total_count is AUDIOBOOKS (rows of catalog.csv). Ebooks are counted
    # separately; None means the count was not available, so say nothing
    # about ebooks rather than "0".
    ebook_count = new_books_data.get("ebook_count")
    library_counts = f"**{total_count}** audiobooks"
    if ebook_count is not None:
        library_counts += f"\n**{ebook_count}** ebooks"

    # Main embed
    embeds = []

    if new_count == 0:
        # No new books, just update notification
        embeds.append(
            {
                "title": "📚 Audiobook Catalog Updated",
                "description": "Catalog refreshed with "
                + library_counts.replace("\n", " and ") + ".",
                "color": 5814783,  # Blue
                "fields": [
                    {"name": "🔗 View Catalog", "value": f"[Click here to browse]({site_url})"},
                    {"name": "⏰ Deployed", "value": f"<t:{int(datetime.now(timezone.utc).timestamp())}:R>"},
                ],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )
    else:
        # New books added
        description = f"**{new_count}** new {noun}{'s' if new_count != 1 else ''} added to the library!"
        if new_count > len(books):
            description += f"\n\n*Showing first {len(books)} {noun}s*"

        embeds.append(
            {
                "title": f"📚 New {noun.capitalize()}s Added!",
                "description": description,
                "color": 3066993,  # Green
                "fields": [
                    {
                        "name": "📊 Library Stats",
                        "value": f"{library_counts}\n**{new_count}** new additions",
                        "inline": True,
                    },
                    {"name": "🔗 View Catalog", "value": f"[Browse Library]({site_url})", "inline": True},
                ],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )

        # Add individual book embeds (max 9 to stay under Discord's 10 embed limit)
        for book in books[:9]:
            title = book.get("title", "Unknown Title")
            author = book.get("author", "Unknown Author")
            series = book.get("series", "")
            series_index = book.get("series_index", "")
            narrator = book.get("narrator", "")
            cover = book.get("cover", "")
            year = book.get("year", "")
            genre = book.get("genre", "")
            duration = book.get("duration", "")

            # Build book description
            book_desc = f"**Author:** {author}"
            if narrator:
                book_desc += f"\n**Narrator:** {narrator}"
            if series:
                series_text = series
                if series_index:
                    series_text += f" #{series_index}"
                book_desc += f"\n**Series:** {series_text}"

            # Build fields
            fields = []
            if year or genre:
                field_value = []
                if year:
                    field_value.append(f"📅 {year}")
                if genre:
                    field_value.append(f"🎭 {genre}")
                fields.append({"name": "Details", "value": " • ".join(field_value), "inline": True})
            if duration:
                fields.append({"name": "Duration", "value": f"⏱️ {duration}", "inline": True})
            if kind == "ebook":
                fmt = (book.get("format") or "").upper()
                fields.append({"name": "Format", "value": "📖 Ebook" + (f" · {fmt}" if fmt else ""), "inline": True})

            book_embed = {
                "title": title[:256],  # Discord title limit
                "description": book_desc[:4096],  # Discord description limit
                "color": 5814783,  # Blue
            }
            
            # Only add fields if there are any
            if fields:
                book_embed["fields"] = fields

            # Add cover image if available.
            # Covers are served from Cloudflare R2, not from site_url — resolve
            # through the one knob (app/config.py COVERS_BASE_URL) so the embed
            # thumbnail keeps working now that site/covers is out of git.
            if cover:
                book_embed["thumbnail"] = {"url": cover_src(cover)}

            embeds.append(book_embed)

    return embeds


def verify_delivery(response):
    """
    Verify Discord webhook delivery.
    Discord returns 204 No Content on success. Log details for debugging.
    """
    status = response.status_code
    # Discord webhook success codes: 200 or 204
    if status in (200, 204):
        print(f"  ✓ Delivery verified: HTTP {status}")
        # Log rate limit headers for observability
        remaining = response.headers.get("X-RateLimit-Remaining")
        reset_after = response.headers.get("X-RateLimit-Reset-After")
        if remaining is not None:
            print(f"  ℹ Rate limit remaining: {remaining} (resets in {reset_after}s)")
        return True
    else:
        print(f"  ✗ Unexpected status: HTTP {status}")
        print(f"  Response body: {response.text[:500]}")
        return False


def send_notification(webhook_url, embeds):
    """Send notification to Discord webhook with delivery verification."""
    payload = {"embeds": embeds}

    try:
        response = requests.post(
            webhook_url, json=payload,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        response.raise_for_status()
        verified = verify_delivery(response)
        if verified:
            print("✓ Discord notification sent and verified")
        return verified
    except requests.exceptions.RequestException as e:
        print(f"✗ First attempt failed: {e}")
        if hasattr(e, "response") and e.response is not None:
            print(f"  Response: {e.response.text[:300]}")

        # Retry without thumbnails (common cause of 400 errors)
        print("  Retrying without cover images...")
        for embed in embeds:
            embed.pop("thumbnail", None)
        payload = {"embeds": embeds}
        try:
            response = requests.post(
                webhook_url, json=payload,
                headers={"Content-Type": "application/json"},
                timeout=30,
            )
            response.raise_for_status()
            verified = verify_delivery(response)
            if verified:
                print("✓ Discord notification sent and verified (without covers)")
            return verified
        except requests.exceptions.RequestException as e2:
            print(f"✗ Retry also failed: {e2}")
            if hasattr(e2, "response") and e2.response is not None:
                print(f"  Response: {e2.response.text[:300]}")
            return False


def main():
    # Get environment variables
    webhook_url = os.environ.get("DISCORD_WEBHOOK")
    # ⚠️ The default used to be https://skymitch9.github.io/audiobook_catalog/.
    # That host was retired on 2026-08-10 (cutover steps 14 and 15) and no longer
    # exists — making the old default a link to nothing, silently, on the one
    # code path nobody watches. deploy.yml passes SITE_URL from the repo
    # variable; this is only the belt to that braces.
    site_url = os.environ.get("SITE_URL", "https://audiobooks.heygabi.ai/")

    if not webhook_url:
        print("DISCORD_WEBHOOK not set, skipping notification")
        sys.exit(0)

    # --ebooks: the ebook post (detect_new_ebooks.py writes new_ebooks.json).
    # Same builder, same webhook, same job; the link goes to the ebook shelf.
    ebooks = "--ebooks" in sys.argv
    kind = "ebook" if ebooks else "audiobook"
    if ebooks:
        site_url = site_url.rstrip("/") + "/ebooks.html"

    # Load new books data
    new_books_file = Path("new_ebooks.json" if ebooks else "new_books.json")
    if not new_books_file.exists():
        if ebooks:
            # Never a generic "updated" post for the ebook shelf: no payload
            # means nothing was detected, not that something changed.
            print("No new_ebooks.json found, skipping the ebook notification")
            sys.exit(0)
        print("No new_books.json found, sending generic update notification")
        new_books_data = {"new_count": 0, "total_count": 0, "books": []}
    else:
        with open(new_books_file, "r", encoding="utf-8") as f:
            new_books_data = json.load(f)

    # Create embeds
    embeds = create_embed(new_books_data, site_url, kind=kind)

    # Send notification with verification. ⚠️ Counts only: this runs in the
    # public repo's Actions log, and ebook titles are gated.
    print(f"Sending Discord notification ({len(embeds)} embed(s))...")
    print(f"  Target: {site_url}")
    print(f"  New {kind}s: {new_books_data.get('new_count', 0)}")
    success = send_notification(webhook_url, embeds)

    if success:
        print("\n=== DISCORD NOTIFICATION: DELIVERED ===")
    else:
        print("\n=== DISCORD NOTIFICATION: FAILED ===")

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
