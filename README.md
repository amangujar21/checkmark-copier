# Checkmark Copier

A small, local Windows app that copies the check marks you placed with **Adobe Fill & Sign** from one PDF page to any range of other pages — at the same position and in the same style. The copies are written as native Fill & Sign objects, so Acrobat's Fill & Sign tool can still select and move them.

![Checkmark Copier](samples/screenshot-result.png)

## What it does

1. Open a PDF that already has check marks on one page.
2. Choose that source page. The app finds the checks automatically.
3. Review them. Click **Place checks** if you need to add any by hand.
4. Enter target pages (for example `2-10, 12`) and click **Save a copy**.

The original PDF is never changed. The app writes a new PDF beside it.

Every vector check is recreated as the same private Fill & Sign object tree Adobe writes: a marked-content block containing `/page`, `/fields` and `/check` forms, the `ADBE_FillSign` metadata, and the "Adobe Fill & Sign" optional-content group. That tree is what makes a mark selectable and movable in Acrobat; a plain drawn vector is not.

## The check mark

The built-in manual mark is the **same three-point check used by Adobe Fill & Sign**:

- start at the left mid-edge,
- down to the lower third,
- up to the upper-right,
- round line caps,
- stroke weight = `(width + height) / 20`,
- default size 12 × 12 pt, default colour black (Adobe's selected blue is `#2175C8`).

Detected marks are copied from the PDF itself, so each one keeps its actual vector points, width and colour. If a check exists only as a small PDF annotation image, the appearance is copied as a transparent image at up to 240 px per side.

Vector and manually placed checks are not drawn into the page. They are emitted as Adobe Fill & Sign page objects, preserving the same geometry and colour while keeping Fill & Sign's selection, resize and delete behaviour in Acrobat.

## Detection

The app looks for:

- **Vector check paths** — the normal Fill & Sign mark, common two-line checks, and checks split across two stroke paths.
- **Small annotations** — Stamp, FreeText, Ink and Widget appearances that have check-like geometry (or say “Check” in their annotation text).
- **Annotation appearance vectors** — when the PDF exposes them through its page drawings, the exact vector is used instead of an image.

It does **not** use OCR. Scanned pages are just pixels, so they cannot be found automatically. For those, use **Place checks** and click each position — the app still writes native Fill & Sign marks.

## Mapping and page sizes

For pages that are the same size and rotation (the normal case), the copy is point-for-point exact. If a target page has a different size, the mark's centre keeps the same fractional position and its size is scaled by the smaller page-axis ratio. This keeps the check square and on the page.

## Limits / notes

- Existing PDF digital signatures will be invalidated by adding page content. This tool always writes a new file; sign after copying if a signature is required.
- Password-protected PDFs are supported. The password is kept in memory for the save.
- Very unusual check shapes (for example a check drawn as a curved font outline) may need manual placement.
- If a check is detected only as a raster annotation image, it is copied as an image (the transparent appearance is preserved), but Acrobat cannot edit that image like a Fill & Sign vector mark.
- If the target page is rotated differently from the source page, marks follow the page's unrotated coordinate system rather than the visible orientation.
- No network access, telemetry or uploads. Everything runs locally.

## Running the EXE

`dist\CheckmarkCopier.exe` is a self-contained build. Windows 10/11 x64. No Python installation needed. The first launch can take a few seconds while the one-file bundle unpacks.

### "Could not create temporary directory!"

The one-file build unpacks itself into `%TEMP%` on every launch, so that folder has to exist and be writable. If `TEMP`/`TMP` is redirected, missing, read-only, or inside a sandbox that blocks writes, the app never starts and Windows shows that dialog — it comes from the packer, not from the app.

Any of these gets you going again:

- Run the portable build: `dist\CheckmarkCopier-portable\CheckmarkCopier.exe` (or unzip `dist\CheckmarkCopier-1.0.0-win64-portable.zip` and run the exe inside). A folder build runs in place and never touches `%TEMP%`, so it also works from a restricted or sandboxed shell.
- Or point `TEMP` and `TMP` at a normal writable folder (for example `C:\Users\<you>\AppData\Local\Temp`), then start the single file again.

## Building from source

Requires Python 3.10+ on Windows.

```powershell
python -m pip install -r requirements.txt
powershell -ExecutionPolicy Bypass -File .\build.ps1
```

`build.ps1` runs the tests, then builds both release shapes:

- `dist\CheckmarkCopier.exe` — one file, unpacks to `%TEMP%` at launch.
- `dist\CheckmarkCopier-portable\` — folder build, runs in place, no unpacking.

To build the one-file EXE by hand:

```powershell
python -m PyInstaller --noconfirm --clean --onefile --windowed `
  --name CheckmarkCopier --icon assets/app.ico `
  --add-data "assets;assets" --version-file version_info.txt app.py
```

The result is `dist\CheckmarkCopier.exe`. Swap `--onefile` for `--onedir` to get the portable folder instead.

If PowerShell refuses to run the helper script, call it through `powershell -ExecutionPolicy Bypass -File .\build.ps1`.

## Tests

```powershell
python -m unittest discover -s tests -v
```

The tests create synthetic PDFs and verify detection, exact-position copying, rotation handling, annotation appearances, range parsing, and the GUI save path. The core suite also checks that copied marks contain Adobe's native Fill & Sign page-object tree, not just visible vectors.

## Files

- `app.py` — Tkinter GUI.
- `checkmark_core.py` — PDF detection, projection, drawing and save logic.
- `samples/demo.pdf` — six-page demo checklist with four checks on page 1.
- `tests/` — core and GUI smoke tests.

## Disclaimer

Not affiliated with or endorsed by Adobe. Adobe, Acrobat and Fill & Sign are trademarks of Adobe Inc. The check geometry was reimplemented from observed behaviour and the shipped Fill & Sign implementation for interoperability; no Adobe code is included.
