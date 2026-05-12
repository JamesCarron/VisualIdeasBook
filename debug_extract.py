"""Diagnostic harness for the extractor.

Run this against a single post URL to see exactly what the parser sees.

Usage:
    python debug_extract.py [url]

Defaults to the post the user mentioned (the-2-types-of-talent).
"""

from __future__ import annotations

import sys
from pathlib import Path

from bs4 import BeautifulSoup, Tag

from extractor import (
    ALLOWED_SECTIONS,
    extract_post,
    extract_section_pairs,
    fetch_post_html,
    parse_post_date,
    parse_post_title,
    section_key,
)

DEFAULT_URL = "https://idea-milanicreative.beehiiv.com/p/the-2-types-of-talent"
DEBUG_DIR = Path("data") / "debug"


def banner(msg: str) -> None:
    print("\n" + "=" * 70)
    print(msg)
    print("=" * 70)


def main(url: str) -> None:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)

    banner(f"Fetching: {url}")
    html = fetch_post_html(url)
    print(f"HTML length: {len(html):,} chars")

    # Save raw HTML for offline inspection
    html_path = DEBUG_DIR / "last_post.html"
    html_path.write_text(html, encoding="utf-8")
    print(f"Saved raw HTML -> {html_path}")

    soup = BeautifulSoup(html, "lxml")

    banner("Title + Date")
    print(f"Title: {parse_post_title(soup)!r}")
    print(f"Date:  {parse_post_date(soup)!r}")

    banner("All <h1>, <h2>, <h3>, <h4> headings")
    for tag in soup.find_all(["h1", "h2", "h3", "h4"]):
        text = tag.get_text(" ", strip=True)
        print(f"  <{tag.name}> {text[:100]!r}")

    banner("Section detection (testing each <h2>)")
    headings = soup.find_all("h2")
    print(f"Found {len(headings)} <h2> elements")
    for h in headings:
        text = h.get_text(" ", strip=True)
        key = section_key(text)
        marker = "Y" if key else "."
        print(f"  {marker} {text[:80]!r} -> {key}")

    banner(f"Section allowlist = {ALLOWED_SECTIONS}")

    banner("Document-order walk between h2 boundaries (img + p only)")
    boundaries = soup.find_all(["h1", "h2"])
    for idx, h in enumerate(boundaries):
        if h.name != "h2":
            continue
        key = section_key(h.get_text())
        if not key:
            continue
        next_boundary = boundaries[idx + 1] if idx + 1 < len(boundaries) else None
        print(f"\n--- Section: {key} ({h.get_text(' ', strip=True)!r}) ---")
        count_img = count_p = count_p_in_a = 0
        for el in h.find_all_next():
            if next_boundary is not None and el is next_boundary:
                break
            if not isinstance(el, Tag):
                continue
            if el.name == "img":
                count_img += 1
                src = (el.get("src") or "")[:80]
                print(f"    img: {src}")
            elif el.name == "p":
                if el.find_parent("a") is not None:
                    count_p_in_a += 1
                    continue
                count_p += 1
                text = el.get_text(" ", strip=True)[:100]
                print(f"    p:   {text!r}")
        print(f"    => {count_img} images, {count_p} text paragraphs "
              f"({count_p_in_a} skipped as share-button captions)")

    banner("Trying extract_section_pairs()")
    try:
        pairs = extract_section_pairs(soup)
        print(f"Extracted {len(pairs)} pairs")
        for i, (section, img_url, text) in enumerate(pairs, 1):
            print(f"\n  [{i}] section={section}")
            print(f"      image_url={img_url[:100]}")
            print(f"      text=({len(text)} chars) {text[:200]!r}")
    except Exception as exc:  # noqa: BLE001
        print(f"extract_section_pairs FAILED: {type(exc).__name__}: {exc}")

    banner("Full extract_post() (downloads images, computes hashes)")
    images_dir = DEBUG_DIR / "images"
    try:
        entries = extract_post(url, images_dir)
        print(f"Got {len(entries)} ImageEntry records")
        for i, e in enumerate(entries, 1):
            status = "FAIL" if e.fetch_failed else "ok"
            print(
                f"  [{i}] [{status}] {e.section} "
                f"img={Path(e.image_path).name if e.image_path else '-'} "
                f"hash={e.image_hash or '-'} "
                f"text_chars={len(e.text)}"
            )
            if e.fetch_failed:
                print(f"      error: {e.fetch_error}")
    except Exception as exc:  # noqa: BLE001
        print(f"extract_post FAILED: {type(exc).__name__}: {exc}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    main(target)
