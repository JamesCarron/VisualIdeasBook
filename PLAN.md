# Beehiiv Archive Scraper + PDF Generator — Implementation Plan

## Overview

Local browser-based tool. Scrapes the Milani Creative newsletter archive, extracts images + text from each post's themed sections, deduplicates by image, and generates a B5 PDF — one image per page with associated text below. NiceGUI provides a reactive Python-only browser dashboard for control and preview.

---

## Site Reconnaissance Findings

Investigated `idea-milanicreative.beehiiv.com` directly. Findings drove the architecture below.

| Resource | Status | Notes |
| --- | --- | --- |
| `/sitemap.xml` | ✅ Found | 106 URLs, mostly `/p/*` posts. **Single fetch returns everything.** |
| `/feed` (RSS) | ❌ 404 | No RSS feed exists |
| `/archive` | ✅ Server-rendered HTML | Has paginated post cards (1...8). Backup only. |
| Post pages | ✅ Server-rendered HTML | h3 headings + plain HTML. No JS needed. |

**Implications:**

- **No browser automation required.** Drop Playwright entirely. Use `httpx` + `beautifulsoup4`.
- **Sitemap is the source of truth** for post URL discovery.
- Sections are `<h3>` elements with emoji prefix (e.g., `🤔 INTERESTING`, `📐 DESIGN`).
- Images are wrapped in `<a>` tags pointing to LinkedIn — must be unwrapped during extraction.
- Date format on posts: `Apr 29, 2026` — needs parsing to `YYYY-MM-DD`.

---

## Architecture

### Stage 1: URL Discovery

- `GET /sitemap.xml`
- Parse XML, filter `<loc>` elements matching `/p/*` pattern
- Persist URL list to checkpoint store
- ~50–80 post URLs expected (sitemap has 106 total, includes nav/forms/etc.)

### Stage 2: Post Extraction

For each post URL not yet scraped:

1. `GET` the post HTML
2. Parse with BeautifulSoup (`lxml` backend for speed)
3. Extract:
   - Post title (from `<h1>` or `<title>`)
   - Post date (parse `Apr 29, 2026` format → `YYYY-MM-DD`)
4. Locate `<h3>` section headers matching the allowlist:
   - `INTERESTING`, `DESIGN`, `ENCHANTING`, `ANALOGY`
   - Explicitly **skip** `WHAT I'M READING NOW` (book covers, not design diagrams)
5. For each allowed section, find all images + adjacent text blocks
6. Build one `ImageEntry` per image (per-image text only — first image gets section intro)
7. Strip `<a>` tags from text HTML while preserving inner text and other formatting (`<b>`, `<em>`, `<p>`, etc.)
8. Download image to `data/images/`, compute perceptual hash

### Stage 3: Data Model

```python
@dataclass
class ImageEntry:
    """One image + associated text from a post section."""
    post_url: str            # Source post URL (key for resume logic)
    post_title: str
    post_date: str           # YYYY-MM-DD
    section: str             # "INTERESTING" | "DESIGN" | "ENCHANTING" | "ANALOGY"
    image_url: str           # Original full-size image URL
    image_path: str          # Relative path in data/images/
    image_hash: str          # Perceptual hash (phash) string for dedup
    text: str                # HTML fragment, hyperlinks stripped
    fetch_failed: bool = False
    fetch_error: str = ""
```

**Persistence:** `data/entries.json` (list of entries) + `data/images/` (cache)

- Resumable: skip post URLs already represented in the store
- Failed image fetches are flagged but don't halt the run

### Stage 4: Deduplication

- Use `imagehash.phash` (perceptual hash) — tolerant of resize/compression
- Within configurable Hamming distance threshold (default 4), entries are duplicates
- When duplicates found, keep entry with **earliest `post_date`** — discard others
- Run as a separate pass after scraping completes (or on-demand from UI)

### Stage 5: PDF Generation

- **Library:** ReportLab (`Platypus` for flow layout)
- **Page size:** B5 (176 × 250 mm)
- **Layout per page:**
  - Image fills upper portion (scaled to fit width, aspect ratio preserved, max ~60% page height)
  - Section heading below image
  - HTML text (rendered as ReportLab `Paragraph` — supports `<b>`, `<em>`, `<p>`)
  - Post date + source post URL as footer (small font)
- **Page order:** Chronological by `post_date` (oldest first), then by section order within a post
- **Output:** `output/archive.pdf`

---

## Frontend: NiceGUI Dashboard

Pure Python. No HTML, CSS, or JavaScript files. Reactive bindings replace polling.

### UI Layout

```
┌─────────────────────────────────────────────────┐
│  Visual Ideas Archive Tool                      │
├─────────────────────────────────────────────────┤
│  [Start Scrape] [Pause] [Resume]  Status: idle  │
│  Progress: ████████░░░░░░░░  24 / 67 posts      │
├─────────────────────────────────────────────────┤
│  Log:                                            │
│  ┌─────────────────────────────────────────┐    │
│  │ 12:01:34 Discovered 67 post URLs        │    │
│  │ 12:01:36 Scraped post 1: 4 images       │    │
│  │ ...                                     │    │
│  └─────────────────────────────────────────┘    │
├─────────────────────────────────────────────────┤
│  Entries (124 total, 8 duplicates):             │
│  ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐           │
│  │ img1 │ │ img2 │ │ img3 │ │ img4 │  ...      │
│  └──────┘ └──────┘ └──────┘ └──────┘           │
├─────────────────────────────────────────────────┤
│  [Generate PDF]      Output: archive.pdf        │
└─────────────────────────────────────────────────┘
```

### Controls

- **Start Scrape** — runs Stage 1 + 2 in background thread
- **Pause** — signals scraper to stop after current post finishes
- **Resume** — continues from checkpoint (skips already-scraped URLs)
- **Generate PDF** — runs Stage 4 (dedup) + Stage 5, shows download link when ready

### Reactivity

NiceGUI bindings tie UI elements directly to `ScraperState` fields. No polling:

```python
ui.linear_progress().bind_value_from(state, 'progress')
ui.label().bind_text_from(state, 'current_message')
```

---

## Concurrency & State

`state.py` defines a single `ScraperState` singleton:

```python
@dataclass
class ScraperState:
    status: str = "idle"              # "idle" | "scraping" | "paused" | "done" | "error"
    total: int = 0
    completed: int = 0
    current_message: str = ""
    pause_event: threading.Event = field(default_factory=threading.Event)
    log_lines: list[str] = field(default_factory=list)

    @property
    def progress(self) -> float:
        return self.completed / self.total if self.total else 0.0
```

- **Pause signal**: scraper checks `pause_event.is_set()` between posts
- **Single-job guard**: "Start" button disabled when `status != "idle"`
- **Thread-safety**: only the scraper thread writes; UI thread reads. No locks needed for primitive fields.

---

## Project Structure

```
c:\GitHub\VisualIdeas\
├── requirements.txt
├── models.py        # ImageEntry dataclass + EntryStore (JSON checkpoint)
├── archive.py       # Stage 1: sitemap.xml → post URL list
├── extractor.py     # Stage 2: post HTML → ImageEntry list
├── dedup.py         # Stage 4: perceptual hash dedup
├── pdf.py           # Stage 5: ReportLab B5 generator
├── state.py         # ScraperState + threading primitives
├── app.py           # NiceGUI dashboard (entry point)
├── data/
│   ├── entries.json # Checkpoint (resumable)
│   ├── images/      # Cached image files (md5(url) filenames)
│   └── scraper.log  # Timestamped log
└── output/
    └── archive.pdf
```

---

## Libraries & Tech Stack

| Purpose | Library |
| --- | --- |
| Web UI | `nicegui` |
| HTTP client | `httpx` |
| HTML parsing | `beautifulsoup4` + `lxml` |
| Image handling | `Pillow` |
| Image dedup | `imagehash` |
| PDF output | `reportlab` |

**Dropped from earlier plan:** `playwright`, `click`, `flask`, `requests` (replaced by httpx).

---

## Key Design Decisions

1. **Sitemap over scraping** — `/sitemap.xml` gives all post URLs in one fetch, eliminating Playwright and pagination logic.
2. **`httpx` over `requests`** — modern, supports HTTP/2, same sync API, future-proof for async if needed.
3. **NiceGUI over Flask + JS** — single-language Python, reactive bindings, no polling, no HTML/CSS/JS files.
4. **Section allowlist** — only `INTERESTING`, `DESIGN`, `ENCHANTING`, `ANALOGY`. `WHAT I'M READING NOW` skipped for visual consistency.
5. **One PDF page per image** — multi-image sections (e.g., Design with 2 images) yield multiple pages.
6. **Per-image text only** — section intro stays with first image's entry; later images carry only their own captions.
7. **Raw HTML preserved, hyperlinks stripped** — ReportLab's `Paragraph` renders `<b>`/`<em>`/`<p>` natively; `<a>` tags unwrapped to plain text.
8. **Perceptual hashing** — `imagehash.phash` with Hamming distance threshold for fuzzy dedup, earliest post_date wins.
9. **B5 page size** — fits the image-dominant layout well.
10. **Resumable by JSON checkpoint** — interrupting and re-running is safe; skips URLs already in the store.
11. **Failed fetches recorded, not fatal** — entries with `fetch_failed=True` are logged and skipped in PDF gen.
12. **Image cache filenames: `md5(image_url).<ext>`** — deterministic, no collisions, no download needed to derive name.
13. **Polite scraping** — 0.5s delay between post requests, custom User-Agent header.

---

## Workflow

```bash
# Install dependencies
pip install -r requirements.txt

# Launch dashboard (auto-opens browser)
python app.py
```

Browser dashboard:

1. **Start Scrape** → fetches sitemap, then extracts each post
2. **Watch progress** → live bar + log
3. **Pause / Resume** → safe to interrupt at any time
4. **Generate PDF** → dedup + B5 PDF generation, download link appears

**Output files:**

- `data/entries.json` — checkpoint
- `data/images/*` — cached images
- `data/scraper.log` — timestamped log
- `output/archive.pdf` — final PDF

---

## Implementation Tasks

- [x] Investigate site (sitemap exists, server-rendered, sections are h3)
- [ ] Update `requirements.txt` (drop playwright/click/flask/requests, add nicegui/httpx/lxml)
- [ ] Rewrite `models.py` (ImageEntry + EntryStore matching new schema)
- [ ] Write `state.py` (ScraperState dataclass + pause event)
- [ ] Write `archive.py` (sitemap.xml → list of `/p/*` URLs)
- [ ] Write `extractor.py` (post HTML → ImageEntry list, section allowlist, hyperlink stripping, image download, phash)
- [ ] Write `dedup.py` (perceptual hash dedup, earliest-date wins)
- [ ] Write `pdf.py` (B5 ReportLab generator, image + section + HTML text + footer)
- [ ] Write `app.py` (NiceGUI dashboard with reactive bindings + threading)
- [ ] Test end-to-end on a few posts, then full archive

---

## Open Questions for Later

- Should the dedup threshold (Hamming distance) be configurable in the UI?
- Should the PDF have a cover page / table of contents?
- Should section heading colors/icons match the emoji theme (🤔 📐 🔮 🧠)?

These can be revisited after the MVP is working.
