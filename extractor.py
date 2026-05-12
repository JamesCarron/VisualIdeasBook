from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx
import imagehash
from bs4 import BeautifulSoup, NavigableString, Tag
from PIL import Image

from models import ImageEntry

USER_AGENT = "VisualIdeas/1.0 (Personal Archive Tool)"
ALLOWED_SECTIONS = ("INTERESTING", "DESIGN", "ENCHANTING", "ANALOGY")
ALLOWED_INLINE_TAGS = {"b", "strong", "i", "em", "u", "br"}
ALLOWED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

# Beehiiv post dates render as "Apr 29, 2026". Match month abbr + day + year.
DATE_RE = re.compile(r"\b([A-Z][a-z]{2,9})\s+(\d{1,2}),?\s+(\d{4})\b")


def fetch_post_html(url: str, timeout: float = 30.0) -> str:
    resp = httpx.get(
        url,
        headers={"User-Agent": USER_AGENT},
        timeout=timeout,
        follow_redirects=True,
    )
    resp.raise_for_status()
    return resp.text


def parse_post_title(soup: BeautifulSoup) -> str:
    h1 = soup.find("h1")
    if h1:
        return h1.get_text(strip=True)
    title = soup.find("title")
    return title.get_text(strip=True) if title else ""


def parse_post_date(soup: BeautifulSoup) -> str:
    """Return YYYY-MM-DD or empty string."""
    # 1) Prefer a <time datetime="..."> element if present
    time_el = soup.find("time")
    if isinstance(time_el, Tag):
        dt_attr = time_el.get("datetime")
        if dt_attr:
            iso = dt_attr.rstrip("Z").replace("Z", "")
            try:
                return datetime.fromisoformat(iso).strftime("%Y-%m-%d")
            except ValueError:
                pass

    # 2) Fall back to regex on visible page text
    text = soup.get_text(" ", strip=True)
    match = DATE_RE.search(text)
    if not match:
        return ""
    month_str, day, year = match.groups()
    for fmt in ("%b %d %Y", "%B %d %Y"):
        try:
            return datetime.strptime(f"{month_str} {day} {year}", fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def section_key(h3_text: str) -> str | None:
    """Map a heading like '🤔 INTERESTING' to its allowlist key, or None."""
    upper = h3_text.upper()
    for section in ALLOWED_SECTIONS:
        if section in upper:
            return section
    return None


def _clean_text_html(element: Tag | NavigableString) -> str:
    """Return inner HTML cleaned of <a> tags, attrs, and disallowed tags.

    Preserves inline formatting (b, strong, i, em, u, br) so ReportLab's
    Paragraph can render it. All other tags are unwrapped to plain text.
    """
    if isinstance(element, NavigableString):
        return str(element).strip()

    copy = BeautifulSoup(str(element), "lxml")
    target: Tag = copy.body or copy  # type: ignore[assignment]

    # Unwrap all <a> tags (keep inner text/elements)
    for a in target.find_all("a"):
        a.unwrap()

    # Strip <img> elements from text blocks (they're handled separately)
    for img in target.find_all("img"):
        img.decompose()

    # Whitelist tags; unwrap others
    for tag in list(target.find_all(True)):
        if tag.name in ALLOWED_INLINE_TAGS:
            tag.attrs = {}
        else:
            tag.unwrap()

    html = "".join(str(c) for c in target.children).strip()
    # Collapse runs of whitespace
    html = re.sub(r"\s+", " ", html)
    return html


def extract_section_pairs(
    soup: BeautifulSoup,
) -> list[tuple[str, str, str, str]]:
    """Walk the post body. For each allowed section heading (<h2>), pair
    each image with its own caption and pass along the section intro.

    Beehiiv wraps each section's <h2> in its own container <div>, so the
    section's content lives at the wrapper's level — not as siblings of
    the <h2> itself. We therefore walk the document linearly using
    ``find_all_next`` and stop at the next <h1> or <h2> boundary.

    Pairing rule:
      - Text BEFORE the first image of a section = the section intro
        (rendered below the section title in the PDF). Broadcast to every
        ImageEntry in the section so that dedup can drop the first image
        without losing the intro.
      - Text AFTER image N (up to the next image, or end of section) =
        the caption for image N. Belongs to that image only.

    Returns a list of (section_name, section_intro_html, image_url,
    caption_html) tuples. One tuple per image.
    """
    results: list[tuple[str, str, str, str]] = []

    # All section boundaries in document order. h1 appears once for the
    # post title and again for the site footer — both terminate a section.
    boundaries = soup.find_all(["h1", "h2"])

    for idx, heading in enumerate(boundaries):
        if heading.name != "h2":
            continue
        section = section_key(heading.get_text())
        if not section:
            continue

        next_boundary = boundaries[idx + 1] if idx + 1 < len(boundaries) else None

        # Collect every <img> and <p> between this heading and the next
        # boundary, in document order.
        items: list[tuple[str, Tag | str]] = []
        for el in heading.find_all_next():
            if next_boundary is not None and el is next_boundary:
                break
            if not isinstance(el, Tag):
                continue
            if el.name == "img":
                items.append(("img", el))
            elif el.name == "p":
                # Skip paragraphs whose ancestor chain includes an <a> —
                # Beehiiv wraps share buttons as <a><img/><small><p>
                # Share on LinkedIn</p></small></a>. Article paragraphs
                # may CONTAIN an inline <a>, but are not inside one.
                if el.find_parent("a") is not None:
                    continue
                cleaned = _clean_text_html(el)
                if cleaned:
                    items.append(("text", cleaned))

        if not any(kind == "img" for kind, _ in items):
            continue

        # Text-after pairing: intro is everything before the first image,
        # each image's caption is the text that follows it.
        intro_chunks: list[str] = []
        entries: list[dict] = []
        current: dict | None = None
        for kind, value in items:
            if kind == "img":
                current = {"img": value, "caption_chunks": []}
                entries.append(current)
            else:  # "text"
                if current is None:
                    intro_chunks.append(value)  # type: ignore[arg-type]
                else:
                    current["caption_chunks"].append(value)

        intro_html = "<br/><br/>".join(intro_chunks)

        for entry in entries:
            img_tag: Tag = entry["img"]  # type: ignore[assignment]
            img_url = (img_tag.get("src") or "").strip()
            if not img_url:
                continue
            caption_html = "<br/><br/>".join(entry["caption_chunks"])
            results.append((section, intro_html, img_url, caption_html))

    return results


def _image_filename(url: str) -> str:
    parsed = urlparse(url)
    ext = Path(parsed.path).suffix.lower()
    if ext not in ALLOWED_IMAGE_EXTS:
        ext = ".jpg"
    digest = hashlib.md5(url.encode("utf-8")).hexdigest()
    return f"{digest}{ext}"


def download_image(url: str, images_dir: Path, timeout: float = 30.0) -> tuple[Path, str]:
    """Download image to cache and return (local_path, perceptual_hash_hex)."""
    images_dir.mkdir(parents=True, exist_ok=True)
    local_path = images_dir / _image_filename(url)

    if not local_path.exists():
        resp = httpx.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
            follow_redirects=True,
        )
        resp.raise_for_status()
        local_path.write_bytes(resp.content)

    with Image.open(local_path) as img:
        img.load()  # force decode so phash sees real pixels
        phash = str(imagehash.phash(img))

    return local_path, phash


def extract_post(url: str, images_dir: Path) -> list[ImageEntry]:
    """Fetch a post and return ImageEntry records for its allowed sections.

    Image download failures are recorded on the entry (fetch_failed=True)
    but do not abort the rest of the post.
    """
    html = fetch_post_html(url)
    soup = BeautifulSoup(html, "lxml")

    title = parse_post_title(soup)
    date = parse_post_date(soup)
    pairs = extract_section_pairs(soup)

    entries: list[ImageEntry] = []
    for section, intro_html, img_url, caption_html in pairs:
        entry = ImageEntry(
            post_url=url,
            post_title=title,
            post_date=date,
            section=section,
            section_intro=intro_html,
            image_url=img_url,
            text=caption_html,
        )
        try:
            local_path, phash = download_image(img_url, images_dir)
            entry.image_path = str(local_path)
            entry.image_hash = phash
        except Exception as exc:  # noqa: BLE001 — log every failure, continue
            entry.fetch_failed = True
            entry.fetch_error = f"{type(exc).__name__}: {exc}"
        entries.append(entry)

    return entries
