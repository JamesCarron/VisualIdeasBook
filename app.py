from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from nicegui import app, ui

from archive import fetch_post_urls
from dedup import deduplicate
from extractor import extract_post
from latex_gen import SECTION_ORDER, compute_page_index, generate_pdf
from models import EntryStore, ImageEntry, entry_key
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


# --- Static mounts -----------------------------------------------------

app.add_static_files("/cached-images", str(IMAGES_DIR))
app.add_static_files("/pdf", str(OUTPUT_DIR))


# --- Home page --------------------------------------------------------


@ui.page("/")
def home_page() -> None:
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

        ui.label().bind_text_from(
            state, "status", lambda s: f"Status: {s}"
        ).classes("mt-2")
        progress = ui.linear_progress(show_value=False).props("size=20px")
        progress.bind_value_from(state, "progress")
        ui.label().bind_text_from(
            state,
            "completed",
            lambda _: f"{state.completed} / {state.total} posts",
        )
        ui.label().bind_text_from(state, "current_message").classes(
            "text-xs text-gray-500"
        )

    with ui.card().classes("w-full"):
        ui.label("Log").classes("text-lg font-semibold")
        log_view = ui.log(max_lines=300).classes("h-56 w-full font-mono text-xs")

    log_index = {"seen": 0}

    def sync_log() -> None:
        n = len(state.log_lines)
        if n > log_index["seen"]:
            for line in state.log_lines[log_index["seen"]:n]:
                log_view.push(line)
            log_index["seen"] = n

    ui.timer(0.5, sync_log)

    @ui.refreshable
    def entries_view() -> None:
        stats = store.stats()
        ui.label(
            f"Stored entries: {stats['entries_total']}  •  "
            f"Posts processed: {stats['posts_processed']}  •  "
            f"Posts failed: {stats['posts_failed']}  •  "
            f"Image fetch failures: {stats['entries_failed']}  •  "
            f"Ignored: {stats['entries_ignored']}  •  "
            f"Done: {stats['entries_done']}"
        ).classes("text-sm")

        entries = store.all_entries()
        shown = [e for e in entries if e.image_path and not e.fetch_failed][:48]
        if not shown:
            ui.label("(no entries yet — start a scrape)").classes(
                "text-gray-500 italic"
            )
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
            ui.button(
                "Refresh", on_click=entries_view.refresh, icon="refresh"
            ).props("flat dense")
        entries_view()

    @ui.refreshable
    def pdf_view() -> None:
        if state.pdf_path and Path(state.pdf_path).exists():
            ui.label(f"Latest PDF: {state.pdf_path}")
            ui.link("Download", f"/pdf/{Path(state.pdf_path).name}", new_tab=True)
        else:
            ui.label("(PDF not generated yet)").classes("text-gray-500 italic")

    def generate_pdf_action(gen_combined: bool = True) -> None:
        try:
            _record("Deduplicating…")
            entries = store.all_entries()
            kept, removed = deduplicate(entries)
            _record(
                f"Kept {len(kept)}; removed {len(removed)} (duplicates/failed)"
            )

            if not kept:
                _record("No entries to render — scrape some posts first")
                ui.notify("No entries to render", type="warning")
                return

            mode = "combined + per-year" if gen_combined else "per-year only"
            _record(f"Generating PDF ({mode})…")
            path = generate_pdf(kept, PDF_PATH, combined=gen_combined, store=store)
            if gen_combined:
                state.pdf_path = str(path)
            _record(f"PDF written: {path}")
            ui.notify("PDF ready")
            pdf_view.refresh()
        except Exception as exc:  # noqa: BLE001
            log.exception("PDF generation failed")
            ui.notify(f"PDF failed: {exc}", type="negative")

    with ui.card().classes("w-full mb-8"):
        ui.label("PDF Output").classes("text-lg font-semibold")

        combined_toggle = ui.switch(
            "Include combined PDF (slower)", value=False
        ).props("dense")
        combined_toggle.tooltip(
            "If on: generates combined + per-year PDFs. "
            "If off: per-year PDFs only (faster)."
        )

        with ui.row().classes("items-center gap-2"):
            def on_gen_click() -> None:
                generate_pdf_action(gen_combined=bool(combined_toggle.value))

            ui.button(
                "Generate PDF",
                on_click=on_gen_click,
                icon="picture_as_pdf",
            )
            ui.button(
                "Refine entries",
                on_click=lambda: ui.navigate.to("/refine"),
                icon="edit_note",
            ).props("flat")
        pdf_view()


# --- Refine page ------------------------------------------------------


def _sorted_entries() -> list[ImageEntry]:
    """All entries, oldest post first; deterministic within a post."""
    return sorted(
        store.all_entries(),
        key=lambda e: (
            e.post_date or "9999-99-99",
            e.post_url,
            SECTION_ORDER.get(e.section, 99),
            e.image_path or "",
        ),
    )


def _available_years() -> list[str]:
    """Distinct YYYY prefixes of all post_dates in the store, sorted."""
    years = {
        (e.post_date or "")[:4]
        for e in store.all_entries()
        if e.post_date
    }
    return sorted(y for y in years if y)


@ui.page("/refine")
def refine_page() -> None:
    page_state: dict = {
        "filter": "not_done",
        "year": "all",
        "index": 0,
        "dirty": False,
        "page_index": {},
    }

    def rebuild_page_index() -> None:
        """Recompute entry → PDF-page mapping using the same pipeline
        as ``generate_pdf_action`` (dedup + group + sort)."""
        try:
            kept, _removed = deduplicate(store.all_entries())
            page_state["page_index"] = compute_page_index(kept)
        except Exception:  # noqa: BLE001
            log.exception("compute_page_index failed")
            page_state["page_index"] = {}

    rebuild_page_index()

    def filtered() -> list[ImageEntry]:
        items = _sorted_entries()
        # Only show entries that:
        # 1. Are in the page_index (survived dedup, have valid image)
        # 2. Are not marked ignored
        items = [
            e
            for e in items
            if entry_key(e) in page_state["page_index"] and not e.ignored
        ]
        year = page_state["year"]
        if year != "all":
            items = [e for e in items if (e.post_date or "")[:4] == year]
        if page_state["filter"] == "not_done":
            items = [e for e in items if not e.done]
        elif page_state["filter"] == "done":
            items = [e for e in items if e.done]
        return items

    def current() -> ImageEntry | None:
        items = filtered()
        if not items:
            page_state["index"] = 0
            return None
        page_state["index"] = max(0, min(page_state["index"], len(items) - 1))
        return items[page_state["index"]]

    ui.label("Refine Entries").classes("text-2xl font-bold mt-4")
    with ui.row().classes("items-center gap-4 mb-2 flex-wrap"):
        ui.button(
            "Back",
            on_click=lambda: ui.navigate.to("/"),
            icon="arrow_back",
        ).props("flat")
        with ui.row().classes("items-center gap-2"):
            ui.label("Status:").classes("text-sm text-gray-600")
            filter_toggle = ui.toggle(
                {"all": "All", "not_done": "Not done", "done": "Done"},
                value=page_state["filter"],
            )
        with ui.row().classes("items-center gap-2"):
            ui.label("Year:").classes("text-sm text-gray-600")
            year_options = {"all": "All"}
            for y in _available_years():
                year_options[y] = y
            year_select = (
                ui.select(year_options, value=page_state["year"])
                .props("dense outlined")
                .classes("min-w-[7rem]")
            )

        def on_filter_change(e) -> None:
            page_state["filter"] = e.value
            page_state["index"] = 0
            page_state["dirty"] = False
            entry_panel.refresh()

        def on_year_change(e) -> None:
            page_state["year"] = e.value
            page_state["index"] = 0
            page_state["dirty"] = False
            entry_panel.refresh()

        filter_toggle.on_value_change(on_filter_change)
        year_select.on_value_change(on_year_change)

    @ui.refreshable
    def entry_panel() -> None:
        entry = current()
        items = filtered()
        total = len(items)

        with ui.row().classes("items-center justify-between w-full mb-2"):
            if entry is None:
                ui.label("0 / 0").classes("text-sm text-gray-500")
            else:
                ui.label(f"{page_state['index'] + 1} / {total}").classes(
                    "text-sm text-gray-500"
                )
            with ui.row().classes("items-center gap-1"):
                ui.button(
                    "Prev",
                    on_click=lambda: navigate(-1),
                    icon="chevron_left",
                ).props("flat dense")
                ui.button(
                    "Next",
                    on_click=lambda: navigate(1),
                ).props("flat dense icon-right=chevron_right")

        if entry is None:
            ui.label("No entries match the current filter.").classes(
                "text-gray-500 italic"
            )
            return

        eid = entry_key(entry)

        ui.label(
            f"{entry.post_date or '—'}  ·  {entry.section}  ·  {entry.post_title}"
        ).classes("text-base font-semibold")
        if entry.post_url:
            ui.link(entry.post_url, entry.post_url, new_tab=True).classes(
                "text-xs text-blue-600"
            )

        location = page_state["page_index"].get(eid)
        pdf_file = (
            OUTPUT_DIR / location["year_pdf"] if location else None
        )
        if location and pdf_file and pdf_file.exists():
            cache_buster = int(pdf_file.stat().st_mtime)
            pdf_url = (
                f"/pdf/{location['year_pdf']}"
                f"?v={cache_buster}#page={location['year_page']}"
                "&toolbar=0&navpanes=0&view=FitV"
            )
            ui.label(
                f"PDF preview · {location['year_pdf']} page {location['year_page']}"
                f"  (combined page {location['combined_page']})"
            ).classes("text-xs text-gray-500 mt-2")
            # ui.html sanitizes via DOMPurify which strips <iframe>; disable
            # since the URL is built from values we control.
            ui.html(
                f'<iframe src="{pdf_url}" '
                'style="width:100%;height:720px;border:1px solid #ccc;'
                'border-radius:4px;" '
                'title="LaTeX-rendered page"></iframe>',
                sanitize=False,
            ).classes("w-full")
        else:
            filename = Path(entry.image_path).name if entry.image_path else ""
            if filename and Path(entry.image_path).exists():
                ui.label(
                    "(PDF page not available — showing source image. "
                    "Generate the PDF from the home page to see the rendered page.)"
                ).classes("text-xs text-gray-500 italic mt-2")
                ui.image(f"/cached-images/{filename}").classes(
                    "max-h-96 w-auto rounded my-2"
                )
            else:
                ui.label("(image missing)").classes("text-red-500 italic")

        if entry.section_intro:
            with ui.expansion("Section intro").classes("w-full"):
                ui.html(entry.section_intro).classes("text-sm")

        if entry.text_original:
            with ui.expansion("Original text (pre-edit)").classes("w-full"):
                ui.html(entry.text_original).classes(
                    "text-sm bg-gray-50 p-2 rounded"
                )

        ui.label("Edited text").classes("text-sm font-medium mt-2")
        ta = ui.textarea(value=entry.text or "").classes("w-full font-mono")
        ta.props("autogrow outlined")

        def mark_dirty(_=None) -> None:
            page_state["dirty"] = True

        ta.on_value_change(mark_dirty)

        with ui.row().classes("items-center gap-6 mt-2"):
            ignored_switch = ui.switch(
                "Ignore (exclude from PDF)", value=entry.ignored
            )
            done_switch = ui.switch("Mark as done", value=entry.done)

        def toggle_field(field: str, value: bool) -> None:
            store.update_entry(eid, **{field: value})
            page_state["dirty"] = False
            # NOTE: we do NOT rebuild page_index here. Ignored entries
            # will drop out of the PDF once generate_pdf_action() runs,
            # but the PDFs on disk haven't changed yet. Rebuilding now
            # would show the wrong page numbers (post-ignored, but pre-
            # regeneration). User will see the correct page after
            # regenerating the PDF from the home page.
            entry_panel.refresh()

        ignored_switch.on_value_change(lambda e: toggle_field("ignored", e.value))
        done_switch.on_value_change(lambda e: toggle_field("done", e.value))

        def save() -> ImageEntry | None:
            updated = store.update_entry(eid, text=ta.value or "")
            page_state["dirty"] = False
            return updated

        def save_action() -> None:
            if save() is None:
                ui.notify("Entry not found", type="negative")
                return
            ui.notify("Saved")
            entry_panel.refresh()

        def save_and_next() -> None:
            if save() is None:
                ui.notify("Entry not found", type="negative")
                return
            page_state["index"] += 1
            entry_panel.refresh()

        def discard() -> None:
            page_state["dirty"] = False
            entry_panel.refresh()

        with ui.row().classes("items-center gap-2 mt-3"):
            ui.button("Save", on_click=save_action, icon="save").props("color=primary")
            ui.button(
                "Save & Next", on_click=save_and_next, icon="east"
            ).props("color=primary")
            ui.button("Discard changes", on_click=discard, icon="undo").props("flat")

    async def navigate(delta: int) -> None:
        if page_state["dirty"]:
            with ui.dialog() as dialog, ui.card():
                ui.label("You have unsaved changes to the edited text.").classes(
                    "text-base"
                )
                ui.label(
                    "Save first if you want to keep them, or discard to lose them."
                ).classes("text-sm text-gray-500")
                with ui.row().classes("justify-end gap-2 mt-2"):
                    ui.button(
                        "Cancel", on_click=lambda: dialog.submit("cancel")
                    ).props("flat")
                    ui.button(
                        "Discard & continue",
                        on_click=lambda: dialog.submit("discard"),
                    ).props("flat color=negative")
            choice = await dialog
            if choice != "discard":
                return
            page_state["dirty"] = False
        page_state["index"] = max(0, page_state["index"] + delta)
        entry_panel.refresh()

    entry_panel()


if __name__ in {"__main__", "__mp_main__"}:
    ui.run(
        title="Visual Ideas Archive",
        port=8080,
        show=True,
        reload=False,
        favicon="📐",
    )
