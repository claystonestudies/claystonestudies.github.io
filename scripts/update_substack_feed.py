#!/usr/bin/env python3
"""Create the checked-in Substack card data from the publication's RSS feed."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
from datetime import timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit


FEED_URL = "https://thinkingatcs.substack.com/feed"
ALLOWED_HOST = "thinkingatcs.substack.com"
MAX_FEED_BYTES = 2_000_000
MAX_EXCERPT_LENGTH = 240
POST_COUNT = 6
CONTENT_TAG = "{http://purl.org/rss/1.0/modules/content/}encoded"


class PlainTextExtractor(HTMLParser):
    """Reduce feed HTML to text; the site later inserts it with textContent only."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style"}:
            self.ignored_depth += 1
        elif tag.lower() in {"br", "div", "li", "p"}:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style"} and self.ignored_depth:
            self.ignored_depth -= 1
        elif tag.lower() in {"div", "li", "p"}:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.ignored_depth:
            self.parts.append(data)


def plain_text(value: str) -> str:
    parser = PlainTextExtractor()
    parser.feed(value)
    parser.close()
    text = html.unescape("".join(parser.parts))
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"\s+Read more\s*$", "", text, flags=re.IGNORECASE).strip()


def truncate_at_word(text: str, limit: int = MAX_EXCERPT_LENGTH) -> str:
    if len(text) <= limit:
        return text
    shortened = text[: limit + 1].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return f"{shortened}…"


def clean_post_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme != "https" or parsed.hostname != ALLOWED_HOST or not parsed.path.startswith("/p/"):
        raise ValueError(f"Unexpected post URL: {value!r}")
    return f"https://{ALLOWED_HOST}{parsed.path.rstrip('/')}"


def format_date(value: str) -> tuple[str, str]:
    published = parsedate_to_datetime(value)
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    published = published.astimezone(timezone.utc)
    display = f"{published.strftime('%B')} {published.day}, {published.year}"
    return published.date().isoformat(), display


def feed_timestamp(channel: ET.Element) -> str:
    raw_value = (channel.findtext("lastBuildDate") or "").strip()
    if not raw_value:
        raise ValueError("RSS feed has no lastBuildDate")
    value = parsedate_to_datetime(raw_value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_feed(feed_bytes: bytes) -> dict[str, object]:
    root = ET.fromstring(feed_bytes)
    channel = root.find("channel")
    if channel is None:
        raise ValueError("RSS feed has no channel")

    posts: list[dict[str, str]] = []
    for item in channel.findall("item"):
        try:
            title = plain_text(item.findtext("title") or "")
            url = clean_post_url(item.findtext("link") or "")
            date, date_display = format_date(item.findtext("pubDate") or "")
            source = item.findtext(CONTENT_TAG) or item.findtext("description") or ""
            excerpt = truncate_at_word(plain_text(source))
        except (TypeError, ValueError, OverflowError):
            continue

        if title and excerpt:
            posts.append(
                {
                    "title": title[:180],
                    "date": date,
                    "date_display": date_display,
                    "excerpt": excerpt,
                    "url": url,
                }
            )
        if len(posts) == POST_COUNT:
            break

    if len(posts) != POST_COUNT:
        raise ValueError(f"Expected {POST_COUNT} valid posts, found {len(posts)}")

    return {
        "source": FEED_URL,
        "feed_updated_at": feed_timestamp(channel),
        "posts": posts,
    }


def fetch_feed(url: str) -> bytes:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != ALLOWED_HOST:
        raise ValueError("Feed URL must use the approved HTTPS Substack host")

    request = urllib.request.Request(url, headers={"User-Agent": "ClaystoneStudiesFeedUpdater/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        final_url = urlsplit(response.geturl())
        if final_url.scheme != "https" or final_url.hostname != ALLOWED_HOST:
            raise ValueError("Feed redirected to an unexpected host")
        content = response.read(MAX_FEED_BYTES + 1)

    if len(content) > MAX_FEED_BYTES:
        raise ValueError("RSS response exceeded the size limit")
    return content


def write_json(output: Path, payload: dict[str, object]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=output.parent, delete=False) as handle:
        handle.write(rendered)
        temporary_path = Path(handle.name)
    os.replace(temporary_path, output)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--feed-url", default=FEED_URL)
    parser.add_argument("--input", type=Path, help="Read a local RSS fixture instead of using the network")
    parser.add_argument("--output", type=Path, default=Path("data/substack-posts.json"))
    args = parser.parse_args()

    feed_bytes = args.input.read_bytes() if args.input else fetch_feed(args.feed_url)
    write_json(args.output, parse_feed(feed_bytes))


if __name__ == "__main__":
    main()
