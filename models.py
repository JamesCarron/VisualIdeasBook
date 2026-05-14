from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, fields
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
    ignored: bool = False        # exclude from PDF output
    done: bool = False           # user has finished reviewing this entry
    text_original: str = ""      # snapshot of `text` taken on first edit


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
            "entries":   [ImageEntry, ...],
            "year_checksums": {"2024": "abc123...", ...}  # optional
        }
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._processed: dict[str, PostRecord] = {}
        self._entries: list[ImageEntry] = []
        self._year_checksums: dict[str, str] = {}
        if path.exists():
            self._load()

    def _load(self) -> None:
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        post_field_names = {f.name for f in fields(PostRecord)}
        for rec in raw.get("processed", []):
            pr = PostRecord(**{k: v for k, v in rec.items() if k in post_field_names})
            self._processed[pr.url] = pr
        entry_field_names = {f.name for f in fields(ImageEntry)}
        self._entries = [
            ImageEntry(**{k: v for k, v in e.items() if k in entry_field_names})
            for e in raw.get("entries", [])
        ]
        self._year_checksums = raw.get("year_checksums", {})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "processed": [asdict(p) for p in self._processed.values()],
            "entries": [asdict(e) for e in self._entries],
            "year_checksums": self._year_checksums,
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

    def get_by_id(self, entry_id: str) -> ImageEntry | None:
        for e in self._entries:
            if entry_key(e) == entry_id:
                return e
        return None

    def update_entry(self, entry_id: str, **changes) -> ImageEntry | None:
        """Apply field changes to the entry with matching id and persist.

        ``text`` is special-cased: the first time it changes, the prior
        value is snapshotted into ``text_original`` (only if that field
        is still empty).
        """
        entry = self.get_by_id(entry_id)
        if entry is None:
            return None
        allowed = {f.name for f in fields(ImageEntry)}
        for key, value in changes.items():
            if key not in allowed:
                continue
            if key == "text" and value != entry.text and not entry.text_original:
                entry.text_original = entry.text
            setattr(entry, key, value)
        self.save()
        return entry

    def set_year_checksum(self, year: str, checksum: str) -> None:
        """Store a checksum for the given year's entries."""
        self._year_checksums[year] = checksum
        self.save()

    def get_year_checksum(self, year: str) -> str | None:
        """Retrieve the stored checksum for a year, or None if not set."""
        return self._year_checksums.get(year)

    def stats(self) -> dict:
        return {
            "posts_processed": len(self._processed),
            "posts_failed": sum(1 for p in self._processed.values() if p.failed),
            "entries_total": len(self._entries),
            "entries_failed": sum(1 for e in self._entries if e.fetch_failed),
            "entries_ignored": sum(1 for e in self._entries if e.ignored),
            "entries_done": sum(1 for e in self._entries if e.done),
        }


def entry_key(entry: ImageEntry) -> str:
    """Stable id derived from (post_url, section, image_path).

    Used to round-trip an entry through URL/state without exposing the
    full path. md5 keeps the id short and URL-safe.
    """
    raw = f"{entry.post_url}|{entry.section}|{entry.image_path}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()
