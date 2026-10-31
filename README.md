# Checkmark Copier

A small, local Windows app that copies the check marks you placed with **Adobe Fill & Sign** from one PDF page to any range of other pages — at the same position and in the same style.

![Checkmark Copier](samples/screenshot-result.png)

## What it does

1. Open a PDF that already has check marks on one page.
2. Choose that source page. The app finds the checks automatically.
3. Review them. Click **Place checks** if you need to add any by hand.
4. Enter target pages (for example `2-10, 12`) and click **Save a copy**.

The original PDF is never changed. The app writes a new PDF beside it.

## The check mark

The built-in manual mark is the **same three-point check used by Adobe Fill & Sign**:

- start at the left mid-edge,
- down to the lower third,
- up to the upper-right,
- round line caps,
- stroke weight = `(width + height) / 20`,
- default size 12 × 12 pt, default colour black (Adobe's selected blue is `#2175C8`).

Detected marks are copied from the PDF itself, so each one keeps its actual vector points, width and colour. If a check exists only as a small PDF annotation image, the appearance is copied as a transparent image at up to 240 px per side.

## Detection

The app looks for:

- **Vector check paths** — the normal Fill & Sign mark, common two-line checks, and checks split across two stroke paths.
- **Small annotations** — Stamp, FreeText, Ink and Widget appearances that have check-like geometry (or say “Check” in their annotation text).
- **Annotation appearance vectors** — when the PDF exposes them through its page drawings, the exact vector is used instead of an image.

It does **not** use OCR. Scanned pages are just pixels, so they cannot be found automatically. For those, use **Place checks** and click each position — the app still writes exact vector marks.

## Mapping and page sizes

For pages that are the same size and rotation (the normal case), the copy is point-for-point exact. If a target page has a different size, the mark's centre keeps the same fractional position and its size is scaled by the smaller page-axis ratio. This keeps the check square and on the page.

## Limits / notes

- Existing PDF digital signatures will be invalidated by adding page content. This tool always writes a new file; sign after copying if a signature is required.
- Password-protected PDFs are supported. The password is kept in memory for the save.
- Very unusual check shapes (for example a check drawn as a curved font outline) may need manual placement.
- If the target page is rotated differently from the source page, marks follow the page's unrotated coordinate system rather than the visible orientation.
- No network access, telemetry or uploads. Everything runs locally.

## Running the EXE

`dist\CheckmarkCopier.exe` is a self-contained build. Windows 10/11 x64. No Python installation needed. The first launch can take a few seconds while the one-file bundle unpacks.

## Building from source

Requires Python 3.10+ on Windows.

```powershell
python -m pip install -r requirements.txt
python -m PyInstaller --noconfirm --clean --onefile --windowed `
  --name CheckmarkCopier --icon assets/app.ico `
  --add-data "assets;assets" --version-file version_info.txt app.py
```

The result is `dist\CheckmarkCopier.exe`.

If PowerShell refuses to run the helper script, use:

```powershell
powershell -ExecutionPolicy Bypass -File .\build.ps1
```

## Tests

```powershell
python -m unittest discover -s tests -v
```

The tests create synthetic PDFs and verify detection, exact-position copying, rotation handling, annotation appearances, range parsing, and the GUI save path.

## Files

- `app.py` — Tkinter GUI.
- `checkmark_core.py` — PDF detection, projection, drawing and save logic.
- `samples/demo.pdf` — six-page demo checklist with four checks on page 1.
- `tests/` — core and GUI smoke tests.

## Disclaimer

Not affiliated with or endorsed by Adobe. Adobe, Acrobat and Fill & Sign are trademarks of Adobe Inc. The check geometry was reimplemented from observed behaviour and the shipped Fill & Sign implementation for interoperability; no Adobe code is included.
