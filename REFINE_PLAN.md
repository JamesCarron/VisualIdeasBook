# Entry Refinement GUI — Plan

A NiceGUI screen for reviewing scraped entries one at a time, editing
their text, and marking which ones should appear in the generated PDF.

## Goals

1. Walk through every entry chronologically (newest post first).
2. Edit an entry's caption text while preserving the original for
   reference.
3. Mark entries as **Ignored** (excluded from PDF) or **Done** (review
   finished).
4. Filter the navigation set: All / Not done / Done.
5. Persist all state in `data/entries.json` so progress survives
   restarts.

## Data Model Changes — [models.py](models.py)

Three new fields on `ImageEntry` (all with defaults so old JSON loads
without migration code):

| Field           | Type | Default | Purpose                                                          |
| --------------- | ---- | ------- | ---------------------------------------------------------------- |
| `ignored`       | bool | False   | Excludes entry from PDF generation                               |
| `done`          | bool | False   | User has finished reviewing this entry                           |
| `text_original` | str  | ""      | The first-ever value of `text` — captured the first time the user edits |

`text_original` is set lazily: empty until the user changes `text`, at
which point we snapshot the old value into `text_original` once and
never overwrite it again. This keeps the original available as
reference without doubling storage for entries that are never touched.

### JSON backwards compatibility

`EntryStore._load` currently does `ImageEntry(**e)`. If the on-disk
JSON has fields the dataclass doesn't know about, that throws. The
reverse (dataclass has fields the JSON doesn't have) is fine because
they all have defaults. Adding fields is safe; we only need to guard
if we ever remove one.

## PDF Generation Filter — [latex_gen.py](latex_gen.py)

`_group_by_section` already filters on `fetch_failed` and missing
images. Add `e.ignored` to that filter so ignored entries silently
drop out of both the combined and per-year PDFs.

## Store API — [models.py](models.py)

Two small additions to `EntryStore`:

- `update_entry(post_url, section, image_path, **fields)` — locate the
  matching entry (composite key — post_url + section + image_path is
  unique) and update fields. Handles the lazy `text_original`
  snapshotting. Saves immediately.
- `get_by_id(entry_id)` — convenience lookup. We'll use a deterministic
  id derived from post_url+section+image_path to round-trip through
  the URL.

Saving on every edit is fine — entries.json is small and the user is
making one change at a time.

## GUI — new page in [app.py](app.py)

New route `/refine` (separate page so the main scraper UI stays
uncluttered). A link from the home page sends the user there.

### Layout (one entry per page view)

```
+--------------------------------------------------------------+
| Refine Entries                                  [← Back]     |
| Filter: ( • All ) ( Not done ) ( Done )                      |
| Showing 12 / 184            [Prev]  [Next]                   |
+--------------------------------------------------------------+
|                                                              |
|   2024-08-15 · idea-42-... · INTERESTING                     |
|                                                              |
|   ┌─────────────────────────────────────┐                    |
|   │                                     │                    |
|   │           (image preview)           │                    |
|   │                                     │                    |
|   └─────────────────────────────────────┘                    |
|                                                              |
|   ▼ Original text (collapsed, read-only)                     |
|                                                              |
|   ┌─ Edited text ──────────────────────────────────────────┐ |
|   │ <textarea, multiline, HTML allowed>                    │ |
|   └────────────────────────────────────────────────────────┘ |
|                                                              |
|   [ ] Ignore (exclude from PDF)                              |
|   [ ] Mark as done                                           |
|                                                              |
|   [Save & Next]   [Save]   [Discard changes]                 |
+--------------------------------------------------------------+
```

### Ordering

```python
sorted(
    entries,
    key=lambda e: (
        e.post_date or "0000-00-00",   # newest first → reverse sort
        e.post_url,
        SECTION_ORDER.get(e.section, 99),
        e.image_path,
    ),
    reverse=True,
)
```

Reverse sort puts the latest post first. Within a post we keep
section order (INTERESTING → DESIGN → ENCHANTING → ANALOGY) and within
a section, deterministic by image_path.

### Filter behavior

The filter narrows the navigation set, not the full list. So `Next`
on "Not done" skips past entries already marked done. The counter
reflects position within the filtered set.

### Editing semantics

- The edit textarea is pre-filled with `entry.text` (the current
  working copy, edited or not).
- The "Original text" expansion shows `text_original` if non-empty,
  otherwise the current `text` (since they're identical pre-edit).
- **Save** writes the textarea value into `entry.text`. If
  `text_original` was empty AND the new text differs from the current
  `text`, the *current* `text` is snapshotted into `text_original`
  first.
- **Discard** reloads the entry from the store, throwing away unsaved
  textarea changes.
- "Ignore" and "Done" toggles save immediately (no need to also click
  Save).
- Navigating away with unsaved textarea changes shows a confirmation.

### Image serving

`/cached-images` static mount already exists. The refine page reuses
it.

## File changes summary

| File                      | Change                                                                                  |
| ------------------------- | --------------------------------------------------------------------------------------- |
| [models.py](models.py)    | Add `ignored`, `done`, `text_original` fields; add `update_entry`, `get_by_id` methods. |
| [latex_gen.py](latex_gen.py) | `_group_by_section` skips `ignored` entries.                                         |
| [app.py](app.py)          | New `/refine` page; link from home; reuse existing `store` and static mounts.           |

No new files. No new dependencies — NiceGUI is already in use.

## Open questions for confirmation

1. **One entry per screen, or one (post, section) group per screen?**
   The data model has multiple entries per (post, section) when a
   section has more than one image. Editing the whole group at once
   matches how it renders in the PDF; editing per-entry matches how
   `text` is stored. Recommendation: **per-entry**, since text is
   per-entry. Sibling entries from the same group appear as small
   thumbnails for context.
2. **"Done" semantics — automatic or manual?** Auto-mark done on Save?
   Or always require explicit click? Recommendation: **manual** so
   "edit text" and "finished reviewing" stay independent.
3. **Auto-save on textarea blur, or only on explicit Save?**
   Recommendation: **explicit Save**, since edits can be exploratory
   and an accidental tab-out shouldn't commit a bad rewrite.
4. **Bulk operations** (e.g. "mark all not-yet-edited as done")?
   Recommendation: **out of scope** for v1 — add later if useful.
