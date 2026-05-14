"""LaTeX-based PDF generator.

Builds a B5 ``.tex`` document and compiles it to PDF via ``xelatex``.
xelatex is preferred over pdflatex because it uses system fonts and
natively handles Unicode (curly quotes, em dashes, etc.) in the
extracted text.

Public entry point: ``generate_pdf(entries, output_path)`` — same
signature as the older ReportLab-based ``pdf.generate_pdf``, so callers
can swap implementations transparently.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import logging
import re
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup, NavigableString, Tag

from models import ImageEntry, entry_key

log = logging.getLogger(__name__)

# --- Section ordering & layout ----------------------------------------

SECTION_ORDER = {
    "INTERESTING": 0,
    "DESIGN": 1,
    "ENCHANTING": 2,
    "ANALOGY": 3,
}

# Max image height as a fraction of \textheight, indexed by image count
# per section. Conservative — B5 is small and beehiiv captions are long,
# so multi-image sections need image space proportionally shrunk.
IMAGE_HEIGHT_BY_COUNT = {
    1: 0.55,
    2: 0.24,
    3: 0.17,
    4: 0.13,
}


# --- LaTeX escaping ----------------------------------------------------

_LATEX_ESCAPES = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
    "<": r"\textless{}",
    ">": r"\textgreater{}",
}


def _escape_latex(text: str) -> str:
    """Escape LaTeX special characters in a plain-text string."""
    return "".join(_LATEX_ESCAPES.get(ch, ch) for ch in text)


_DOUBLE_BREAK_RE = re.compile(r"(\\\\\s*){2,}")
_LEADING_BREAK_RE = re.compile(r"^(\\\\\s*)+")
_TRAILING_BREAK_RE = re.compile(r"(\\\\\s*)+$")


def _html_to_latex(html: str) -> str:
    """Convert our restricted HTML (b/strong/i/em/u/br) to LaTeX.

    The extractor already stripped <a> tags and any disallowed elements;
    we only need to recognise the inline-formatting subset here.
    Everything else collapses to escaped plain text.

    The extractor joins paragraph chunks with ``<br/><br/>`` so we
    post-process consecutive ``\\\\`` line breaks into paragraph breaks
    (blank lines) — LaTeX otherwise emits "no line to end" warnings.
    """
    if not html:
        return ""
    soup = BeautifulSoup(html, "lxml")
    latex = _node_to_latex(soup)
    latex = _DOUBLE_BREAK_RE.sub("\n\n", latex)
    latex = _LEADING_BREAK_RE.sub("", latex)
    latex = _TRAILING_BREAK_RE.sub("", latex)
    return latex.strip()


def _node_to_latex(node) -> str:
    if isinstance(node, NavigableString):
        return _escape_latex(str(node))
    if not isinstance(node, Tag):
        return ""

    inner = "".join(_node_to_latex(c) for c in node.children)
    name = node.name
    if name in ("strong", "b"):
        return r"\textbf{" + inner + "}"
    if name in ("em", "i"):
        return r"\textit{" + inner + "}"
    if name == "u":
        return r"\underline{" + inner + "}"
    if name == "br":
        return r"\\" + "\n"
    # html/body/[document] and anything else: pass through children
    return inner


# --- Path helpers -----------------------------------------------------


def _tex_path(p: str | Path, base: Path) -> str:
    """Return an absolute, forward-slash path for use inside \\includegraphics.

    LaTeX tolerates forward slashes on Windows even though the OS uses
    backslashes. Avoiding spaces matters too — our cached filenames are
    md5 hashes so they're safe.
    """
    abs_path = Path(p).resolve()
    return abs_path.as_posix()


# --- LaTeX document template ------------------------------------------


def _preamble() -> str:
    return r"""\documentclass[11pt]{article}

\usepackage[
    paperwidth=176mm,
    paperheight=250mm,
    top=18mm,
    bottom=22mm,
    left=18mm,
    right=18mm,
    footskip=10mm,
]{geometry}

\usepackage{graphicx}
\usepackage{xcolor}
\usepackage{fancyhdr}
\usepackage{parskip}
\usepackage{ragged2e}
\usepackage{fontspec}
\usepackage{lastpage}

% Use Arial — present on every Windows install, looked up via fontconfig.
% fontspec will fall back to the system default if Arial is unavailable.
\IfFontExistsTF{Arial}{%
    \setmainfont{Arial}%
}{%
    % Latin Modern is bundled with every TeX distribution; safe fallback.
    \setmainfont{Latin Modern Roman}%
}

\pagestyle{fancy}
\fancyhf{}
\renewcommand{\headrulewidth}{0pt}
\renewcommand{\footrulewidth}{0.4pt}

\newcommand{\theposttitle}{}
\newcommand{\thepostdate}{}

\fancyfoot[L]{\color{gray!70}\footnotesize\itshape\theposttitle}
\fancyfoot[C]{\color{gray!70}\footnotesize\thepage\,/\,\pageref{LastPage}}
\fancyfoot[R]{\color{gray!70}\footnotesize\thepostdate}

\setlength{\parindent}{0pt}

\begin{document}
"""


_POSTAMBLE = r"""
\end{document}
"""


def _render_page(
    section: str,
    section_intro: str,
    images: list[tuple[str, str]],   # list of (latex_image_path, caption_latex)
    post_title: str,
    post_date: str,
) -> str:
    """Render one section page (vertically centered, multi-image-aware)."""
    count = len(images)
    h_frac = IMAGE_HEIGHT_BY_COUNT.get(count, IMAGE_HEIGHT_BY_COUNT[4])

    parts: list[str] = []
    parts.append(r"\renewcommand{\theposttitle}{" + _escape_latex(post_title) + "}")
    parts.append(r"\renewcommand{\thepostdate}{" + _escape_latex(post_date) + "}")
    parts.append(r"\vspace*{\fill}")

    if section_intro:
        parts.append(r"\begin{center}")
        parts.append(r"\begin{minipage}{0.92\textwidth}")
        parts.append(r"\small")
        parts.append(section_intro)
        parts.append(r"\end{minipage}")
        parts.append(r"\end{center}")
        parts.append(r"\vspace{4mm}")

    for i, (img_path, caption_latex) in enumerate(images):
        if i > 0:
            parts.append(r"\vspace{5mm}")
        parts.append(r"\begin{center}")
        parts.append(
            r"\includegraphics[width=\textwidth, height="
            f"{h_frac}"
            r"\textheight, keepaspectratio]{"
            + img_path
            + "}"
        )
        parts.append(r"\end{center}")
        if caption_latex:
            parts.append(r"\vspace{2mm}")
            parts.append(r"\begin{center}")
            parts.append(r"\begin{minipage}{0.92\textwidth}")
            parts.append(r"\small")
            parts.append(caption_latex)
            parts.append(r"\end{minipage}")
            parts.append(r"\end{center}")

    parts.append(r"\vspace*{\fill}")
    return "\n".join(parts)


# --- Grouping ---------------------------------------------------------


def _group_by_section(
    entries: list[ImageEntry],
) -> list[tuple[str, str, str, str, list[ImageEntry]]]:
    """Group entries into one (post_url, section) bucket per page.

    Returns a list of (post_url, section, post_title, post_date, entries)
    tuples sorted chronologically and by canonical section order.
    """
    _SUPPORTED_EXTS = {".png", ".jpg", ".jpeg", ".pdf"}

    buckets: dict[tuple[str, str], list[ImageEntry]] = defaultdict(list)
    for e in entries:
        if not e.image_path or e.fetch_failed or e.ignored:
            continue
        p = Path(e.image_path)
        if not p.exists():
            continue
        if p.suffix.lower() not in _SUPPORTED_EXTS:
            log.debug("Skipping unsupported image format: %s", p.name)
            continue
        buckets[(e.post_url, e.section)].append(e)

    out: list[tuple[str, str, str, str, list[ImageEntry]]] = []
    for (post_url, section), group in buckets.items():
        # Use any entry's metadata for the post (they're identical).
        sample = group[0]
        out.append((post_url, section, sample.post_title, sample.post_date, group))

    out.sort(
        key=lambda t: (
            t[3] or "9999-99-99",            # post_date
            t[0],                            # post_url
            SECTION_ORDER.get(t[1], 99),     # section order
        )
    )
    return out


# --- Entry filename helpers -------------------------------------------

_SLUG_UNSAFE_RE = re.compile(r"[^a-z0-9]+")


def _url_slug(post_url: str) -> str:
    """Return a filesystem-safe slug derived from the post URL path."""
    path = urlparse(post_url).path.strip("/")
    slug = path.split("/")[-1] if path else "post"
    slug = _SLUG_UNSAFE_RE.sub("-", slug.lower()).strip("-")
    return slug or "post"


def _entry_filename(post_date: str, post_url: str, section: str) -> str:
    """Return the .tex filename for a single entry (no directory prefix)."""
    date = post_date or "0000-00-00"
    return f"{date}_{_url_slug(post_url)}_{section}.tex"


# --- Checksum for change detection ------------------------------------


def compute_year_checksum(year: str, entries: list[ImageEntry]) -> str:
    """Compute a checksum over all entries for a given year.

    Used to detect if entries have changed (text edited, ignored toggled,
    etc.) since the last PDF generation. Only checks content that affects
    rendering: text, ignored, image_path, section, post_title, post_date.
    """
    year_entries = [e for e in entries if (e.post_date or "")[:4] == year]
    year_entries.sort(
        key=lambda e: (e.post_date, e.post_url, e.section, e.image_path)
    )
    content = json.dumps(
        [
            {
                "text": e.text,
                "ignored": e.ignored,
                "image_path": e.image_path,
                "section": e.section,
                "post_title": e.post_title,
                "post_date": e.post_date,
            }
            for e in year_entries
        ],
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# --- Page index (for review / preview UI) -----------------------------


def compute_page_index(entries: list[ImageEntry]) -> dict[str, dict]:
    """Map ``entry_key(e)`` → location of its rendered page.

    Re-applies the same filter/group/sort logic that ``generate_pdf``
    uses, so the result faithfully describes where each entry will
    appear in the combined and per-year PDFs.

    Each value is a dict with keys:
        combined_page: int (1-based page in archive.pdf)
        year: str
        year_pdf: str (filename, e.g. ``archive_2024.pdf``)
        year_page: int (1-based page in the per-year PDF)

    Entries that are dropped from PDF rendering (ignored, missing
    image, fetch failed) are absent from the result.
    """
    groups = _group_by_section(entries)
    out: dict[str, dict] = {}
    year_counters: dict[str, int] = {}
    for combined_idx, (_post_url, _section, _title, post_date, group) in enumerate(
        groups, start=1
    ):
        year = (post_date or "0000")[:4]
        year_counters[year] = year_counters.get(year, 0) + 1
        year_page = year_counters[year]
        location = {
            "combined_page": combined_idx,
            "year": year,
            "year_pdf": f"archive_{year}.pdf",
            "year_page": year_page,
        }
        for member in group:
            out[entry_key(member)] = location
    return out


# --- Public API -------------------------------------------------------


def generate_pdf(
    entries: list[ImageEntry],
    output_path: Path,
    combined: bool = True,
    store: object | None = None,  # EntryStore, but avoid circular import
) -> Path:
    """Generate B5 PDFs: one (post, section) per page.

    Writes a master ``.tex`` file plus one ``.tex`` per entry under an
    ``entries/`` subdirectory alongside the output PDF.  The master file
    ``\\input``s each entry file so common formatting lives in one place.

    By default generates both the combined ``archive.pdf`` (all years) and
    per-year PDFs. Pass ``combined=False`` to skip the combined PDF and
    only generate per-year PDFs, which is faster.

    If ``store`` is provided (an EntryStore instance), only compiles PDFs
    for years whose entries have changed (detected via checksum). Pass
    ``store=None`` to always regenerate all PDFs.

    Always writes the ``.tex`` sources regardless of whether xelatex is
    available — compile on Overleaf or another machine if needed.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    entries_dir = output_path.parent / "entries"
    entries_dir.mkdir(exist_ok=True)

    groups = _group_by_section(entries)
    if not groups:
        raise ValueError("No renderable entries to write to PDF")

    input_lines: list[str] = []
    for i, (post_url, section, post_title, post_date, group) in enumerate(groups):
        intro_html = next((e.section_intro for e in group if e.section_intro), "")
        intro_latex = _html_to_latex(intro_html)

        images: list[tuple[str, str]] = []
        for entry in group:
            img_path = _tex_path(entry.image_path, output_path.parent)
            caption_latex = _html_to_latex(entry.text)
            images.append((img_path, caption_latex))

        page_tex = _render_page(section, intro_latex, images, post_title, post_date)

        year = (post_date or "0000")[:4]
        year_dir = entries_dir / year
        year_dir.mkdir(exist_ok=True)

        entry_filename = _entry_filename(post_date, post_url, section)
        (year_dir / entry_filename).write_text(page_tex, encoding="utf-8")

        if i > 0:
            input_lines.append(r"\newpage")
        rel = f"entries/{year}/{entry_filename}"
        input_lines.append(r"\input{" + rel + "}")

    # Build combined master tex
    tex = _preamble() + "\n".join(input_lines) + _POSTAMBLE
    tex_path = output_path.with_suffix(".tex")
    tex_path.write_text(tex, encoding="utf-8")
    log.info("Wrote %d entry files in %s", len(groups), entries_dir)
    log.info("Wrote master LaTeX: %s", tex_path)

    # Build per-year tex files
    year_input_lines: dict[str, list[str]] = defaultdict(list)
    for i, (post_url, section, post_title, post_date, _group) in enumerate(groups):
        year = (post_date or "0000")[:4]
        entry_filename = _entry_filename(post_date, post_url, section)
        rel = f"entries/{year}/{entry_filename}"
        if year_input_lines[year]:
            year_input_lines[year].append(r"\newpage")
        year_input_lines[year].append(r"\input{" + rel + "}")

    for year, lines in year_input_lines.items():
        year_tex = _preamble() + "\n".join(lines) + _POSTAMBLE
        year_tex_path = output_path.parent / f"archive_{year}.tex"
        year_tex_path.write_text(year_tex, encoding="utf-8")
        log.info("Wrote year LaTeX: %s", year_tex_path)

    engine = shutil.which("xelatex")
    if engine is None:
        raise RuntimeError(
            "xelatex not found in PATH. The .tex file was written to "
            f"{tex_path} — install MikTeX (https://miktex.org/) or "
            "TeX Live, then re-run."
        )

    log.info("Compiling with xelatex: %s", engine)

    def _compile(tex_file: Path, out_pdf: Path) -> None:
        # We'd prefer to unlink any prior PDF so a stale file can't
        # masquerade as a fresh output, but Windows refuses to unlink
        # a file held open by another process (e.g. a PDF viewer or a
        # browser iframe). Fall back to comparing mtimes in that case.
        pre_mtime = 0.0
        if out_pdf.exists():
            try:
                out_pdf.unlink()
            except PermissionError:
                pre_mtime = out_pdf.stat().st_mtime
                log.warning(
                    "Could not unlink %s (locked); will verify update by mtime",
                    out_pdf,
                )
        for pass_num in (1, 2):
            result = subprocess.run(
                [
                    engine,
                    "-interaction=nonstopmode",
                    "-halt-on-error",
                    tex_file.name,
                ],
                cwd=tex_file.parent,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
            )
            if result.returncode != 0:
                log_path = tex_file.with_suffix(".log")
                raise RuntimeError(
                    f"xelatex failed on pass {pass_num} for {tex_file.name}. "
                    f"See log: {log_path}\n"
                    f"Last stderr lines:\n{(result.stderr or '')[-1000:]}"
                )
        if not out_pdf.exists():
            raise RuntimeError(
                f"xelatex returned 0 but {out_pdf} was not produced."
            )
        if out_pdf.stat().st_mtime <= pre_mtime:
            raise RuntimeError(
                f"{out_pdf} was not updated by xelatex — likely held open "
                "by another application (close the PDF and retry)."
            )
        log.info("PDF generated: %s", out_pdf)

    # Compile combined + per-year PDFs in parallel. xelatex auxiliary
    # files are named after each .tex basename so concurrent runs in
    # the same directory don't collide.
    # If a store is provided, skip years whose entries haven't changed.
    compile_tasks: list[tuple[Path, Path, str | None]] = []
    if combined:
        compile_tasks.append((tex_path, output_path, None))
    for year in year_input_lines:
        skip_year = False
        if store is not None:
            current_checksum = compute_year_checksum(year, entries)
            stored_checksum = store.get_year_checksum(year)
            if stored_checksum == current_checksum:
                log.info(
                    "Skipping %s (entries unchanged since last generation)",
                    year,
                )
                skip_year = True
        if not skip_year:
            compile_tasks.append(
                (
                    output_path.parent / f"archive_{year}.tex",
                    output_path.parent / f"archive_{year}.pdf",
                    year,
                )
            )

    log.info(
        "Compiling %d PDFs in parallel (%d years%s)",
        len(compile_tasks),
        len(year_input_lines),
        " + combined" if combined else "",
    )

    if not compile_tasks:
        log.info("No PDFs to compile (all entries unchanged)")
        return output_path

    errors: list[tuple[Path, BaseException]] = []

    def _compile_and_store(tex_file: Path, out_pdf: Path, year: str | None) -> None:
        _compile(tex_file, out_pdf)
        # Update the checksum after successful compilation
        if store is not None and year is not None:
            checksum = compute_year_checksum(year, entries)
            store.set_year_checksum(year, checksum)

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(len(compile_tasks), 8)
    ) as pool:
        futures = {
            pool.submit(_compile_and_store, t, p, y): (t, y)
            for (t, p, y) in compile_tasks
        }
        for fut in concurrent.futures.as_completed(futures):
            tex_file, year = futures[fut]
            try:
                fut.result()
            except BaseException as exc:  # noqa: BLE001
                errors.append((tex_file, exc))

    if errors:
        names = ", ".join(t.name for t, _ in errors)
        raise RuntimeError(
            f"xelatex failed for: {names}"
        ) from errors[0][1]

    return output_path
