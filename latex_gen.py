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

_BOILERPLATE_CHUNK_RE = re.compile(
    r"(subscribe below|thanks for reading|made this newsletter with beehiiv|"
    r"say hi on |visual i\.?d\.?e\.?a|thinking in visual metaphors|"
    r"reply any time|cohort \d+ is|straight into (their|your) inbox|"
    r"getting .{0,50} subscriber)",
    re.IGNORECASE,
)


def _strip_boilerplate(html: str) -> str:
    """Remove newsletter footer boilerplate chunks from caption HTML.

    Captions are stored as chunks joined by <br/><br/>. Any chunk that
    matches the boilerplate pattern is dropped.
    """
    if not html:
        return html
    chunks = html.split("<br/><br/>")
    kept = [c for c in chunks if not _BOILERPLATE_CHUNK_RE.search(c)]
    return "<br/><br/>".join(kept)


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
    paper=a5paper,
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
\usepackage{subfiles}

\definecolor{pagebg}{HTML}{C5E3FE}
\pagecolor{pagebg}

\IfFontExistsTF{Arial}{%
    \setmainfont{Arial}%
}{%
    \setmainfont{Latin Modern Roman}%
}

\pagestyle{fancy}
\fancyhf{}
\renewcommand{\headrulewidth}{0.4pt}
\renewcommand{\footrulewidth}{0.4pt}

\newcommand{\theposttitle}{}
\newcommand{\thepostdate}{}

\fancyhead[L]{\color{gray!70}\footnotesize\itshape\theposttitle}
\fancyfoot[L]{\color{gray!70}\footnotesize\thepostdate}
\fancyfoot[C]{\color{gray!70}\footnotesize\thepage\,/\,\pageref{LastPage}}

\setlength{\parindent}{0pt}

\begin{document}
"""


def _year_preamble() -> str:
    return "\\documentclass[archive.tex]{subfiles}\n\\begin{document}\n"


def _cover_page(year: str) -> str:
    return (
        "\\thispagestyle{empty}\n"
        "\\vspace*{\\fill}\n"
        "\\begin{center}\n"
        "{\\fontsize{72}{86}\\selectfont\\bfseries " + year + "}\n"
        "\\end{center}\n"
        "\\vspace*{\\fill}\n"
        "\\newpage\n"
    )


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
    year_seen: set[str] = set()
    combined_extra = 0  # cumulative cover pages encountered so far
    for combined_idx, (_post_url, _section, _title, post_date, group) in enumerate(
        groups, start=1
    ):
        year = (post_date or "0000")[:4]
        if year not in year_seen:
            year_seen.add(year)
            combined_extra += 1  # cover page for this year
        year_counters[year] = year_counters.get(year, 0) + 1
        year_page = year_counters[year] + 1  # +1 for year cover page
        location = {
            "combined_page": combined_idx + combined_extra,
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
    """Generate A5 PDFs: one (post, section) per page.

    Writes a master ``.tex`` plus one ``.tex`` per year (with content
    inlined — no per-entry files).  Each year file is a standalone
    subfile compilable on its own or included in the master via \\subfile.

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

    groups = _group_by_section(entries)
    if not groups:
        raise ValueError("No renderable entries to write to PDF")

    # Render each page and accumulate into per-year buckets
    year_pages: dict[str, list[str]] = defaultdict(list)
    for post_url, section, post_title, post_date, group in groups:
        intro_html = next((e.section_intro for e in group if e.section_intro), "")
        intro_latex = _html_to_latex(intro_html)

        images: list[tuple[str, str]] = []
        for entry in group:
            img_path = _tex_path(entry.image_path, output_path.parent)
            caption_latex = _html_to_latex(entry.text)
            images.append((img_path, caption_latex))

        page_tex = _render_page(section, intro_latex, images, post_title, post_date)
        year = (post_date or "0000")[:4]
        year_pages[year].append(page_tex)

    # Write per-year .tex files with inlined content
    for year, pages in year_pages.items():
        cover = _cover_page(year)
        body = ("\n\\newpage\n").join(pages)
        year_tex = _year_preamble() + cover + body + _POSTAMBLE
        year_tex_path = output_path.parent / f"archive_{year}.tex"
        year_tex_path.write_text(year_tex, encoding="utf-8")
        log.info("Wrote year LaTeX: %s", year_tex_path)

    # Write master .tex referencing year subfiles in sorted order
    sorted_years = sorted(year_pages.keys())
    subfile_lines = [r"\subfile{archive_" + y + "}" for y in sorted_years]
    tex = _preamble() + "\n".join(subfile_lines) + _POSTAMBLE
    tex_path = output_path.with_suffix(".tex")
    tex_path.write_text(tex, encoding="utf-8")
    log.info("Wrote %d pages across %d years", len(groups), len(year_pages))
    log.info("Wrote master LaTeX: %s", tex_path)

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
    for year in year_pages:
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
        len(year_pages),
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
