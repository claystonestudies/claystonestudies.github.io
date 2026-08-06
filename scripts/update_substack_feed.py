#!/usr/bin/env python3
"""Maintain the Substack archive catalog and deterministic featured snapshot."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit


FEED_URL = "https://thinkingatcs.substack.com/feed"
ARCHIVE_URL = "https://thinkingatcs.substack.com/api/v1/archive"
ALLOWED_HOST = "thinkingatcs.substack.com"
MAX_RESPONSE_BYTES = 8_000_000
MAX_EXCERPT_LENGTH = 240
FEATURED_COUNT = 6
ROTATION_DAYS = 3
ARCHIVE_PAGE_SIZE = 50
ARCHIVE_PAGE_DELAY_SECONDS = 0.5
MAX_ARCHIVE_PAGES = 100
CONTENT_TAG = "{http://purl.org/rss/1.0/modules/content/}encoded"


class PlainTextExtractor(HTMLParser):
    """Reduce source HTML to text; the site inserts excerpts with textContent."""

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


def normalize_datetime(value: datetime) -> tuple[str, str, str]:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    published = value.astimezone(timezone.utc)
    display = f"{published.strftime('%B')} {published.day}, {published.year}"
    timestamp = published.isoformat().replace("+00:00", "Z")
    return timestamp, published.date().isoformat(), display


def parse_rss_datetime(value: str) -> tuple[str, str, str]:
    return normalize_datetime(parsedate_to_datetime(value))


def parse_iso_datetime(value: str) -> tuple[str, str, str]:
    normalized = value.strip().replace("Z", "+00:00")
    return normalize_datetime(datetime.fromisoformat(normalized))


def feed_timestamp(channel: ET.Element) -> str:
    raw_value = (channel.findtext("lastBuildDate") or "").strip()
    if not raw_value:
        raise ValueError("RSS feed has no lastBuildDate")
    timestamp, _, _ = parse_rss_datetime(raw_value)
    return timestamp


def parse_feed(feed_bytes: bytes) -> tuple[str, list[dict[str, str]]]:
    root = ET.fromstring(feed_bytes)
    channel = root.find("channel")
    if channel is None:
        raise ValueError("RSS feed has no channel")

    posts: list[dict[str, str]] = []
    for item in channel.findall("item"):
        try:
            title = plain_text(item.findtext("title") or "")
            url = clean_post_url(item.findtext("link") or "")
            published_at, published_date, date_display = parse_rss_datetime(item.findtext("pubDate") or "")
            source = item.findtext(CONTENT_TAG) or item.findtext("description") or ""
            excerpt = truncate_at_word(plain_text(source))
        except (ET.ParseError, TypeError, ValueError, OverflowError):
            continue

        if title and excerpt:
            posts.append(
                {
                    "title": title[:180],
                    "published_at": published_at,
                    "date": published_date,
                    "date_display": date_display,
                    "excerpt": excerpt,
                    "url": url,
                }
            )

    if not posts:
        raise ValueError("RSS feed contained no valid posts")
    return feed_timestamp(channel), posts


def parse_archive_post(item: object) -> dict[str, str] | None:
    if not isinstance(item, dict):
        return None
    try:
        title = plain_text(str(item.get("title") or ""))
        url = clean_post_url(str(item.get("canonical_url") or ""))
        published_at, published_date, date_display = parse_iso_datetime(str(item.get("post_date") or ""))
        source = next(
            (
                str(item.get(field) or "")
                for field in ("subtitle", "truncated_body_text", "description")
                if plain_text(str(item.get(field) or ""))
            ),
            "",
        )
        excerpt = truncate_at_word(plain_text(source))
    except (TypeError, ValueError, OverflowError):
        return None
    if not title or not excerpt:
        return None
    return {
        "title": title[:180],
        "published_at": published_at,
        "date": published_date,
        "date_display": date_display,
        "excerpt": excerpt,
        "url": url,
    }


def validated_request(url: str, *, timeout: int = 30) -> bytes:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != ALLOWED_HOST:
        raise ValueError("Request URL must use the approved HTTPS Substack host")

    request = urllib.request.Request(url, headers={"User-Agent": "ClaystoneStudiesArchiveUpdater/2.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        final_url = urlsplit(response.geturl())
        if final_url.scheme != "https" or final_url.hostname != ALLOWED_HOST:
            raise ValueError("Substack redirected to an unexpected host")
        content = response.read(MAX_RESPONSE_BYTES + 1)

    if len(content) > MAX_RESPONSE_BYTES:
        raise ValueError("Substack response exceeded the size limit")
    return content


def fetch_with_retries(url: str, attempts: int = 3) -> bytes:
    for attempt in range(attempts):
        try:
            return validated_request(url)
        except urllib.error.HTTPError as error:
            if error.code not in {429, 500, 502, 503, 504} or attempt + 1 == attempts:
                raise
            retry_after = error.headers.get("Retry-After", "")
            delay = float(retry_after) if retry_after.isdigit() else float(2**attempt)
        except (TimeoutError, urllib.error.URLError):
            if attempt + 1 == attempts:
                raise
            delay = float(2**attempt)
        time.sleep(min(delay, 30.0))
    raise RuntimeError("Substack request retries were exhausted")


def fetch_archive() -> list[dict[str, str]]:
    """Fetch a complete archive snapshot before returning any records."""
    posts: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    offset = 0

    for page_number in range(MAX_ARCHIVE_PAGES):
        query = urllib.parse.urlencode(
            {"sort": "new", "search": "", "offset": offset, "limit": ARCHIVE_PAGE_SIZE}
        )
        payload = json.loads(fetch_with_retries(f"{ARCHIVE_URL}?{query}"))
        if not isinstance(payload, list):
            raise ValueError("Substack archive returned an unexpected format")

        for item in payload:
            post = parse_archive_post(item)
            if post and post["url"] not in seen_urls:
                seen_urls.add(post["url"])
                posts.append(post)

        if len(payload) < ARCHIVE_PAGE_SIZE:
            break
        offset += len(payload)
        if page_number + 1 < MAX_ARCHIVE_PAGES:
            time.sleep(ARCHIVE_PAGE_DELAY_SECONDS)
    else:
        raise ValueError("Substack archive exceeded the pagination safety limit")

    if not posts:
        raise ValueError("Substack archive contained no valid posts")
    return posts


def load_catalog(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"posts": []}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("posts"), list):
        raise ValueError(f"Catalog has an unexpected format: {path}")
    return payload


def merge_posts(*collections: list[dict[str, str]]) -> list[dict[str, str]]:
    merged: dict[str, dict[str, str]] = {}
    for collection in collections:
        for post in collection:
            try:
                url = clean_post_url(str(post["url"]))
            except (KeyError, TypeError, ValueError):
                continue
            merged[url] = {key: str(value) for key, value in post.items() if key in {
                "title", "published_at", "date", "date_display", "excerpt", "url"
            }}
            merged[url]["url"] = url
    return sorted(merged.values(), key=lambda post: (post.get("published_at", ""), post["url"]), reverse=True)


def bucket_for(day: date) -> dict[str, object]:
    epoch = date(1970, 1, 1)
    index = (day - epoch).days // ROTATION_DAYS
    starts_at = epoch + timedelta(days=index * ROTATION_DAYS)
    return {
        "index": index,
        "starts_at": starts_at.isoformat(),
        "ends_at": (starts_at + timedelta(days=ROTATION_DAYS - 1)).isoformat(),
    }


def select_featured(
    posts: list[dict[str, str]], day: date, previous: dict[str, object] | None = None
) -> tuple[dict[str, object], list[dict[str, str]]]:
    bucket = bucket_for(day)
    by_url = {post["url"]: post for post in posts}

    if previous and previous.get("selection_bucket") == bucket:
        previous_posts = previous.get("posts")
        if isinstance(previous_posts, list):
            urls = [post.get("url") for post in previous_posts if isinstance(post, dict)]
            if len(urls) == min(FEATURED_COUNT, len(posts)) and len(set(urls)) == len(urls) and all(url in by_url for url in urls):
                return bucket, [by_url[url] for url in urls]

    bucket_seed = str(bucket["index"])
    ranked = sorted(
        posts,
        key=lambda post: (hashlib.sha256(f"{bucket_seed}:{post['url']}".encode()).hexdigest(), post["url"]),
    )
    return bucket, ranked[:FEATURED_COUNT]


def load_previous_snapshot(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def write_json(output: Path, payload: dict[str, object]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=output.parent, delete=False) as handle:
        handle.write(rendered)
        temporary_path = Path(handle.name)
    os.replace(temporary_path, output)


def warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--feed-url", default=FEED_URL)
    parser.add_argument("--input", type=Path, help="Read a local RSS fixture instead of using the network")
    parser.add_argument("--catalog", type=Path, default=Path("data/substack-catalog.json"))
    parser.add_argument("--output", type=Path, default=Path("data/substack-posts.json"))
    parser.add_argument("--import-archive", action="store_true", help="Import the complete public archive")
    parser.add_argument("--offline", action="store_true", help="Select from the saved catalog without network access")
    parser.add_argument("--as-of", type=date.fromisoformat, default=datetime.now(timezone.utc).date())
    args = parser.parse_args()

    catalog_payload = load_catalog(args.catalog)
    existing_posts = [post for post in catalog_payload["posts"] if isinstance(post, dict)]
    archive_posts: list[dict[str, str]] = []
    feed_posts: list[dict[str, str]] = []
    feed_updated_at = str(catalog_payload.get("feed_updated_at") or "")

    if args.import_archive and not args.offline:
        try:
            archive_posts = fetch_archive()
        except (OSError, ValueError, json.JSONDecodeError) as error:
            warn(f"archive import failed; retaining the saved catalog ({error})")

    if not args.offline:
        try:
            feed_bytes = args.input.read_bytes() if args.input else fetch_with_retries(args.feed_url)
            feed_updated_at, feed_posts = parse_feed(feed_bytes)
        except (OSError, ValueError, ET.ParseError, urllib.error.URLError) as error:
            warn(f"RSS update failed; retaining the saved catalog ({error})")

    posts = merge_posts(existing_posts, archive_posts, feed_posts)
    if not posts:
        raise SystemExit("No valid saved or fetched Substack posts were available")

    catalog = {
        "archive_source": "https://thinkingatcs.substack.com/archive",
        "archive_api": ARCHIVE_URL,
        "rss_source": FEED_URL,
        "feed_updated_at": feed_updated_at,
        "post_count": len(posts),
        "posts": posts,
    }
    previous = load_previous_snapshot(args.output)
    bucket, featured = select_featured(posts, args.as_of, previous)
    snapshot = {
        "source": "data/substack-catalog.json",
        "selection_method": "sha256 URL ranking within a three-day UTC bucket",
        "selection_bucket": bucket,
        "posts": featured,
    }

    write_json(args.catalog, catalog)
    write_json(args.output, snapshot)
    print(f"Catalog: {len(posts)} posts; featured: {len(featured)}; bucket: {bucket['starts_at']} to {bucket['ends_at']}")


if __name__ == "__main__":
    main()
