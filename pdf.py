from __future__ import annotations

from pathlib import Path

from PIL import Image as PILImage
from reportlab.lib.colors import HexColor
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import B5
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Image,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
)

from models import ImageEntry

# --- Layout constants -------------------------------------------------

MARGIN = 18 * mm
TITLE_GAP = 6 * mm          # space between title and image
IMAGE_TEXT_GAP = 6 * mm     # space between image and body text
FOOTER_BAND = 12 * mm       # space at the page bottom reserved for the footer
IMAGE_MAX_FRACTION = 0.62   # image won't exceed this fraction of usable height
MIN_IMAGE_HEIGHT = 25 * mm  # don't shrink images below this

SECTION_ORDER = {
    "INTERESTING": 0,
    "DESIGN": 1,
    "ENCHANTING": 2,
    "ANALOGY": 3,
}

# --- Styles -----------------------------------------------------------


def _build_styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "section": ParagraphStyle(
            "Section",
            parent=base["Heading1"],
            fontName="Helvetica-Bold",
            fontSize=20,
            leading=24,
            alignment=TA_CENTER,
            textColor=HexColor("#1a1a1a"),
            spaceBefore=0,
            spaceAfter=0,
        ),
        "body": ParagraphStyle(
            "Body",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=10.5,
            leading=14.5,
            alignment=TA_LEFT,
            textColor=HexColor("#333333"),
            spaceBefore=0,
            spaceAfter=0,
        ),
    }


# --- Helpers ----------------------------------------------------------


def _scaled_image(path: str, max_width: float, max_height: float) -> Image | None:
    try:
        with PILImage.open(path) as pil:
            pil.load()
            iw, ih = pil.size
    except Exception:
        return None
    if iw <= 0 or ih <= 0 or max_height <= 0 or max_width <= 0:
        return None

    aspect = ih / iw
    width = max_width
    height = width * aspect
    if height > max_height:
        height = max_height
        width = height / aspect
    return Image(path, width=width, height=height, hAlign="CENTER")


def _build_entry_flowables(
    entry: ImageEntry,
    styles: dict[str, ParagraphStyle],
    usable_width: float,
    usable_height: float,
) -> list:
    """Build [title, gap, image, gap, body] for one entry.

    Image height is computed dynamically so the whole block fits within
    usable_height (i.e. one page minus margins minus footer band).
    """
    title = Paragraph(entry.section, styles["section"])
    _, title_h = title.wrap(usable_width, usable_height)

    body: Paragraph | None = None
    body_h = 0.0
    if entry.text:
        body = Paragraph(entry.text, styles["body"])
        _, body_h = body.wrap(usable_width, usable_height)

    gaps = TITLE_GAP + (IMAGE_TEXT_GAP if body is not None else 0)
    available_for_image = usable_height - title_h - body_h - gaps
    image_cap = usable_height * IMAGE_MAX_FRACTION
    image_max = max(MIN_IMAGE_HEIGHT, min(image_cap, available_for_image))

    img = _scaled_image(entry.image_path, usable_width, image_max)
    if img is None:
        return []

    flowables: list = [title, Spacer(1, TITLE_GAP), img]
    if body is not None:
        flowables.append(Spacer(1, IMAGE_TEXT_GAP))
        flowables.append(body)
    return flowables


def _block_height(flowables: list, width: float, height: float) -> float:
    total = 0.0
    for f in flowables:
        if isinstance(f, Spacer):
            total += f.height
        else:
            total += f.wrap(width, height)[1]
    return total


def _make_footer_callback(entries: list[ImageEntry]):
    """Return an onPage callback that draws a per-page footer.

    Pages are 1:1 with entries (we PageBreak after each), so a page
    counter is enough to look up the entry for the current page.
    """
    page_idx = [0]
    total = len(entries)
    page_width, _ = B5

    def on_page(canvas, _doc) -> None:
        idx = page_idx[0]
        if idx < total:
            entry = entries[idx]
            canvas.saveState()

            # Thin divider line above the footer text
            canvas.setStrokeColor(HexColor("#dddddd"))
            canvas.setLineWidth(0.4)
            y_line = MARGIN * 0.65
            canvas.line(MARGIN, y_line, page_width - MARGIN, y_line)

            canvas.setFont("Helvetica", 7.5)
            canvas.setFillColor(HexColor("#888888"))
            y_text = MARGIN * 0.35

            title = entry.post_title or ""
            if len(title) > 48:
                title = title[:47] + "…"
            canvas.drawString(MARGIN, y_text, title)
            canvas.drawCentredString(page_width / 2, y_text, f"{idx + 1} / {total}")
            canvas.drawRightString(page_width - MARGIN, y_text, entry.post_date or "")

            canvas.restoreState()
        page_idx[0] += 1

    return on_page


# --- Public API -------------------------------------------------------


def generate_pdf(entries: list[ImageEntry], output_path: Path) -> Path:
    """Generate a B5 PDF: one entry per page, content vertically centered.

    Per-page layout (top to bottom):
        - top padding (auto-computed to center the block)
        - SECTION title (bold, centered)
        - image (centered, scaled to fit remaining space)
        - body text (left-aligned, rich HTML rendered by ReportLab)
        - footer drawn on the canvas: title • page n/N • date
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    sorted_entries = sorted(
        entries,
        key=lambda e: (
            e.post_date or "9999-99-99",
            e.post_url,
            SECTION_ORDER.get(e.section, 99),
        ),
    )
    renderable = [
        e for e in sorted_entries
        if e.image_path and not e.fetch_failed and Path(e.image_path).exists()
    ]
    if not renderable:
        raise ValueError("No renderable entries to write to PDF")

    page_width, page_height = B5
    usable_width = page_width - 2 * MARGIN
    usable_height = page_height - 2 * MARGIN - FOOTER_BAND

    styles = _build_styles()
    story: list = []

    for entry in renderable:
        flowables = _build_entry_flowables(entry, styles, usable_width, usable_height)
        if not flowables:
            continue
        total_h = _block_height(flowables, usable_width, usable_height)
        top_pad = max(0.0, (usable_height - total_h) / 2)
        if top_pad > 0:
            story.append(Spacer(1, top_pad))
        story.extend(flowables)
        story.append(PageBreak())

    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=B5,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=MARGIN,
        bottomMargin=MARGIN,
        title="Visual Ideas Archive",
    )

    on_page = _make_footer_callback(renderable)
    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    return output_path
