from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import unittest

import fitz
from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import checkmark_core as core


def make_pdf(path: str, pages: int = 3, width: float = 400, height: float = 300, rotation: int = 0) -> None:
    doc = fitz.open()
    for _ in range(pages):
        page = doc.new_page(width=width, height=height)
        if rotation:
            page.set_rotation(rotation)
    doc.save(path)
    doc.close()


def draw_check(page, center, size=18, colour=(0.0, 0.0, 0.0)):
    mark = core.adobe_check_mark(center, size=size, color=colour)
    page.draw_polyline([fitz.Point(p) for p in mark.points], color=mark.color, width=mark.width, lineCap=1, lineJoin=0)
    return mark


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def path(self, name: str) -> str:
        return os.path.join(self.dir, name)

    def test_adobe_geometry(self):
        mark = core.adobe_check_mark((100, 100), size=20, color=core.ADOBE_BLUE)
        self.assertEqual(mark.kind, "vector")
        self.assertAlmostEqual(mark.width, 2.0, places=5)
        self.assertEqual(mark.points[0], (92.0, 100.0))
        self.assertAlmostEqual(mark.points[1][0], 90 + 20 / 3.0, places=5)
        self.assertAlmostEqual(mark.points[1][1], 108.0, places=5)
        self.assertEqual(mark.points[2], (108.0, 92.0))
        self.assertAlmostEqual(mark.size, 16.0, places=5)

    def test_vector_detection_and_copy(self):
        src = self.path("source.pdf")
        make_pdf(src, pages=3)
        doc = fitz.open(src)
        page = doc[0]
        for i, colour in enumerate(((0, 0, 0), core.ADOBE_BLUE, core.ADOBE_RED)):
            draw_check(page, (80 + i * 90, 80 + i * 25), size=18, colour=colour)
        doc.saveIncr()
        doc.close()

        doc = fitz.open(src)
        marks = core.detect_checks(doc[0])
        self.assertEqual(len(marks), 3)
        self.assertTrue(all(m.kind == "vector" for m in marks))
        self.assertEqual(marks[0].color, (0.0, 0.0, 0.0))
        doc.close()

        with open(src, "rb") as fh:
            before = hashlib.sha256(fh.read()).hexdigest()
        out = self.path("out.pdf")
        stats = core.write_copy(src, out, 0, [1, 2], marks)
        self.assertEqual(stats.stamp_count, 6)
        self.assertEqual(stats.page_count, 2)
        with open(src, "rb") as fh:
            after = hashlib.sha256(fh.read()).hexdigest()
        self.assertEqual(before, after, "input PDF must never be modified")

        doc = fitz.open(out)
        for pno in (1, 2):
            copied = core.detect_checks(doc[pno])
            self.assertEqual(len(copied), 3)
            for a, b in zip(marks, copied):
                self.assertAlmostEqual(a.rect.x0, b.rect.x0, places=3)
                self.assertAlmostEqual(a.rect.y0, b.rect.y0, places=3)
                self.assertAlmostEqual(a.width, b.width, places=3)
        doc.close()

    def test_rotated_page(self):
        src = self.path("rot.pdf")
        make_pdf(src, pages=2, rotation=90)
        doc = fitz.open(src)
        draw_check(doc[0], (100, 100), size=18)
        doc.saveIncr()
        doc.close()

        doc = fitz.open(src)
        marks = core.detect_checks(doc[0])
        self.assertEqual(len(marks), 1)
        self.assertAlmostEqual(marks[0].rect.x0, 92.8, places=2)
        out = self.path("rot_out.pdf")
        core.write_copy(src, out, 0, [1], marks)
        doc.close()

        doc = fitz.open(out)
        copied = core.detect_checks(doc[1])
        self.assertEqual(len(copied), 1)
        self.assertAlmostEqual(copied[0].rect.x0, marks[0].rect.x0, places=3)
        doc.close()

    def test_parse_page_range(self):
        self.assertEqual(core.parse_page_range("2-4, 7, 9-end", 10), [2, 3, 4, 7, 9, 10])
        self.assertEqual(core.parse_page_range("all", 3), [1, 2, 3])
        self.assertEqual(core.parse_page_range(" 1 \u2013 3 ", 5), [1, 2, 3])
        with self.assertRaises(ValueError):
            core.parse_page_range("0-2", 5)
        with self.assertRaises(ValueError):
            core.parse_page_range("4-2", 5)
        with self.assertRaises(ValueError):
            core.parse_page_range("", 5)
        with self.assertRaises(ValueError):
            core.parse_page_range("6", 5)

    def test_preview_composite(self):
        src = self.path("prev.pdf")
        make_pdf(src, pages=2)
        doc = fitz.open(src)
        draw_check(doc[0], (120, 120), size=18, colour=core.ADOBE_BLUE)
        doc.saveIncr()
        doc.close()
        doc = fitz.open(src)
        marks = core.detect_checks(doc[0])
        img = core.composite_result_preview(doc[1], doc[0], marks, 1.0)
        self.assertGreater(img.width, 100)
        self.assertEqual(img.mode, "RGB")
        doc.close()

    @unittest.skipUnless(__import__("importlib").util.find_spec("pikepdf"), "pikepdf not installed")
    def test_annotation_appearance(self):
        from pikepdf import Array, Dictionary, Name, Pdf

        base = self.path("ann_base.pdf")
        make_pdf(base, pages=2)
        out = self.path("ann.pdf")
        pdf = Pdf.open(base)
        page = pdf.pages[0]
        stream = pdf.make_stream(
            b"q 0 0 0 RG 1.2 w 1 J 1.2 6 m 4 1.2 l 10.8 10.8 l S Q"
        )
        stream.Type = Name.XObject
        stream.Subtype = Name.Form
        stream.BBox = [0, 0, 12, 12]
        stream.Resources = Dictionary()
        annot = pdf.make_indirect(
            Dictionary(
                Type=Name.Annot,
                Subtype=Name.Stamp,
                Rect=[100, 188, 112, 200],
                F=4,
                AP=Dictionary(N=stream),
            )
        )
        page.Annots = Array([annot])
        pdf.save(out)
        pdf.close()

        doc = fitz.open(out)
        marks = core.detect_checks(doc[0])
        self.assertEqual(len(marks), 1)
        self.assertIn(marks[0].kind, ("vector", "image"))
        doc.close()

    def test_refuses_same_output(self):
        src = self.path("same.pdf")
        make_pdf(src, pages=2)
        with self.assertRaises(ValueError):
            core.write_copy(src, src, 0, [1], [core.adobe_check_mark((50, 50))])

    def test_annotation_classifier_x(self):
        # A cross must not be mistaken for a check.
        im = Image.new("L", (64, 64), 0)
        d = ImageDraw.Draw(im)
        d.line([(8, 8), (56, 56)], fill=255, width=6)
        d.line([(56, 8), (8, 56)], fill=255, width=6)
        import numpy as np

        sim, xsim = core._check_similarity(np.asarray(im) > 50)
        self.assertLess(sim, 0.5)
        self.assertGreater(xsim, sim)


if __name__ == "__main__":
    unittest.main(verbosity=2)
