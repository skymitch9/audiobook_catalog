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


def create_embed(new_books_data, site_url):
    """Create Discord embed with new books."""
    new_count = new_books_data.get("new_count", 0)
    total_count = new_books_data.get("total_count", 0)
    books = new_books_data.get("books", [])

    # Main embed
    embeds = []

    if new_count == 0:
        # No new books, just update notification
        embeds.append(
            {
                "title": "📚 Audiobook Catalog Updated",
                "description": f"Catalog refreshed with **{total_count}** books.",
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
        description = f"**{new_count}** new book{'s' if new_count != 1 else ''} added to the library!"
        if new_count > len(books):
            description += f"\n\n*Showing first {len(books)} books*"

        embeds.append(
            {
                "title": "📚 New Books Added!",
                "description": description,
                "color": 3066993,  # Green
                "fields": [
                    {
                        "name": "📊 Library Stats",
                        "value": f"**{total_count}** total books\n**{new_count}** new additions",
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

            book_embed = {
                "title": title[:256],  # Discord title limit
                "description": book_desc[:4096],  # Discord description limit
                "color": 5814783,  # Blue
            }
            
            # Only add fields if there are any
            if fields:
                book_embed["fields"] = fields

            # Add cover image if available
            if cover:
                # Convert relative path to full URL (URL-encode spaces and special chars)
                from urllib.parse import quote
                cover_encoded = quote(cover.lstrip('/'), safe='/')
                cover_url = f"{site_url.rstrip('/')}/{cover_encoded}"
                book_embed["thumbnail"] = {"url": cover_url}

            embeds.append(book_embed)

    return embeds


def send_notification(webhook_url, embeds):
    """Send notification to Discord webhook."""
    payload = {"embeds": embeds}

    try:
        response = requests.post(webhook_url, json=payload, headers={"Content-Type": "application/json"})
        response.raise_for_status()
        print("✓ Discord notification sent successfully")
        return True
    except requests.exceptions.RequestException as e:
        print(f"✗ First attempt failed: {e}")
        if hasattr(e, "response") and e.response is not None:
            print(f"Response: {e.response.text}")

        # Retry without thumbnails (common cause of 400 errors)
        print("  Retrying without cover images...")
        for embed in embeds:
            embed.pop("thumbnail", None)
        payload = {"embeds": embeds}
        try:
            response = requests.post(webhook_url, json=payload, headers={"Content-Type": "application/json"})
            response.raise_for_status()
            print("✓ Discord notification sent (without covers)")
            return True
        except requests.exceptions.RequestException as e2:
            print(f"✗ Retry also failed: {e2}")
            if hasattr(e2, "response") and e2.response is not None:
                print(f"Response: {e2.response.text}")
            return False


def main():
    # Get environment variables
    webhook_url = os.environ.get("DISCORD_WEBHOOK")
    site_url = os.environ.get("SITE_URL", "https://skymitch9.github.io/audiobook_catalog/")

    if not webhook_url:
        print("DISCORD_WEBHOOK not set, skipping notification")
        sys.exit(0)

    # Load new books data
    new_books_file = Path("new_books.json")
    if not new_books_file.exists():
        print("No new_books.json found, sending generic update notification")
        new_books_data = {"new_count": 0, "total_count": 0, "books": []}
    else:
        with open(new_books_file, "r", encoding="utf-8") as f:
            new_books_data = json.load(f)

    # Create embeds
    embeds = create_embed(new_books_data, site_url)

    # Send notification
    success = send_notification(webhook_url, embeds)

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
