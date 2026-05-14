"""
logo_remover.py — Remove milaniCREATIVE.art logo watermarks from newsletter images.

Uses multi-scale, multi-orientation OpenCV template matching against data/logo.png.

Matching method: TM_CCOEFF_NORMED on grayscale without mask.
The logo template is composited onto a neutral gray background (BG_GRAY=200) so
that the contrast pattern (dark text on light field) matches the typical newsletter
illustration style. Mean-subtraction in CCOEFF handles per-image brightness offsets.

Three guards eliminate false positives:
1. Position filter — only the bottom 30% or rightmost 22% of each image is searched.
2. Light-fraction check — the matched patch must be ≥30% bright pixels (logos sit on
   light backgrounds; dark illustrations fail this check).
3. Score threshold — only accepts matches with normalised correlation ≥ 0.40.

Run directly:
    .venv/Scripts/python logo_remover.py
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw

# ── Tunable constants ─────────────────────────────────────────────────────────
# Logo template: 1237 × 193 px.
# Horizontal logos range ~150–750 px wide → scales 0.12–0.60.
# Vertical strip logos: ~20–30 px wide (from 193 px height) → scale ~0.10.
# Extend to 0.80 to cover large footer logos.
SCALES = tuple(round(s, 3) for s in np.linspace(0.07, 0.80, 30))

# Background gray level used when compositing the transparent logo for matching.
# ~200 matches the light-blue newsletter background in grayscale.
BG_GRAY = 200

# TM_CCOEFF_NORMED: 1.0 = perfect match.
# Real logos score 0.45–0.68; anything below 0.40 is noise or illustration content.
MATCH_THRESHOLD  = 0.40
EARLY_STOP       = 0.75   # stop iterating templates if a strong match is found

BBOX_PADDING     = 8      # px to expand the matched bounding box on each side
BG_SAMPLE_BUF    = 24     # px wide ring used to sample the local background colour
BG_DARK_THRESH   = 80     # exclude pixels with all RGB channels below this when sampling
MIN_TEMPLATE_DIM = 6      # skip any template dimension below this (px)
MAX_PASSES       = 6      # maximum logo-removal passes per image (catches multiple logos)

# Logos are found only in:
#   bottom strip: match origin y > LOGO_REGION_BOTTOM_FRAC × image_height  (bottom 30%)
#   right strip:  match right edge x+w > LOGO_REGION_RIGHT_FRAC × image_width (right 22%)
# 0.70 bottom threshold eliminates matches in the upper/middle illustration area.
LOGO_REGION_BOTTOM_FRAC = 0.70
LOGO_REGION_RIGHT_FRAC  = 0.78

# The matched bounding box must contain at least this fraction of light pixels
# (grayscale > 140). Logos sit on light backgrounds; dark-illustration patches fail.
MIN_LIGHT_FRACTION = 0.30

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif"}


# ── Template builder ──────────────────────────────────────────────────────────

def build_templates(
    logo_path: Path = Path("data/logo.png"),
    scales: tuple[float, ...] = SCALES,
) -> list[tuple[np.ndarray, float, int]]:
    """
    Load logo.png, composite it onto a neutral gray background, and return a list
    of (template_gray, scale, angle_deg) tuples.

    Angles produced:
      0   – horizontal (native orientation)
      90  – vertical, rotated 90° CW  (letter tops point right)
      270 – vertical, rotated 90° CCW (letter tops point left — right-edge style)
    """
    logo_pil = Image.open(logo_path).convert("RGBA")
    logo_arr = np.array(logo_pil)

    logo_gray = cv2.cvtColor(logo_arr[:, :, :3], cv2.COLOR_RGB2GRAY)
    logo_alpha = logo_arr[:, :, 3]

    # Composite: use logo pixels where alpha > 0, neutral gray elsewhere
    logo_composite = np.where(logo_alpha > 0, logo_gray, BG_GRAY).astype(np.uint8)

    orig_h, orig_w = logo_composite.shape
    templates: list[tuple[np.ndarray, float, int]] = []

    for scale in scales:
        new_w = max(1, int(orig_w * scale))
        new_h = max(1, int(orig_h * scale))

        if new_w < MIN_TEMPLATE_DIM or new_h < MIN_TEMPLATE_DIM:
            continue

        t = cv2.resize(logo_composite, (new_w, new_h), interpolation=cv2.INTER_AREA)

        templates.append((t, scale, 0))

        t_cw = cv2.rotate(t, cv2.ROTATE_90_CLOCKWISE)
        templates.append((t_cw, scale, 90))

        t_ccw = cv2.rotate(t, cv2.ROTATE_90_COUNTERCLOCKWISE)
        templates.append((t_ccw, scale, 270))

    return templates


# ── Logo detector ─────────────────────────────────────────────────────────────

def find_logo(
    image_gray: np.ndarray,
    templates: list[tuple[np.ndarray, float, int]],
) -> tuple[int, int, int, int, int] | None:
    """
    Return (x, y, w, h, angle_deg) of the best-matching logo region, or None.

    Constraints applied before accepting a match:
    1. Position: bottom 30% of image OR right 22% strip.
    2. Light fraction: bounding box must be ≥30% light pixels.
    3. Score: normalised CCOEFF ≥ MATCH_THRESHOLD.
    """
    img_h, img_w = image_gray.shape
    best_val = -1.0
    best_match: tuple[int, int, int, int, int] | None = None

    for t, _scale, angle in templates:
        th, tw = t.shape
        if th >= img_h or tw >= img_w:
            continue

        result = cv2.matchTemplate(image_gray, t, cv2.TM_CCOEFF_NORMED)
        np.nan_to_num(result, nan=-1.0, posinf=-1.0, neginf=-1.0, copy=False)

        # Build position mask, initialised to the minimum score so disallowed
        # positions can never win the max search.
        h_r, w_r = result.shape
        pos = np.full((h_r, w_r), -1.0, dtype=np.float32)

        # Bottom strip
        bottom_y = max(0, int(img_h * LOGO_REGION_BOTTOM_FRAC))
        if bottom_y < h_r:
            pos[bottom_y:, :] = result[bottom_y:, :]

        # Right strip (right edge of match must exceed the threshold)
        right_x = max(0, int(img_w * LOGO_REGION_RIGHT_FRAC) - tw)
        if right_x < w_r:
            pos[:, right_x:] = np.maximum(pos[:, right_x:], result[:, right_x:])

        _, max_val, _, max_loc = cv2.minMaxLoc(pos)

        if max_val <= best_val:
            continue

        # Light-fraction check: reject matches inside dark illustration regions
        x_loc, y_loc = max_loc
        patch = image_gray[y_loc:y_loc + th, x_loc:x_loc + tw]
        if (patch > 140).mean() < MIN_LIGHT_FRACTION:
            continue

        best_val = max_val
        best_match = (x_loc, y_loc, tw, th, angle)

        if best_val >= EARLY_STOP:
            break

    if best_val >= MATCH_THRESHOLD and best_match is not None:
        return best_match
    return None


# ── Background sampler ────────────────────────────────────────────────────────

def _sample_background(
    rgb: np.ndarray,
    x: int,
    y: int,
    w: int,
    h: int,
) -> tuple[int, int, int]:
    """
    Return the median RGB of the BG_SAMPLE_BUF-wide ring around the bounding box,
    excluding pixels that are too dark (residual logo ink).
    """
    img_h, img_w = rgb.shape[:2]

    x0 = max(0, x - BG_SAMPLE_BUF)
    y0 = max(0, y - BG_SAMPLE_BUF)
    x1 = min(img_w, x + w + BG_SAMPLE_BUF)
    y1 = min(img_h, y + h + BG_SAMPLE_BUF)

    region = rgb[y0:y1, x0:x1]

    inner = np.zeros(region.shape[:2], dtype=bool)
    iy0, ix0 = y - y0, x - x0
    inner[max(0, iy0):min(inner.shape[0], iy0 + h),
          max(0, ix0):min(inner.shape[1], ix0 + w)] = True

    dark = np.all(region < BG_DARK_THRESH, axis=2)
    valid = ~inner & ~dark
    samples = region[valid]

    if len(samples) == 0:
        return (200, 220, 240)

    return (
        int(np.median(samples[:, 0])),
        int(np.median(samples[:, 1])),
        int(np.median(samples[:, 2])),
    )


# ── Single-image processor ────────────────────────────────────────────────────

def remove_logo_from_image(
    src: Path,
    dst: Path,
    templates: list[tuple[np.ndarray, float, int]],
) -> bool:
    """
    Detect and erase every logo instance in src, writing the result to dst.
    Runs up to MAX_PASSES times to handle images with multiple logos.
    Returns True if at least one logo was removed.
    """
    img_pil = Image.open(src).convert("RGB")
    removed = 0

    for _ in range(MAX_PASSES):
        img_gray = np.array(img_pil.convert("L"))
        match = find_logo(img_gray, templates)
        if match is None:
            break

        x, y, w, h, _angle = match
        img_w, img_h = img_pil.size

        x0 = max(0, x - BBOX_PADDING)
        y0 = max(0, y - BBOX_PADDING)
        x1 = min(img_w, x + w + BBOX_PADDING)
        y1 = min(img_h, y + h + BBOX_PADDING)

        rgb_arr = np.array(img_pil)
        bg = _sample_background(rgb_arr, x0, y0, x1 - x0, y1 - y0)

        draw = ImageDraw.Draw(img_pil)
        draw.rectangle([x0, y0, x1, y1], fill=bg)
        removed += 1

    img_pil.save(dst)
    return removed > 0


# ── Batch processor ───────────────────────────────────────────────────────────

def process_all_images(
    source_dir: Path = Path("data/images"),
    output_dir: Path = Path("data/images_clean"),
    logo_path: Path = Path("data/logo.png"),
) -> dict[str, int]:
    """
    Process every image in source_dir and write cleaned copies to output_dir.
    Builds the template list once, then iterates over all images.
    Returns {"processed", "modified", "unchanged", "skipped"} counts.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    print("Building templates...", end=" ", flush=True)
    templates = build_templates(logo_path)
    print(f"{len(templates)} ready.")

    stats: dict[str, int] = {
        "processed": 0, "modified": 0, "unchanged": 0, "skipped": 0
    }

    files = sorted(p for p in source_dir.iterdir() if p.suffix.lower() in IMAGE_EXTS)
    total = len(files)

    for i, src in enumerate(files, 1):
        dst = output_dir / src.name
        try:
            modified = remove_logo_from_image(src, dst, templates)
            stats["processed"] += 1
            stats["modified" if modified else "unchanged"] += 1
        except Exception as exc:
            print(f"  [skip] {src.name}: {exc}")
            stats["skipped"] += 1

        if i % 50 == 0 or i == total:
            pct = i / total * 100
            print(f"  {i}/{total} ({pct:.0f}%)  "
                  f"modified={stats['modified']}  unchanged={stats['unchanged']}")

    return stats


# ── CLI entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    result = process_all_images()
    print(f"\nDone: {result}")
