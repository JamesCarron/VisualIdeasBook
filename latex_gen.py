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

import logging
import re
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString, Tag

from models import ImageEntry

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
    parts.append(r"\begin{center}")
    parts.append(r"{\Huge\bfseries " + _escape_latex(section) + r"}\\[6mm]")
    parts.append(r"\end{center}")

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
    buckets: dict[tuple[str, str], list[ImageEntry]] = defaultdict(list)
    for e in entries:
        if not e.image_path or e.fetch_failed:
            continue
        if not Path(e.image_path).exists():
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


# --- Public API -------------------------------------------------------


def generate_pdf(entries: list[ImageEntry], output_path: Path) -> Path:
    """Generate a B5 PDF: one (post, section) per page.

    Always writes the ``.tex`` source next to the output PDF, regardless
    of whether xelatex is available. If xelatex isn't found, the .tex is
    still useful — compile it elsewhere (Overleaf, another machine).
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    groups = _group_by_section(entries)
    if not groups:
        raise ValueError("No renderable entries to write to PDF")

    body_parts: list[str] = []
    for i, (post_url, section, post_title, post_date, group) in enumerate(groups):
        # Section intro: take the first non-empty value across the group
        # (extractor broadcasts the same intro to every image in a section).
        intro_html = next((e.section_intro for e in group if e.section_intro), "")
        intro_latex = _html_to_latex(intro_html)

        images: list[tuple[str, str]] = []
        for entry in group:
            img_path = _tex_path(entry.image_path, output_path.parent)
            caption_latex = _html_to_latex(entry.text)
            images.append((img_path, caption_latex))

        if i > 0:
            body_parts.append(r"\newpage")
        body_parts.append(
            _render_page(section, intro_latex, images, post_title, post_date)
        )

    tex = _preamble() + "\n".join(body_parts) + _POSTAMBLE
    tex_path = output_path.with_suffix(".tex")
    tex_path.write_text(tex, encoding="utf-8")
    log.info("Wrote LaTeX source: %s", tex_path)

    engine = shutil.which("xelatex")
    if engine is None:
        raise RuntimeError(
            "xelatex not found in PATH. The .tex file was written to "
            f"{tex_path} — install MikTeX (https://miktex.org/) or "
            "TeX Live, then re-run."
        )

    log.info("Compiling with xelatex: %s", engine)
    # Touch the target PDF so we can detect whether xelatex actually wrote
    # a new one (vs leaving a stale file from a previous run untouched).
    if output_path.exists():
        output_path.unlink()

    # Run xelatex twice so \pageref{LastPage} resolves on the second pass.
    # xelatex writes outputs into its cwd by default; setting cwd to the
    # parent directory keeps all .aux/.log/.pdf files alongside the .tex.
    for pass_num in (1, 2):
        result = subprocess.run(
            [
                engine,
                "-interaction=nonstopmode",
                "-halt-on-error",
                tex_path.name,
            ],
            cwd=tex_path.parent,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
        if result.returncode != 0:
            log_path = tex_path.with_suffix(".log")
            raise RuntimeError(
                f"xelatex failed on pass {pass_num}. See log: {log_path}\n"
                f"Last stderr lines:\n{(result.stderr or '')[-1000:]}"
            )

    if not output_path.exists():
        raise RuntimeError(
            f"xelatex returned 0 but {output_path} was not produced."
        )

    log.info("PDF generated: %s", output_path)
    return output_path
