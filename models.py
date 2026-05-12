from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path


@dataclass
class ImageEntry:
    """One image + associated text from a post section.

    Multiple entries from the same (post_url, section) constitute one
    logical "section item" and are rendered together on one PDF page.
    """
    post_url: str
    post_title: str = ""
    post_date: str = ""          # YYYY-MM-DD
    section: str = ""            # INTERESTING | DESIGN | ENCHANTING | ANALOGY
    section_intro: str = ""      # HTML; text before the section's first image.
                                 # Broadcast to all entries in the section so
                                 # dedup can drop the first image safely.
    image_url: str = ""
    image_path: str = ""         # absolute path on disk
    image_hash: str = ""         # perceptual hash (hex string)
    text: str = ""               # HTML caption shown below this image
    fetch_failed: bool = False
    fetch_error: str = ""


@dataclass
class PostRecord:
    """Per-post processing status. Persisted alongside entries."""
    url: str
    scraped_at: str = ""
    failed: bool = False
    error: str = ""
    entry_count: int = 0


class EntryStore:
    """JSON-backed store of ImageEntry records + per-post processing status.

    Schema on disk:
        {
            "processed": [PostRecord, ...],
            "entries":   [ImageEntry, ...]
        }
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._processed: dict[str, PostRecord] = {}
        self._entries: list[ImageEntry] = []
        if path.exists():
            self._load()

    def _load(self) -> None:
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        for rec in raw.get("processed", []):
            pr = PostRecord(**rec)
            self._processed[pr.url] = pr
        self._entries = [ImageEntry(**e) for e in raw.get("entries", [])]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "processed": [asdict(p) for p in self._processed.values()],
            "entries": [asdict(e) for e in self._entries],
        }
        self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def processed_urls(self) -> set[str]:
        return set(self._processed.keys())

    def is_processed(self, url: str) -> bool:
        return url in self._processed

    def add_post(self, url: str, entries: list[ImageEntry]) -> None:
        # Replace any prior entries for this post (idempotent re-scrape)
        self._entries = [e for e in self._entries if e.post_url != url]
        self._entries.extend(entries)
        self._processed[url] = PostRecord(
            url=url,
            scraped_at=datetime.now().isoformat(timespec="seconds"),
            failed=False,
            error="",
            entry_count=len(entries),
        )
        self.save()

    def mark_failed(self, url: str, error: str) -> None:
        self._processed[url] = PostRecord(
            url=url,
            scraped_at=datetime.now().isoformat(timespec="seconds"),
            failed=True,
            error=error,
            entry_count=0,
        )
        self.save()

    def all_entries(self) -> list[ImageEntry]:
        return list(self._entries)

    def stats(self) -> dict:
        return {
            "posts_processed": len(self._processed),
            "posts_failed": sum(1 for p in self._processed.values() if p.failed),
            "entries_total": len(self._entries),
            "entries_failed": sum(1 for e in self._entries if e.fetch_failed),
        }
