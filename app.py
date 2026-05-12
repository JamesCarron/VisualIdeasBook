from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from nicegui import app, ui

from archive import fetch_post_urls
from dedup import deduplicate
from extractor import extract_post
from latex_gen import generate_pdf
from models import EntryStore
from state import ScraperState

# --- Paths -------------------------------------------------------------

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
IMAGES_DIR = DATA_DIR / "images"
ENTRIES_PATH = DATA_DIR / "entries.json"
LOG_PATH = DATA_DIR / "scraper.log"
OUTPUT_DIR = ROOT / "output"
PDF_PATH = OUTPUT_DIR / "archive.pdf"

DATA_DIR.mkdir(parents=True, exist_ok=True)
IMAGES_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# --- Logging -----------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("visualideas")

# --- Shared state ------------------------------------------------------

state = ScraperState()
store = EntryStore(ENTRIES_PATH)
_scrape_thread: threading.Thread | None = None
POLITE_DELAY_S = 0.5

# --- Background work ---------------------------------------------------


def _record(msg: str) -> None:
    state.log(msg)
    log.info(msg)


def _scrape_worker() -> None:
    try:
        state.status = "scraping"
        _record("Fetching sitemap…")
        urls = fetch_post_urls()
        _record(f"Found {len(urls)} post URLs in sitemap")

        already = store.processed_urls()
        todo = [u for u in urls if u not in already]
        _record(f"{len(todo)} new posts to scrape ({len(already)} already done)")

        state.total = len(todo)
        state.completed = 0

        for i, url in enumerate(todo, start=1):
            if state.pause_requested():
                state.status = "paused"
                _record("Paused after current post")
                return

            _record(f"[{i}/{len(todo)}] Scraping {url}")
            try:
                entries = extract_post(url, IMAGES_DIR)
                store.add_post(url, entries)
                failed = sum(1 for e in entries if e.fetch_failed)
                _record(f"  → {len(entries)} entries ({failed} image failures)")
            except Exception as exc:  # noqa: BLE001
                store.mark_failed(url, f"{type(exc).__name__}: {exc}")
                _record(f"  ERROR: {exc}")

            state.completed = i
            time.sleep(POLITE_DELAY_S)

        state.status = "done"
        _record("Scraping complete")
    except Exception as exc:  # noqa: BLE001
        state.status = "error"
        _record(f"FATAL: {exc}")
        log.exception("Scraper crashed")


def start_scrape() -> None:
    global _scrape_thread
    if _scrape_thread is not None and _scrape_thread.is_alive():
        ui.notify("Already scraping", type="warning")
        return
    state.reset_for_run()
    _scrape_thread = threading.Thread(target=_scrape_worker, daemon=True)
    _scrape_thread.start()
    ui.notify("Scrape started")


def pause_scrape() -> None:
    if _scrape_thread is None or not _scrape_thread.is_alive():
        ui.notify("Nothing to pause", type="warning")
        return
    state.request_pause()
    ui.notify("Pause requested — will stop after current post")


def resume_scrape() -> None:
    global _scrape_thread
    if _scrape_thread is not None and _scrape_thread.is_alive():
        ui.notify("Already scraping", type="warning")
        return
    state.clear_pause()
    start_scrape()


def generate_pdf_action() -> None:
    try:
        _record("Deduplicating…")
        entries = store.all_entries()
        kept, removed = deduplicate(entries)
        _record(f"Kept {len(kept)}; removed {len(removed)} (duplicates/failed)")

        if not kept:
            _record("No entries to render — scrape some posts first")
            ui.notify("No entries to render", type="warning")
            return

        _record("Generating PDF…")
        path = generate_pdf(kept, PDF_PATH)
        state.pdf_path = str(path)
        _record(f"PDF written: {path}")
        ui.notify("PDF ready")
        pdf_view.refresh()
    except Exception as exc:  # noqa: BLE001
        log.exception("PDF generation failed")
        ui.notify(f"PDF failed: {exc}", type="negative")


# --- Static mounts -----------------------------------------------------

app.add_static_files("/cached-images", str(IMAGES_DIR))
app.add_static_files("/pdf", str(OUTPUT_DIR))

# --- UI ----------------------------------------------------------------

ui.label("Visual Ideas Archive Tool").classes("text-2xl font-bold mt-4")
ui.label(
    "Scrapes the Milani Creative newsletter archive and packages it as a B5 PDF."
).classes("text-sm text-gray-500 mb-4")

with ui.card().classes("w-full"):
    ui.label("Scraper").classes("text-lg font-semibold")
    with ui.row():
        ui.button("Start", on_click=start_scrape, icon="play_arrow")
        ui.button("Pause", on_click=pause_scrape, icon="pause")
        ui.button("Resume", on_click=resume_scrape, icon="restart_alt")

    ui.label().bind_text_from(state, "status", lambda s: f"Status: {s}").classes("mt-2")
    progress = ui.linear_progress(show_value=False).props("size=20px")
    progress.bind_value_from(state, "progress")
    ui.label().bind_text_from(
        state,
        "completed",
        lambda _: f"{state.completed} / {state.total} posts",
    )
    ui.label().bind_text_from(state, "current_message").classes("text-xs text-gray-500")

with ui.card().classes("w-full"):
    ui.label("Log").classes("text-lg font-semibold")
    log_view = ui.log(max_lines=300).classes("h-56 w-full font-mono text-xs")

_log_index = {"seen": 0}


def _sync_log() -> None:
    n = len(state.log_lines)
    if n > _log_index["seen"]:
        for line in state.log_lines[_log_index["seen"]:n]:
            log_view.push(line)
        _log_index["seen"] = n


ui.timer(0.5, _sync_log)


@ui.refreshable
def entries_view() -> None:
    stats = store.stats()
    ui.label(
        f"Stored entries: {stats['entries_total']}  •  "
        f"Posts processed: {stats['posts_processed']}  •  "
        f"Posts failed: {stats['posts_failed']}  •  "
        f"Image fetch failures: {stats['entries_failed']}"
    ).classes("text-sm")

    entries = store.all_entries()
    shown = [e for e in entries if e.image_path and not e.fetch_failed][:48]
    if not shown:
        ui.label("(no entries yet — start a scrape)").classes("text-gray-500 italic")
        return

    with ui.grid(columns=6).classes("gap-2 w-full"):
        for entry in shown:
            with ui.element("div").classes("flex flex-col items-center"):
                filename = Path(entry.image_path).name
                ui.image(f"/cached-images/{filename}").classes(
                    "w-24 h-24 object-cover rounded"
                )
                ui.label(f"{entry.section[:8]} · {entry.post_date}").classes(
                    "text-xxs text-gray-600 text-center"
                )


with ui.card().classes("w-full"):
    with ui.row().classes("items-center justify-between w-full"):
        ui.label("Preview").classes("text-lg font-semibold")
        ui.button("Refresh", on_click=entries_view.refresh, icon="refresh").props(
            "flat dense"
        )
    entries_view()


@ui.refreshable
def pdf_view() -> None:
    if state.pdf_path and Path(state.pdf_path).exists():
        ui.label(f"Latest PDF: {state.pdf_path}")
        ui.link("Download", f"/pdf/{Path(state.pdf_path).name}", new_tab=True)
    else:
        ui.label("(PDF not generated yet)").classes("text-gray-500 italic")


with ui.card().classes("w-full mb-8"):
    ui.label("PDF Output").classes("text-lg font-semibold")
    ui.button("Generate PDF", on_click=generate_pdf_action, icon="picture_as_pdf")
    pdf_view()


if __name__ in {"__main__", "__mp_main__"}:
    ui.run(
        title="Visual Ideas Archive",
        port=8080,
        show=True,
        reload=False,
        favicon="📐",
    )
