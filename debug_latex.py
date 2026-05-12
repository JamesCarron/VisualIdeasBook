"""Single-URL LaTeX/PDF visualisation harness.

Runs the extractor on one post URL, groups the resulting ImageEntry
records by section, and renders them to a PDF via xelatex.

Usage:
    python debug_latex.py [url]
"""

from __future__ import annotations

import logging
import sys
from collections import defaultdict
from pathlib import Path

from extractor import extract_post
from latex_gen import generate_pdf

DEFAULT_URL = "https://idea-milanicreative.beehiiv.com/p/the-2-types-of-talent"
DEBUG_DIR = Path("data") / "debug"
PDF_PATH = DEBUG_DIR / "single_url.pdf"
IMAGES_DIR = DEBUG_DIR / "images"


def banner(msg: str) -> None:
    print("\n" + "=" * 70)
    print(msg)
    print("=" * 70)


def main(url: str) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)

    banner(f"Extracting: {url}")
    entries = extract_post(url, IMAGES_DIR)
    print(f"Got {len(entries)} entries\n")

    # Show per-entry summary, with a section_intro flag
    for i, e in enumerate(entries, 1):
        flag = "FAIL" if e.fetch_failed else "ok"
        intro_marker = "INTRO" if e.section_intro else "-----"
        print(
            f"  [{i}] [{flag}] {e.section:11s} [{intro_marker}] "
            f"caption={len(e.text):4d}c  intro={len(e.section_intro):4d}c  "
            f"img={Path(e.image_path).name if e.image_path else '-'}"
        )
        if e.fetch_failed:
            print(f"      error: {e.fetch_error}")

    # Show the grouping the LaTeX generator will produce
    banner("Grouping by (post_url, section)")
    groups: dict[tuple[str, str], list] = defaultdict(list)
    for e in entries:
        if e.image_path and not e.fetch_failed:
            groups[(e.post_url, e.section)].append(e)
    for (url_, section), group in groups.items():
        intro = next((e.section_intro for e in group if e.section_intro), "")
        print(
            f"  page: {section:11s}  images={len(group)}  "
            f"intro_chars={len(intro)}"
        )

    renderable_count = sum(
        1 for e in entries if e.image_path and not e.fetch_failed
    )
    if renderable_count == 0:
        print("\nNothing to render. Stop.")
        return

    banner(f"Rendering {len(groups)} pages from {renderable_count} entries")
    try:
        out = generate_pdf(entries, PDF_PATH)
    except RuntimeError as exc:
        print(f"\n{exc}")
        print(f"\nThe .tex file is at: {PDF_PATH.with_suffix('.tex')}")
        return

    size_kb = out.stat().st_size / 1024
    print(f"\nPDF written: {out}  ({size_kb:.1f} KB)")
    print(f"Source:     {out.with_suffix('.tex')}")


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    main(target)
