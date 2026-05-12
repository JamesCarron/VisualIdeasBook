from __future__ import annotations

import imagehash

from models import ImageEntry

DEFAULT_THRESHOLD = 4  # Hamming distance — 0 is identical, ~10 is loosely similar


def deduplicate(
    entries: list[ImageEntry],
    threshold: int = DEFAULT_THRESHOLD,
) -> tuple[list[ImageEntry], list[ImageEntry]]:
    """Filter out perceptual-hash duplicates. Earliest post_date wins.

    Entries missing a hash (e.g. failed image fetches) are returned in the
    'removed' list so they don't reach the PDF.

    Returns (kept, removed).
    """
    valid = [e for e in entries if e.image_hash and not e.fetch_failed]
    removed: list[ImageEntry] = [
        e for e in entries if not e.image_hash or e.fetch_failed
    ]

    # Sort by date so the earliest version of any duplicate is kept first
    valid.sort(key=lambda e: (e.post_date or "9999-99-99", e.post_url))

    kept: list[ImageEntry] = []
    kept_hashes: list[imagehash.ImageHash] = []

    for entry in valid:
        try:
            entry_hash = imagehash.hex_to_hash(entry.image_hash)
        except ValueError:
            removed.append(entry)
            continue

        if any((entry_hash - kh) <= threshold for kh in kept_hashes):
            removed.append(entry)
        else:
            kept.append(entry)
            kept_hashes.append(entry_hash)

    return kept, removed
