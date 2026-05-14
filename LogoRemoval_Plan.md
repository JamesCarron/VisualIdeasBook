# Plan: Logo Removal via Template Matching

## Context

Images in `data/images/` (428 files — PNG, JPG, JPEG, GIF) contain a "milaniCREATIVE.art" watermark that has been isolated as `data/logo.png`. The logo is dark teal text on a white background. In the images it appears as dark text over the illustration background and can be:
- **Horizontal** (default orientation)
- **Vertical** (rotated 90°, right-edge strip)
- **Variable size** (smaller logos at ~10–30% of the original template width)

The goal is to locate every instance using multi-scale template matching, then fill the detected region with the local background colour. Originals are never touched; cleaned images go to `data/images_clean/`.

---

## Library choice

**`opencv-python`** — best fit for this task:
- `cv2.matchTemplate` with `TM_CCOEFF_NORMED` handles grayscale matching independent of absolute brightness, which copes with the teal template being rendered as near-black in the images.
- Native multi-scale support via `cv2.resize`.
- Fast (C++ backend); 428 images × ~20 scales × 2 orientations completes in a few minutes.

No other new library is needed. `Pillow` (already installed) is used for I/O and background-fill only.

Add to `requirements.txt`:
- `opencv-python>=4.9`
- `numpy>=1.24` (make the implicit transitive dep explicit)

---

## New file: `logo_remover.py`

### Key steps

#### 1 — Prepare templates once at startup

```
logo.png → grayscale → threshold dark pixels (< 150) → binary mask
```

Generate a list of `(template_gray, mask, scale, angle)` tuples:
- **Scales**: 20 steps from 0.10× to 0.60× of original logo width (covers expected in-image sizes).
- **Orientations**: 0° (horizontal) and 90° (vertical, `cv2.rotate(..., cv2.ROTATE_90_CLOCKWISE)`).
- Skip any template whose width or height becomes < 8 px (too small to match reliably).

#### 2 — Match each image

For every `(template, mask, scale, angle)`:

```python
result = cv2.matchTemplate(image_gray, template, cv2.TM_CCOEFF_NORMED, mask=mask)
_, max_val, _, max_loc = cv2.minMaxLoc(result)
```

Track the single best `(max_val, max_loc, template.shape, angle)` across all scale/orientation trials.

#### 3 — Accept or reject

- Accept if `max_val >= MATCH_THRESHOLD` (default **0.65** — tunable constant).
- If rejected, copy image unchanged.

#### 4 — Fill with background colour

Once a match is accepted:
1. Compute the bounding box `(x, y, w, h)` from `max_loc` and template shape.
2. Expand by `BBOX_PADDING = 6` px on each side (clips at image edges).
3. Sample background: take the **median RGB** of a 20 px border strip around the expanded box, excluding pixels darker than 100 (i.e. excluding any logo pixels that spill into the buffer).
4. Fill the expanded box with the sampled colour using `PIL.ImageDraw.rectangle`.
5. Save to `data/images_clean/<original_filename>`.

---

## Public API

```python
def build_templates(
    logo_path: Path = Path("data/logo.png"),
    scales: tuple[float, ...] = (...),
) -> list[tuple]:
    """Return list of (template_gray, mask, scale, angle) tuples."""

def find_logo(image_gray: np.ndarray, templates: list) -> tuple | None:
    """Return (x, y, w, h, angle) of best match, or None if below threshold."""

def remove_logo_from_image(
    src: Path,
    dst: Path,
    templates: list,
) -> bool:
    """Process one image. Returns True if logo was found and removed."""

def process_all_images(
    source_dir: Path = Path("data/images"),
    output_dir: Path = Path("data/images_clean"),
    logo_path: Path = Path("data/logo.png"),
) -> dict[str, int]:
    """
    Batch-process all images. Builds templates once, then iterates.
    Returns {"processed": N, "modified": N, "unchanged": N, "skipped": N}.
    """
```

When run directly (`python logo_remover.py`), calls `process_all_images()` and prints stats.

---

## Tunable constants

```python
SCALES          = tuple(round(s, 2) for s in np.linspace(0.10, 0.60, 20))
MATCH_THRESHOLD = 0.65   # TM_CCOEFF_NORMED score to accept a match
LOGO_DARK_THRESH = 150   # grayscale value below which a logo pixel is "active"
BBOX_PADDING    = 6      # px expansion around matched bounding box
BG_SAMPLE_BUF   = 20     # px wide sampling border for background colour
MIN_TEMPLATE_DIM = 8     # skip templates smaller than this in either dimension
```

---

## Files to create / modify

| File | Action |
|---|---|
| `logo_remover.py` | **Create** |
| `requirements.txt` | **Edit** — add `opencv-python>=4.9` and `numpy>=1.24` |

`data/images/` — **untouched**.  
`data/images_clean/` — created automatically if absent.

---

## Verification

```powershell
pip install opencv-python numpy

python logo_remover.py
# Expected: "Done: {'processed': 428, 'modified': ~390, 'unchanged': ~38, 'skipped': 0}"

# Confirm originals untouched
(Get-ChildItem data\images | Measure-Object).Count   # still 428

# Spot-check known logo images (both orientations)
# Horizontal logo: data/images/0910457e1f1c2e832c6e35068e4d82ed.png
# Vertical logo:   data/images/058bfbf7e9e5881b248da20e4f201fe3.png
# Dark bg (expect unchanged): data/images/0a6d20fffeef62c5583b0d13f046b88d.png
```

Validate:
1. Logo text region filled with a colour matching the local background.
2. No illustration content outside the logo bounding box altered.
3. `data/images_clean/` file count equals `data/images/` file count.
4. If `MATCH_THRESHOLD` needs tuning (false positives or misses), adjust and re-run.
