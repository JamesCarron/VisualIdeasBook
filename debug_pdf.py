"""Single-URL PDF visualisation harness.

Runs the extractor on one post URL and renders its entries to a PDF
without touching the JSON store or dedup pass. Useful for sanity-checking
PDF layout against a known post.

Usage:
    python debug_pdf.py [url]

Defaults to the post we used for extraction debugging.
"""

from __future__ import annotations

import sys
from pathlib import Path

from extractor import extract_post
from pdf import generate_pdf

DEFAULT_URL = "https://idea-milanicreative.beehiiv.com/p/the-2-types-of-talent"
DEBUG_DIR = Path("data") / "debug"
PDF_PATH = DEBUG_DIR / "single_url.pdf"
IMAGES_DIR = DEBUG_DIR / "images"


def banner(msg: str) -> None:
    print("\n" + "=" * 70)
    print(msg)
    print("=" * 70)


def main(url: str) -> None:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)

    banner(f"Extracting: {url}")
    entries = extract_post(url, IMAGES_DIR)
    print(f"Got {len(entries)} entries")
    for i, e in enumerate(entries, 1):
        flag = "FAIL" if e.fetch_failed else "ok"
        print(
            f"  [{i}] [{flag}] {e.section:11s} "
            f"img={Path(e.image_path).name if e.image_path else '-':40s} "
            f"text={len(e.text):4d} chars"
        )
        if e.fetch_failed:
            print(f"      error: {e.fetch_error}")

    renderable = [e for e in entries if e.image_path and not e.fetch_failed]
    banner(f"Rendering {len(renderable)} entries to PDF")

    if not renderable:
        print("Nothing to render. Stop.")
        return

    out = generate_pdf(renderable, PDF_PATH)
    size_kb = out.stat().st_size / 1024
    print(f"PDF written: {out}  ({size_kb:.1f} KB)")
    print(f"Pages: {len(renderable)} (one per entry)")


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    main(target)
