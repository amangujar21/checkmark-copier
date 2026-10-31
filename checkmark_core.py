"""
Core PDF logic for Checkmark Copier.

The built-in check mark reproduces the geometry used by Adobe Fill & Sign's
"Checkmark" tool: a three-point polyline from the left mid-edge, down to the
lower third, and up to the upper right, stroked with round caps.  The stroke
weight is one twentieth of the combined width and height, matching the
`faf-main.js` implementation shipped with Acrobat.

Detection deliberately works on PDF geometry, not OCR: checks created by
Fill & Sign (or by flattening a Fill & Sign PDF) are vector paths or small
annotation appearances.  Manual placement covers scanned/rasterised pages.
"""

from __future__ import annotations

import io
import logging
import math
import os
import re
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace

import fitz
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

LOG = logging.getLogger(__name__)

SKIP_ANNOT_TYPES = {
    "Link",
    "Popup",
    "Text",
    "FileAttachment",
    "Sound",
    "Movie",
    "Screen",
    "PrinterMark",
    "TrapNet",
    "Watermark",
    "3D",
    "Redact",
}

ANNOT_TYPES = {"Stamp", "FreeText", "Ink", "Widget", "Polygon", "PolyLine"}

ADOBE_BLUE = (0x21 / 255, 0x75 / 255, 0xC8 / 255)
ADOBE_RED = (0xE5 / 255, 0x22 / 255, 0x37 / 255)
BLACK = (0.0, 0.0, 0.0)

MAX_MARKS_PER_PAGE = 500


class PdfPasswordError(RuntimeError):
    """Raised when a PDF needs a password that was not supplied / was wrong."""


class PdfOpenError(RuntimeError):
    """Raised when a PDF cannot be opened."""


@dataclass
class CheckMark:
    """One check mark discovered or placed on a source page."""

    kind: str  # "vector" or "image"
    rect: fitz.Rect
    points: tuple[tuple[float, float], ...] = ()
    width: float = 0.0
    color: tuple[float, float, float] = BLACK
    png: bytes | None = None
    source: str = "vector"
    confidence: float = 1.0
    enabled: bool = True

    def clone(self) -> CheckMark:
        return replace(self, rect=fitz.Rect(self.rect))

    @property
    def size(self) -> float:
        return max(self.rect.width, self.rect.height)

    @property
    def center(self) -> fitz.Point:
        return fitz.Point((self.rect.x0 + self.rect.x1) / 2, (self.rect.y0 + self.rect.y1) / 2)

    def move_by(self, dx: float, dy: float) -> None:
        self.rect = fitz.Rect(
            self.rect.x0 + dx,
            self.rect.y0 + dy,
            self.rect.x1 + dx,
            self.rect.y1 + dy,
        )
        if self.kind == "vector" and self.points:
            self.points = tuple((p[0] + dx, p[1] + dy) for p in self.points)


@dataclass
class MappedMark:
    kind: str
    rect: fitz.Rect
    points: tuple[tuple[float, float], ...] = ()
    width: float = 0.0
    color: tuple[float, float, float] = BLACK
    png: bytes | None = None


@dataclass
class CopyStats:
    output_path: str
    page_count: int
    mark_count: int
    stamp_count: int


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------


def unrotated_page_rect(page: fitz.Page) -> fitz.Rect:
    """The page's visible rect in the coordinate system used by get_drawings."""
    return page.rect * page.derotation_matrix


def point_to_rotated(page: fitz.Page, point: Sequence[float]) -> fitz.Point:
    return fitz.Point(point) * page.rotation_matrix


def point_to_unrotated(page: fitz.Page, point: Sequence[float]) -> fitz.Point:
    return fitz.Point(point) * page.derotation_matrix


def rect_to_rotated_bbox(page: fitz.Page, rect: fitz.Rect) -> fitz.Rect:
    pts = [
        point_to_rotated(page, (rect.x0, rect.y0)),
        point_to_rotated(page, (rect.x1, rect.y0)),
        point_to_rotated(page, (rect.x0, rect.y1)),
        point_to_rotated(page, (rect.x1, rect.y1)),
    ]
    xs = [p.x for p in pts]
    ys = [p.y for p in pts]
    return fitz.Rect(min(xs), min(ys), max(xs), max(ys))


def adobe_check_mark(
    center: Sequence[float] | fitz.Point,
    size: float = 12.0,
    color: Sequence[float] = BLACK,
    width: float | None = None,
) -> CheckMark:
    """Build the exact Fill & Sign checkmark as a vector mark."""

    c = fitz.Point(center)
    size = max(2.0, float(size))
    w = h = size
    x0, y0 = c.x - w / 2, c.y - h / 2
    d = (w + h) / 20.0
    points = (
        (x0 + d, y0 + h / 2),
        (x0 + w / 3.0, y0 + h - d),
        (x0 + w - d, y0 + d),
    )
    rect = fitz.Rect(
        min(p[0] for p in points),
        min(p[1] for p in points),
        max(p[0] for p in points),
        max(p[1] for p in points),
    )
    return CheckMark(
        kind="vector",
        rect=rect,
        points=points,
        width=float(width if width is not None else d),
        color=tuple(float(v) for v in color),
        source="built-in",
        confidence=1.0,
    )


def _is_check_points(
    a: Sequence[float],
    b: Sequence[float],
    c: Sequence[float],
    stroke_width: float,
) -> bool:
    """Recognise a normal (left-to-right) check mark.

    ``b`` is the elbow.  The left point is roughly mid-height, the elbow is
    the lowest point, and the right point is the highest point.
    """

    ax, ay = float(a[0]), float(a[1])
    bx, by = float(b[0]), float(b[1])
    cx, cy = float(c[0]), float(c[1])

    xs = (ax, bx, cx)
    ys = (ay, by, cy)
    w = max(xs) - min(xs)
    h = max(ys) - min(ys)
    if w < 4.0 or h < 4.0:
        return False
    ratio = w / h
    if not (0.50 <= ratio <= 2.0):
        return False

    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)

    # Normal orientation: elbow lowest, right tip highest, left point in between.
    if not (by > ay + 0.03 * h and by > cy + 0.03 * h):
        return False
    if not (cx > ax + 0.15 * w and bx > ax and bx < cx):
        return False
    if not (cy < ay - 0.05 * h):
        return False

    # The three vertices should span the path's own bounding box.
    if ax - x0 > 0.28 * w or x1 - cx > 0.28 * w:
        return False
    if y1 - by > 0.28 * h or cy - y0 > 0.28 * h:
        return False

    l1 = math.dist((ax, ay), (bx, by))
    l2 = math.dist((bx, by), (cx, cy))
    if l1 <= 0.5 or l2 <= 0.5:
        return False
    if not (0.16 <= l1 / l2 <= 0.92):
        return False

    v1 = (ax - bx, ay - by)
    v2 = (cx - bx, cy - by)
    denom = math.hypot(*v1) * math.hypot(*v2)
    if denom <= 0:
        return False
    cos_angle = (v1[0] * v2[0] + v1[1] * v2[1]) / denom
    if not (-0.05 <= cos_angle <= 0.92):
        return False

    if stroke_width > 0:
        if not (0.015 * w <= stroke_width <= 0.36 * w):
            return False
    return True


def _drawing_color(d: dict) -> tuple[float, float, float] | None:
    col = d.get("color")
    if col is None:
        return None
    try:
        if len(col) == 1:
            return (float(col[0]),) * 3
        if len(col) >= 3:
            return (float(col[0]), float(col[1]), float(col[2]))
    except (TypeError, ValueError):
        return None
    return None


def _mark_from_points(
    points: Sequence[Sequence[float]],
    width: float,
    color: Sequence[float],
    source: str = "vector",
    confidence: float = 1.0,
) -> CheckMark:
    pts = tuple((float(p[0]), float(p[1])) for p in points)
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    rect = fitz.Rect(min(xs), min(ys), max(xs), max(ys))
    return CheckMark(
        kind="vector",
        rect=rect,
        points=pts,
        width=max(0.15, float(width)),
        color=tuple(float(v) for v in color),
        source=source,
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# Vector detection
# ---------------------------------------------------------------------------


def _vector_marks(page: fitz.Page, notes: list[str]) -> list[CheckMark]:
    try:
        drawings = page.get_drawings()
    except Exception as exc:  # malformed page content
        LOG.warning("get_drawings failed on page %s: %s", page.number + 1, exc)
        notes.append("Could not read vector drawings on this page.")
        return []

    marks: list[CheckMark] = []
    one_line: list[dict] = []

    for d in drawings:
        try:
            if d.get("type") != "s":
                continue
            if d.get("fill") not in (None, ()):
                continue
            width = float(d.get("width") or 0.0)
            if width <= 0 or width > 8:
                continue
            color = _drawing_color(d)
            if color is None:
                continue
            items = d.get("items") or []
            if len(items) == 2 and items[0][0] == "l" and items[1][0] == "l":
                a = items[0][1]
                b = items[0][2]
                c = items[1][2]
                if _is_check_points(a, b, c, width):
                    marks.append(
                        _mark_from_points(
                            ((a.x, a.y), (b.x, b.y), (c.x, c.y)),
                            width,
                            color,
                        )
                    )
            elif len(items) == 1 and items[0][0] == "l":
                p1, p2 = items[0][1], items[0][2]
                if math.dist((p1.x, p1.y), (p2.x, p2.y)) >= 2.0:
                    one_line.append(
                        {
                            "p1": (float(p1.x), float(p1.y)),
                            "p2": (float(p2.x), float(p2.y)),
                            "width": width,
                            "color": color,
                        }
                    )
        except Exception:
            continue

    # Some producers split the two legs into separate stroke paths.
    if one_line:
        if len(one_line) > 450:
            notes.append("Many single-line paths on this page; check detection may be partial.")
            one_line = one_line[:450]
        seen_pairs: set[tuple[int, int]] = set()
        for i in range(len(one_line)):
            s1 = one_line[i]
            for j in range(i + 1, len(one_line)):
                s2 = one_line[j]
                if (i, j) in seen_pairs:
                    continue
                if abs(s1["width"] - s2["width"]) > max(0.2, 0.12 * max(s1["width"], s2["width"])):
                    continue
                if max(abs(a - b) for a, b in zip(s1["color"], s2["color"])) > 0.06:
                    continue

                # Find a shared endpoint.
                endpoints1 = (s1["p1"], s1["p2"])
                endpoints2 = (s2["p1"], s2["p2"])
                shared = None
                for k1, e1 in enumerate(endpoints1):
                    for k2, e2 in enumerate(endpoints2):
                        if math.dist(e1, e2) <= 1.2:
                            shared = (k1, k2)
                            break
                    if shared:
                        break
                if not shared:
                    continue
                k1, k2 = shared
                elbow = endpoints1[k1]
                a = endpoints1[1 - k1]
                c = endpoints2[1 - k2]
                if _is_check_points(a, elbow, c, s1["width"]):
                    marks.append(
                        _mark_from_points(
                            (a, elbow, c),
                            s1["width"],
                            s1["color"],
                            source="vector",
                            confidence=0.95,
                        )
                    )
                    seen_pairs.add((i, j))
                    break

    return marks


def _rect_iou(a: fitz.Rect, b: fitz.Rect) -> float:
    inter = a & b
    if inter.is_empty:
        return 0.0
    ia = inter.width * inter.height
    if ia <= 0:
        return 0.0
    ua = a.width * a.height + b.width * b.height - ia
    return ia / ua if ua > 0 else 0.0


def _contains_point(rect: fitz.Rect, point: fitz.Point) -> bool:
    return rect.x0 <= point.x <= rect.x1 and rect.y0 <= point.y <= rect.y1


def _dedupe(marks: list[CheckMark]) -> list[CheckMark]:
    """Remove duplicates, preferring exact vector geometry over raster images.

    PyMuPDF exposes annotation appearance paths through ``get_drawings`` on
    many files, so a vector check and an image fallback for the same annotation
    can both appear.  The vector is always the better copy.
    """

    vectors = [m for m in marks if m.kind == "vector"]
    images = [m for m in marks if m.kind == "image"]
    others = [m for m in marks if m.kind not in ("vector", "image")]

    def dedupe_same(items: list[CheckMark]) -> list[CheckMark]:
        out: list[CheckMark] = []
        for m in items:
            dup = False
            for idx, existing in enumerate(out):
                if _rect_iou(existing.rect, m.rect) > 0.72:
                    if existing.confidence >= m.confidence:
                        dup = True
                        break
                    out[idx] = m
                    dup = True
                    break
            if not dup:
                out.append(m)
        return out

    vectors = dedupe_same(vectors)
    images = dedupe_same(images)

    kept_images: list[CheckMark] = []
    for im in images:
        covered = False
        for v in vectors:
            iou = _rect_iou(im.rect, v.rect)
            contains = _contains_point(im.rect, v.center)
            if iou > 0.45 or (contains and im.rect.get_area() < 4.0 * max(1.0, v.rect.get_area())):
                covered = True
                break
        if not covered:
            kept_images.append(im)

    out = vectors + kept_images + others
    out.sort(key=lambda m: (round(m.rect.y0, 1), round(m.rect.x0, 1)))
    return out


# ---------------------------------------------------------------------------
# Annotation detection
# ---------------------------------------------------------------------------

_TEMPLATE_CACHE: dict[int, np.ndarray] = {}
_TEMPLATE_X_CACHE: dict[int, np.ndarray] = {}


def _normalise_mask(mask: np.ndarray, out: int = 64) -> np.ndarray:
    """Crop a boolean ink mask, pad it square, and resize for comparison."""
    if mask.dtype != bool:
        mask = mask > 0
    ys, xs = np.where(mask)
    if len(xs) == 0:
        return np.zeros((out, out), dtype=bool)
    crop = mask[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1]
    im = Image.fromarray(crop.astype(np.uint8) * 255, "L")
    side = max(im.width, im.height)
    margin = max(2, int(round(side * 0.10)))
    square = Image.new("L", (side + 2 * margin, side + 2 * margin), 0)
    square.paste(im, ((square.width - im.width) // 2, (square.height - im.height) // 2))
    square = square.resize((out, out), Image.Resampling.LANCZOS)
    return np.asarray(square) > 55


def _dilate_mask(mask: np.ndarray, radius: int = 1) -> np.ndarray:
    im = Image.fromarray(mask.astype(np.uint8) * 255, "L")
    im = im.filter(ImageFilter.MaxFilter(radius * 2 + 1))
    return np.asarray(im) > 0


def _adobe_template_mask(out: int = 64) -> np.ndarray:
    if out in _TEMPLATE_CACHE:
        return _TEMPLATE_CACHE[out]
    scale = 4
    size = out * scale
    im = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(im)
    stroke = max(2, int(round((size + size) / 20.0)))
    pts = [
        (stroke, size / 2),
        (size / 3.0, size - stroke),
        (size - stroke, stroke),
    ]
    d.line(pts, fill=255, width=stroke, joint="curve")
    for x, y in (pts[0], pts[-1]):
        r = stroke / 2
        d.ellipse((x - r, y - r, x + r, y + r), fill=255)
    mask = np.asarray(im.resize((out, out), Image.Resampling.LANCZOS)) > 55
    _TEMPLATE_CACHE[out] = _normalise_mask(mask, out)
    return _TEMPLATE_CACHE[out]


def _x_template_mask(out: int = 64) -> np.ndarray:
    if out in _TEMPLATE_X_CACHE:
        return _TEMPLATE_X_CACHE[out]
    scale = 4
    size = out * scale
    im = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(im)
    stroke = max(2, int(round(size / 11.0)))
    d.line([(stroke, stroke), (size - stroke, size - stroke)], fill=255, width=stroke)
    d.line([(size - stroke, stroke), (stroke, size - stroke)], fill=255, width=stroke)
    mask = np.asarray(im.resize((out, out), Image.Resampling.LANCZOS)) > 55
    _TEMPLATE_X_CACHE[out] = _normalise_mask(mask, out)
    return _TEMPLATE_X_CACHE[out]


def _mask_similarity(mask: np.ndarray, template: np.ndarray) -> float:
    a = _dilate_mask(_normalise_mask(mask), 1)
    b = _dilate_mask(template, 1)
    union = np.logical_or(a, b).sum()
    if union == 0:
        return 0.0
    return float(np.logical_and(a, b).sum() / union)


def _check_similarity(mask: np.ndarray) -> tuple[float, float]:
    normal = _normalise_mask(mask)
    sim = _mask_similarity(normal, _adobe_template_mask())
    xsim = _mask_similarity(normal, _x_template_mask())
    return sim, xsim


def _has_check_hint(text: str) -> bool:
    if not text:
        return False
    t = text.casefold()
    if any(ch in text for ch in ("\u2713", "\u2714", "\u221a")):
        return True
    return bool(re.search(r"\b(check(?:mark|ed)?|tick(?:ed)?)\b", t))


def _annotation_pixmap(annot: fitz.Annot, rect: fitz.Rect) -> tuple[fitz.Pixmap, float, float]:
    span = max(rect.width, rect.height, 1.0)
    zoom = min(24.0, max(6.0, 240.0 / span))
    pix = annot.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=True)
    if pix.width * pix.height > 900_000:
        factor = math.sqrt(900_000 / (pix.width * pix.height))
        zoom *= factor
        pix = annot.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=True)
    sx = pix.width / rect.width if rect.width else zoom
    sy = pix.height / rect.height if rect.height else zoom
    return pix, sx, sy


def _image_mark_from_annot(
    annot: fitz.Annot,
    rect: fitz.Rect,
    type_name: str,
) -> tuple[CheckMark | None, str]:
    try:
        pix, zx, zy = _annotation_pixmap(annot, rect)
    except Exception as exc:
        LOG.debug("annot pixmap failed: %s", exc)
        return None, ""

    if pix.width < 8 or pix.height < 8:
        return None, ""

    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    if pix.n == 4:
        rgb = arr[:, :, :3].astype(np.int16)
        alpha = arr[:, :, 3].astype(np.float32)
        transparent = alpha.min() < 245 and alpha.max() > 10
        if not transparent:
            dist = np.max(255 - rgb, axis=2).astype(np.float32)
            alpha = np.clip(dist * 1.25, 0, 255)
    elif pix.n == 3:
        rgb = arr[:, :, :3].astype(np.int16)
        dist = np.max(255 - rgb, axis=2).astype(np.float32)
        alpha = np.clip(dist * 1.25, 0, 255)
    else:
        gray = arr[:, :, 0].astype(np.int16)
        alpha = np.clip((255 - gray) * 1.25, 0, 255)
        rgb = np.stack([gray, gray, gray], axis=2).astype(np.int16)

    mask = alpha > 16
    ys, xs = np.where(mask)
    if len(xs) < 12:
        return None, ""

    bx0, bx1 = int(xs.min()), int(xs.max()) + 1
    by0, by1 = int(ys.min()), int(ys.max()) + 1
    pad = 1
    bx0 = max(0, bx0 - pad)
    by0 = max(0, by0 - pad)
    bx1 = min(pix.width, bx1 + pad)
    by1 = min(pix.height, by1 + pad)

    mask_crop = mask[by0:by1, bx0:bx1]
    alpha_crop = alpha[by0:by1, bx0:bx1]
    if mask_crop.size == 0:
        return None, ""
    density = float(mask_crop.mean())
    if density < 0.012 or density > 0.68:
        return None, ""

    sim, xsim = _check_similarity(mask_crop)

    info = {}
    try:
        info = annot.info or {}
    except Exception:
        pass
    hint_text = " ".join(
        str(info.get(k, "")) for k in ("content", "subject", "title")
    )
    hinted = _has_check_hint(hint_text)

    accept = False
    if hinted or sim >= 0.47 and sim >= xsim + 0.035 or type_name == "Widget" and sim >= 0.37 and sim >= xsim:
        accept = True

    if not accept:
        LOG.debug(
            "Rejected annotation %s %.1fx%.1f sim=%.3f xsim=%.3f density=%.3f",
            type_name,
            rect.width,
            rect.height,
            sim,
            xsim,
            density,
        )
        return None, ""

    rgba = np.empty((mask_crop.shape[0], mask_crop.shape[1], 4), dtype=np.uint8)
    if pix.n >= 3:
        rgba[:, :, :3] = rgb[by0:by1, bx0:bx1].clip(0, 255).astype(np.uint8)
    else:
        rgba[:, :, :3] = rgb[by0:by1, bx0:bx1].clip(0, 255).astype(np.uint8)
    rgba[:, :, 3] = np.clip(alpha_crop, 0, 255).astype(np.uint8)

    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, "PNG", optimize=True)
    png = buf.getvalue()

    crop_rect = fitz.Rect(
        rect.x0 + bx0 / zx,
        rect.y0 + by0 / zy,
        rect.x0 + bx1 / zx,
        rect.y0 + by1 / zy,
    )

    label = {
        "Stamp": "Stamp annotation",
        "FreeText": "Text annotation",
        "Ink": "Ink annotation",
        "Widget": "Form field",
        "Polygon": "Annotation",
        "PolyLine": "Annotation",
    }.get(type_name, f"{type_name} annotation")

    conf = max(sim, 0.9 if hinted else sim)
    mark = CheckMark(
        kind="image",
        rect=crop_rect,
        png=png,
        source=label,
        confidence=float(min(1.0, conf)),
    )
    return mark, hint_text


def _annotation_marks(page: fitz.Page, notes: list[str]) -> list[CheckMark]:
    marks: list[CheckMark] = []
    try:
        annots = list(page.annots() or [])
    except Exception as exc:
        LOG.warning("page.annots failed on page %s: %s", page.number + 1, exc)
        return marks

    for annot in annots:
        try:
            type_name = annot.type[1] if annot.type else ""
            if type_name in SKIP_ANNOT_TYPES or type_name not in ANNOT_TYPES:
                continue
            rect = fitz.Rect(annot.rect)
            if rect.width < 4 or rect.height < 4:
                continue
            if max(rect.width, rect.height) > 140:
                continue
            mark, _ = _image_mark_from_annot(annot, rect, type_name)
            if mark:
                marks.append(mark)
        except Exception as exc:
            LOG.debug("annotation skipped: %s", exc)
            continue
    return marks


# ---------------------------------------------------------------------------
# Public detection
# ---------------------------------------------------------------------------


def detect_checks(page: fitz.Page, include_annotations: bool = True) -> list[CheckMark]:
    """Return check marks on one page, sorted top-to-bottom / left-to-right."""

    notes: list[str] = []
    marks = _vector_marks(page, notes)
    if include_annotations:
        marks.extend(_annotation_marks(page, notes))
    marks = _dedupe(marks)
    marks.sort(key=lambda m: (round(m.rect.y0, 1), round(m.rect.x0, 1)))
    if len(marks) > MAX_MARKS_PER_PAGE:
        marks = marks[:MAX_MARKS_PER_PAGE]
    return marks


# ---------------------------------------------------------------------------
# Page-range parsing
# ---------------------------------------------------------------------------


def parse_page_range(spec: str, page_count: int) -> list[int]:
    """Parse a 1-based page range such as ``2-5, 9, 12-end``."""

    if page_count < 1:
        raise ValueError("The PDF has no pages.")
    raw = (spec or "").strip()
    if not raw:
        raise ValueError("Enter a target page range.")
    raw = raw.replace("\u2013", "-").replace("\u2014", "-").replace(" ", "")
    if raw.casefold() in ("all", "*"):
        return list(range(1, page_count + 1))

    pages: list[int] = []
    for token in re.split(r"[,;]+", raw):
        if not token:
            continue
        if "-" in token:
            parts = token.split("-")
            if len(parts) != 2:
                raise ValueError(f"Invalid page range: {token}")
            a_s, b_s = parts
            if a_s.casefold() in ("", "start"):
                a = 1
            else:
                a = int(a_s)
            if b_s.casefold() in ("", "end"):
                b = page_count
            else:
                b = int(b_s)
            if a < 1 or b < 1 or a > page_count or b > page_count:
                raise ValueError(
                    f"Page {a if a < 1 or a > page_count else b} is outside 1-{page_count}."
                )
            if a > b:
                raise ValueError(f"Range start is greater than end: {token}")
            pages.extend(range(a, b + 1))
        else:
            n = int(token)
            if n < 1 or n > page_count:
                raise ValueError(f"Page {n} is outside 1-{page_count}.")
            pages.append(n)
    if not pages:
        raise ValueError("Enter at least one target page.")
    return sorted(set(pages))


# ---------------------------------------------------------------------------
# Projection and stamping
# ---------------------------------------------------------------------------


def project_mark(
    mark: CheckMark,
    src_page: fitz.Page,
    dst_page: fitz.Page,
    mode: str = "proportional",
) -> MappedMark:
    """Map a source-page mark onto another page.

    ``proportional`` maps the mark's centre to the same fractional position
    and scales its size by the smaller page-axis ratio.  For equal-sized pages
    this is an exact point-for-point copy.  ``absolute`` keeps point units and
    offsets by the top-left of each visible page.
    """

    src = unrotated_page_rect(src_page)
    dst = unrotated_page_rect(dst_page)

    if mode == "absolute":
        dx = dst.x0 - src.x0
        dy = dst.y0 - src.y0
        scale = 1.0
    else:
        sx = dst.width / src.width if src.width else 1.0
        sy = dst.height / src.height if src.height else 1.0
        scale = max(0.05, min(sx, sy))
        cx = (mark.rect.x0 + mark.rect.x1) / 2
        cy = (mark.rect.y0 + mark.rect.y1) / 2
        relx = (cx - src.x0) / src.width if src.width else 0.5
        rely = (cy - src.y0) / src.height if src.height else 0.5
        new_cx = dst.x0 + relx * dst.width
        new_cy = dst.y0 + rely * dst.height
        dx = new_cx - cx
        dy = new_cy - cy

    if mark.kind == "vector" and mark.points:
        pts = tuple(
            (
                (p[0] - (mark.rect.x0 + mark.rect.x1) / 2) * scale
                + (mark.rect.x0 + mark.rect.x1) / 2
                + dx,
                (p[1] - (mark.rect.y0 + mark.rect.y1) / 2) * scale
                + (mark.rect.y0 + mark.rect.y1) / 2
                + dy,
            )
            for p in mark.points
        )
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        rect = fitz.Rect(min(xs), min(ys), max(xs), max(ys))
        return MappedMark(
            kind="vector",
            rect=rect,
            points=pts,
            width=max(0.1, mark.width * scale),
            color=mark.color,
        )

    # Image mark: scale around its centre and preserve aspect ratio.
    cx = (mark.rect.x0 + mark.rect.x1) / 2 + dx
    cy = (mark.rect.y0 + mark.rect.y1) / 2 + dy
    w = mark.rect.width * scale
    h = mark.rect.height * scale
    rect = fitz.Rect(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)
    return MappedMark(kind="image", rect=rect, png=mark.png)


def _stamp_page(
    page: fitz.Page,
    marks: Sequence[CheckMark],
    src_page: fitz.Page,
    mode: str,
) -> int:
    mapped = [project_mark(m, src_page, page, mode) for m in marks if m.enabled]
    vector = [m for m in mapped if m.kind == "vector" and len(m.points) >= 2]
    images = [m for m in mapped if m.kind == "image" and m.png]

    if vector:
        shape = page.new_shape()
        for m in vector:
            shape.draw_polyline([fitz.Point(p) for p in m.points])
            shape.finish(
                color=m.color,
                width=m.width,
                lineCap=1,
                lineJoin=0,
                closePath=False,
            )
        shape.commit(overlay=True)

    for m in images:
        if m.rect.width < 0.5 or m.rect.height < 0.5:
            continue
        page.insert_image(
            m.rect,
            stream=m.png,
            overlay=True,
            keep_proportion=True,
            alpha=-1,
        )
    return len(vector) + len(images)


def write_copy(
    input_path: str,
    output_path: str,
    source_page_index: int,
    target_pages: Iterable[int],
    marks: Sequence[CheckMark],
    mode: str = "proportional",
    password: str | None = None,
) -> CopyStats:
    """Write a new PDF with the marks copied to every target page.

    ``target_pages`` are 0-based page indexes.  The input file is never
    modified; output is written to a temporary sibling then atomically moved.
    """

    if os.path.abspath(input_path) == os.path.abspath(output_path):
        raise ValueError("Choose a different output file; the original is never modified.")

    doc = fitz.open(input_path)
    tmp_path: str | None = None
    try:
        if doc.needs_pass:
            if not password or not doc.authenticate(password):
                raise PdfPasswordError("This PDF is password-protected.")
        if doc.page_count < 1:
            raise PdfOpenError("This PDF has no pages.")
        if source_page_index < 0 or source_page_index >= doc.page_count:
            raise ValueError("Source page is outside the document.")

        targets = sorted({int(p) for p in target_pages})
        if not targets:
            raise ValueError("No target pages were selected.")
        for p in targets:
            if p < 0 or p >= doc.page_count:
                raise ValueError(f"Target page {p + 1} is outside the document.")

        src_page = doc[source_page_index]
        enabled = [m for m in marks if m.enabled]
        if not enabled:
            raise ValueError("No check marks are selected.")

        stamp_count = 0
        for p in targets:
            stamp_count += _stamp_page(doc[p], enabled, src_page, mode)

        out_dir = os.path.dirname(os.path.abspath(output_path)) or "."
        os.makedirs(out_dir, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=".checkmark-", suffix=".pdf", dir=out_dir)
        os.close(fd)
        doc.save(
            tmp_path,
            garbage=3,
            deflate=True,
            encryption=fitz.PDF_ENCRYPT_KEEP,
            clean=False,
        )
        doc.close()
        doc = None
        os.replace(tmp_path, output_path)
        tmp_path = None
        return CopyStats(
            output_path=output_path,
            page_count=len(targets),
            mark_count=len(enabled),
            stamp_count=stamp_count,
        )
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Preview rendering helpers
# ---------------------------------------------------------------------------


def render_page(page: fitz.Page, zoom: float) -> Image.Image:
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False, annots=True)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def composite_result_preview(
    page: fitz.Page,
    src_page: fitz.Page,
    marks: Sequence[CheckMark],
    zoom: float,
    mode: str = "proportional",
) -> Image.Image:
    """Render a target page with the projected marks painted on top."""

    base = render_page(page, zoom).convert("RGBA")
    mapped = [project_mark(m, src_page, page, mode) for m in marks if m.enabled]

    # Vector marks first.
    draw_layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(draw_layer)
    for m in mapped:
        if m.kind != "vector" or len(m.points) < 2:
            continue
        pts = []
        for p in m.points:
            rp = point_to_rotated(page, p)
            pts.append((rp.x * zoom, rp.y * zoom))
        w = max(1, int(round(m.width * zoom)))
        col = tuple(int(round(v * 255)) for v in m.color) + (255,)
        d.line(pts, fill=col, width=w, joint="curve")
        for x, y in (pts[0], pts[-1]):
            r = w / 2
            d.ellipse((x - r, y - r, x + r, y + r), fill=col)

    # Image marks.
    for m in mapped:
        if m.kind != "image" or not m.png:
            continue
        try:
            im = Image.open(io.BytesIO(m.png)).convert("RGBA")
            rb = rect_to_rotated_bbox(page, m.rect)
            tw = max(1, int(round(rb.width * zoom)))
            th = max(1, int(round(rb.height * zoom)))
            im = im.resize((tw, th), Image.Resampling.LANCZOS)
            draw_layer.alpha_composite(im, (int(round(rb.x0 * zoom)), int(round(rb.y0 * zoom))))
        except Exception:
            continue

    return Image.alpha_composite(base, draw_layer).convert("RGB")


def colour_to_hex(colour: Sequence[float]) -> str:
    return "#" + "".join(f"{max(0, min(255, int(round(c * 255)))):02X}" for c in colour[:3])


def hex_to_colour(value: str) -> tuple[float, float, float]:
    v = value.strip().lstrip("#")
    if len(v) == 3:
        v = "".join(ch * 2 for ch in v)
    if len(v) != 6:
        raise ValueError("Invalid colour")
    return (int(v[0:2], 16) / 255, int(v[2:4], 16) / 255, int(v[4:6], 16) / 255)
