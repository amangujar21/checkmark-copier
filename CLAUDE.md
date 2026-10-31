# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Checkmark Copier is a local Windows desktop app that copies existing Adobe Fill & Sign-style check marks from one PDF source page to a user-selected target page range. It is a single-user, offline Tkinter application; there is no server, database, or network access.

The two goals that drive design decisions:

1. **Copy the mark exactly** — copy real PDF vector geometry and colour when available, not a redrawn approximation.
2. **Never modify the input PDF** — always write a new file through a temporary sibling and an atomic replace.

## Commands

```powershell
# Install runtime/build dependencies (Python 3.10+)
python -m pip install -r requirements.txt

# Run the GUI
python app.py

# Run with the bundled demo
python app.py --demo

# Run every test (core + GUI smoke)
python -m unittest discover -s tests -v

# Run one test module / one test
python -m unittest discover -s tests -p "test_core.py" -v
python -m unittest discover -s tests -p "test_core.py" -v -k test_adobe_geometry

# Build the one-file EXE and release ZIP
powershell -ExecutionPolicy Bypass -File .\build.ps1

# Development-only screenshot / auto-close
python app.py --demo --demo-preview result --screenshot samples\shot.png --smoke-test
```

`build.ps1` deletes `build/` and `dist/` before building. Do not run it while a copy of the EXE or ZIP is still needed elsewhere.

## Repository layout

- `app.py` — Tkinter application, all UI, event handling, preview rendering, save orchestration.
- `checkmark_core.py` — PDF detection, coordinate projection, drawing/stamping, page-range parsing. It must remain independent of Tkinter.
- `tests/test_core.py` — synthetic-PDF unit tests for geometry, detection, rotation, annotations, range parsing, and file safety.
- `tests/test_app_smoke.py` — headless GUI smoke tests; exercises loading, mark operations, and the save worker.
- `assets/` — app icon used by the window and PyInstaller.
- `samples/demo.pdf` — six-page demo checklist; page 1 has four checks.
- `dist/` — generated EXE/ZIP. It is git-ignored but intentionally present in this working copy.
- `README.md` — user-facing instructions and packaging notes.

## Architecture

### Core flow (`checkmark_core.py`)

1. `detect_checks(page)` collects vector checks and annotation checks, deduplicates them, and returns `CheckMark` objects sorted top-to-bottom.
2. Each `CheckMark` is either:
   - `kind == "vector"`: exact PDF points, stroke width, and normalized RGB colour.
   - `kind == "image"`: a transparent PNG cropped from a small annotation appearance, plus its page-space rect.
3. `project_mark()` maps a mark from the source page to a target page using either `proportional` (fractional position, uniform scale) or `absolute` (point units, page-origin offset).
4. `_stamp_page()` draws all vector marks through one `Page.new_shape()` and inserts image marks with alpha.
5. `write_copy()` opens a fresh document, authenticates if needed, stamps every target page, saves to a temp file in the destination directory, and `os.replace()`s it into place.

Key public types: `CheckMark`, `MappedMark`, `CopyStats`. Key entry points: `detect_checks`, `parse_page_range`, `project_mark`, `write_copy`, `render_page`, `composite_result_preview`.

### UI flow (`app.py`)

- `CheckmarkCopierApp` owns:
  - `self.doc`, `self.pdf_path`, `self.password`
  - `self.page_index` (0-based source page)
  - `self.marks`, `self.selected`
  - `self.target_pages` (0-based), `self.result_pos`
  - preview state (`source` / `result`, `zoom`, `zoom_mode`, `_page_image`, `_photo`)
- Loading a PDF sets an automatic target range, scans page 1, and draws the source preview.
- The source preview draws boxes plus actual vector strokes. The result preview is a PIL composite of the target page plus projected marks.
- The fixed footer card (`04 Copy & save`) must remain outside the scrolling sidebar so **Save a copy** is always visible.
- Saving runs in a worker thread; it communicates only through `queue.Queue`. Never touch Tk widgets from the worker.
- The output path defaults to `<input stem> - checkmarks.pdf` beside the input and is never allowed to equal the input path.

## PDF / coordinate invariants

These are the easiest places to introduce subtle bugs:

- Internal page numbers are **0-based**; user-facing page numbers, target ranges, and nav labels are **1-based**.
- `Page.get_drawings()` and annotation rects use the page's **unrotated** coordinate system. Use `unrotated_page_rect(page)` (implemented as `page.rect * page.derotation_matrix`) for mapping.
- Use `point_to_rotated` / `point_to_unrotated` / `rect_to_rotated_bbox` when drawing to the canvas; do not mix visible `page.rect` coordinates with drawing coordinates on rotated pages.
- PDF colours in this codebase are normalized floats in `[0, 1]`; Tk needs hex (`colour_to_hex`).
- Path rects from `get_drawings()` describe the path, not the stroked bounds. Do not assume the drawing rect includes the stroke.
- `Page.draw_polyline` and `Shape.finish` use `lineCap=1` (round) and `lineJoin=0` (miter) to match the Adobe mark. Do not change these independently of the template.
- Image marks are inserted with `alpha=-1`; preserve transparency, otherwise a white box will cover the target page.
- Rotated target pages are handled in unrotated page coordinates. A target with a different rotation will follow that coordinate system, not the visible orientation.

## Exact Adobe check geometry

The built-in mark lives in `adobe_check_mark()`:

```text
d = (width + height) / 20
(x0+d, y0+h/2) -> (x0+w/3, y0+h-d) -> (x0+w-d, y0+d)
```

- Round caps, miter join, default canvas size 12 × 12 pt.
- Default black; Adobe's selected blue is `#2175C8` (`core.ADOBE_BLUE`).
- `_adobe_template_mask()` must stay in sync with `adobe_check_mark()` because annotation classification compares appearance masks against that template.
- If the geometry changes, update `adobe_check_mark`, `_adobe_template_mask`, the X-template rejection logic if relevant, and `test_adobe_geometry` together.

## Detection details

Vector detection (`_vector_marks`):

- Accepts stroke-only paths with exactly two connected line segments, plus two separate one-line paths that share an elbow.
- `_is_check_points()` requires the elbow to be the lowest point, the right tip to be higher than the left point, a roughly square bounding box, plausible leg-length ratios, and stroke width relative to size.
- Max 500 marks per page (`MAX_MARKS_PER_PAGE`); single-line grouping is capped at 450 segments.

Annotation detection (`_annotation_marks`):

- Candidate types: `Stamp`, `FreeText`, `Ink`, `Widget`, `Polygon`, `PolyLine`.
- Renders the annotation at up to 240 px per side, derives an alpha mask (or one from a white background), and compares it with the Adobe check template.
- Acceptance: explicit check/tick text hint, or template IoU ≥ 0.47 (Widget threshold 0.37), with the X-template rejected.
- The crop rect comes from the ink bounding box, not the full annotation rect, so transparent padding is not copied.

Deduplication (`_dedupe`) always prefers a vector representation over an image representation of the same geometry. Preserve that preference.

## UI / code conventions

- Keep core logic free of Tkinter imports so it stays testable.
- Use `ttk`/`tk` styles defined in `_configure_styles()`; avoid one-off fonts and hard-coded card backgrounds where existing palette constants apply.
- Add new sidebar content inside `_build_sidebar`; keep the action/save card in the fixed footer.
- Any new image shown in Tk must keep a reference (`self._photo`) to avoid garbage collection.
- Use `messagebox` for actionable failures, `scan_status` for inline hints, and `LOG` for diagnostics.
- Logs go to `%LOCALAPPDATA%\CheckmarkCopier\app.log` via a `RotatingFileHandler`.
- No network calls, telemetry, cloud APIs, or external services.
- Keep Python 3.10 compatibility unless the build environment is deliberately upgraded.

## Tests and verification

Run the full suite before committing. The core tests generate PDFs at runtime and must not depend on user files or checked-in fixtures. When changing detection or stamping:

1. Add or update a test in `tests/test_core.py`.
2. Verify exact coordinates, not just “a mark exists”.
3. Verify the input file hash is unchanged.
4. Include rotation and image-annotation cases when touching coordinate or mask code.
5. For UI changes, run the smoke suite and, if possible, inspect a fresh screenshot with `--screenshot`.

The GUI smoke tests run with `root.withdraw()` and test the save worker; avoid adding tests that require a visible desktop.

`tests/test_core.py` uses the optional `pikepdf` package to construct an annotation-appearance fixture. The test is skipped when `pikepdf` is absent; install it (`python -m pip install pikepdf`) when changing annotation detection.

## Packaging notes

- PyInstaller command is in `build.ps1`; it uses `--onefile`, `--windowed`, `--add-data "assets;assets"`, and `version_info.txt`.
- `assets/app.ico` is the EXE icon; `assets/app_256.png` is the in-window icon. Both must be bundled.
- `--demo` is a development aid and resolves `samples/demo.pdf` relative to `app.py`; the demo PDF is not bundled into the EXE.
- In restricted Windows sandboxes, a one-file PyInstaller EXE can fail to create its extraction directory when launched from a deep workspace path. Test the built EXE from a short path (for example the Desktop) or use an onedir build as a fallback.
- `dist/` is git-ignored. Keep release artifacts out of commits unless the repository policy changes.

## Current scope

Supported: one source page, multiple checks on that page, target page ranges with source exclusion, same-size exact copies, proportional size mapping, rotated pages, password-protected PDFs, vector and small annotation appearances, demo/manual placement, and an offline single-file build.

Not supported: OCR or automatic detection of checks that are only pixels on a scanned page (manual placement is the fallback), automatic target-page editing, multi-source-page batches, or preserving digital signatures after modification.
