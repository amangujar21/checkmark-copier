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

    def test_fillsign_objects_are_native(self):
        """Copied vector checks must be real Fill & Sign page objects.

        Adobe does not store its check marks as annotations.  It writes a
        marked-content section containing /page, /fields and /check form
        XObjects carrying /ADBE_FillSign metadata.  Acrobat only treats a
        check as selectable/movable when that tree is present.
        """

        src = self.path("fs_src.pdf")
        make_pdf(src, pages=2)
        doc = fitz.open(src)
        draw_check(doc[0], (120, 120), size=18, colour=core.ADOBE_RED)
        doc.saveIncr()
        doc.close()

        doc = fitz.open(src)
        marks = core.detect_checks(doc[0])
        doc.close()
        out = self.path("fs_out.pdf")
        core.write_copy(src, out, 0, [1], marks)

        doc = fitz.open(out)
        page = doc[1]
        content = page.read_contents().decode("latin-1")
        self.assertIn("/ADBE_FillSign BMC", content)
        self.assertIn("/FmFS0 Do", content)

        xobjects = page.get_xobjects()
        self.assertIn("FmFS0", {name for _, name, _, _ in xobjects})
        subtypes = []
        colours = []
        for xref, _name, _parent, _bbox in xobjects:
            typ, val = doc.xref_get_key(xref, "ADBE_FillSign/Subtype")
            if typ == "name":
                subtypes.append(val)
            colour_type, colour = doc.xref_get_key(xref, "ADBE_FillSign/FieldColor")
            if colour_type == "array":
                colours.append(colour)
        self.assertIn("/page", subtypes)
        self.assertIn("/fields", subtypes)
        self.assertIn("/check", subtypes)
        self.assertTrue(colours, "check form must carry Adobe's FieldColor")
        self.assertEqual(
            len(colours), sum(1 for value in subtypes if value == "/check")
        )

        info_type, info_version = doc.xref_get_key(
            doc.pdf_catalog(), "ADBE_FillSignInfo/Version"
        )
        self.assertEqual((info_type, info_version), ("int", "100"))
        doc.close()

    def _fillsign_tree(self, doc, page):
        """Return (page forms, fields forms, check forms) on ``page``."""

        tree = {"/page": [], "/fields": [], "/check": []}
        for xref, _name, parent, _bbox in page.get_xobjects():
            typ, val = doc.xref_get_key(xref, "ADBE_FillSign/Subtype")
            if typ == "name" and val in tree:
                tree[val].append((xref, parent))
        return tree["/page"], tree["/fields"], tree["/check"]

    def test_fillsign_checks_share_one_group_per_page(self):
        """Acrobat selects only one Fill & Sign group per page.

        Every check on a page must be a child of a single /fields form, as in
        Adobe's own output; otherwise only one copied check is movable.
        """

        src = self.path("fs_multi_src.pdf")
        make_pdf(src, pages=2)
        doc = fitz.open(src)
        for x in (80, 250, 420):
            draw_check(doc[0], (x, 120), size=18, colour=core.ADOBE_RED)
        doc.saveIncr()
        doc.close()

        doc = fitz.open(src)
        marks = core.detect_checks(doc[0])
        doc.close()
        self.assertEqual(len(marks), 3)
        out = self.path("fs_multi_out.pdf")
        core.write_copy(src, out, 0, [1], marks)

        doc = fitz.open(out)
        page = doc[1]
        content = page.read_contents().decode("latin-1")
        self.assertEqual(content.count("/ADBE_FillSign BMC"), 1)
        pages, fields, checks = self._fillsign_tree(doc, page)
        self.assertEqual(len(pages), 1)
        self.assertEqual(len(fields), 1)
        self.assertEqual(fields[0][1], pages[0][0])
        self.assertEqual(len(checks), 3)
        self.assertTrue(all(parent == fields[0][0] for _, parent in checks))
        self.assertEqual(len(core.detect_checks(page)), 3)
        doc.close()

        # Copying onto a page that already has a group must extend it rather
        # than add a second group.
        out2 = self.path("fs_multi_out2.pdf")
        doc = fitz.open(src)
        first = core.detect_checks(doc[0])[:1]
        doc.close()
        core.write_copy(out, out2, 0, [1], first, mode="absolute")
        doc = fitz.open(out2)
        page = doc[1]
        content = page.read_contents().decode("latin-1")
        self.assertEqual(content.count("/ADBE_FillSign BMC"), 1)
        pages, fields, checks = self._fillsign_tree(doc, page)
        self.assertEqual((len(pages), len(fields), len(checks)), (1, 1, 4))
        self.assertTrue(all(parent == fields[0][0] for _, parent in checks))
        doc.close()

    def test_fillsign_groups_not_listed_on_sibling_pages(self):
        """Pages sharing one /Resources object must not see each other's groups.

        Office exports often point every page at one resources dictionary.  If
        a page's Fill & Sign form were added there, Acrobat would list it on
        every page and attach checks added on one page to another page's group.
        """

        src = self.path("fs_shared_src.pdf")
        make_pdf(src, pages=4)
        doc = fitz.open(src)
        shared = doc.get_new_xref()
        doc.update_object(shared, "<< >>")
        for page in doc:
            doc.xref_set_key(page.xref, "Resources", f"{shared} 0 R")
        draw_check(doc[0], (120, 120), size=18)
        doc.save(self.path("fs_shared_src2.pdf"))
        doc.close()
        src = self.path("fs_shared_src2.pdf")
        with open(src, "rb") as fh:
            before = hashlib.sha256(fh.read()).hexdigest()

        doc = fitz.open(src)
        marks = core.detect_checks(doc[0])
        doc.close()
        self.assertEqual(len(marks), 1)
        out = self.path("fs_shared_out.pdf")
        core.write_copy(src, out, 0, [1, 2, 3], marks)

        doc = fitz.open(out)
        resources = set()
        for index in (1, 2, 3):
            page = doc[index]
            resources.add(doc.xref_get_key(page.xref, "Resources")[1])
            pages, fields, checks = self._fillsign_tree(doc, page)
            self.assertEqual((len(pages), len(fields), len(checks)), (1, 1, 1))
            top = [x for x, _n, parent, _b in page.get_xobjects() if parent == 0]
            self.assertEqual(top, [pages[0][0]])
        self.assertEqual(len(resources), 3)
        self.assertEqual(
            [x for x, _n, p, _b in doc[0].get_xobjects() if p == 0], []
        )
        doc.close()
        with open(src, "rb") as fh:
            self.assertEqual(hashlib.sha256(fh.read()).hexdigest(), before)

    def _assert_pages_list_only_own_groups(self, doc):
        for page in doc:
            content = page.read_contents()
            for xref, name, parent, _b in page.get_xobjects():
                typ, val = doc.xref_get_key(xref, "ADBE_FillSign/Subtype")
                if parent == 0 and typ == "name" and val == "/page":
                    self.assertIn(f"/{name} Do".encode(), content, f"page {page.number + 1}")

    def _shared_checks_pdf(self, name):
        """Source page 1 with one check; pages 2-4 share one /Resources object."""

        src = self.path(name)
        make_pdf(src, pages=4)
        doc = fitz.open(src)
        shared = doc.get_new_xref()
        doc.update_object(shared, "<< >>")
        for page in doc:
            doc.xref_set_key(page.xref, "Resources", f"{shared} 0 R")
        draw_check(doc[0], (120, 120), size=18)
        out = self.path("full_" + name)
        doc.save(out)
        doc.close()
        doc = fitz.open(out)
        marks = core.detect_checks(doc[0])
        doc.close()
        return out, marks

    def test_repairs_groups_listed_on_sibling_pages(self):
        """Files written by 1.0.0 list every page's group on every page."""

        src, marks = self._shared_checks_pdf("fs_broken_src.pdf")
        good = self.path("fs_broken_good.pdf")
        core.write_copy(src, good, 0, [1, 2, 3], marks)

        # Rebuild the 1.0.0 layout: one shared dict listing all three groups.
        doc = fitz.open(good)
        entries = []
        for i, index in enumerate((1, 2, 3)):
            page = doc[index]
            (form, _p), = self._fillsign_tree(doc, page)[0]
            entries.append(f"/FmFS{i} {form} 0 R")
            last = page.get_contents()[-1]
            doc.update_stream(last, doc.xref_stream(last).replace(b"/FmFS0 Do", f"/FmFS{i} Do".encode()))
        shared = doc.get_new_xref()
        doc.update_object(shared, "<< /XObject << " + " ".join(entries) + " >> >>")
        for page in doc:
            doc.xref_set_key(page.xref, "Resources", f"{shared} 0 R")
        broken = self.path("fs_broken.pdf")
        doc.save(broken)
        doc.close()

        fixed = self.path("fs_broken_fixed.pdf")
        core.write_copy(broken, fixed, 0, [2], marks, mode="absolute")
        doc = fitz.open(fixed)
        self._assert_pages_list_only_own_groups(doc)
        counts = [len(self._fillsign_tree(doc, doc[i])[2]) for i in range(4)]
        self.assertEqual(counts, [0, 1, 2, 1])
        doc.close()

    def test_group_drawn_by_two_pages_is_not_merged_into(self):
        src, marks = self._shared_checks_pdf("fs_dup_src.pdf")
        first = self.path("fs_dup_first.pdf")
        core.write_copy(src, first, 0, [1], marks)

        # Make page 3 draw page 2's group too (same content stream object).
        doc = fitz.open(first)
        doc.xref_set_key(doc[2].xref, "Resources", doc.xref_get_key(doc[1].xref, "Resources")[1])
        doc.xref_set_key(doc[2].xref, "Contents", doc.xref_get_key(doc[1].xref, "Contents")[1])
        dup = self.path("fs_dup.pdf")
        doc.save(dup)
        doc.close()

        out = self.path("fs_dup_out.pdf")
        core.write_copy(dup, out, 0, [2], marks, mode="absolute")
        doc = fitz.open(out)
        self._assert_pages_list_only_own_groups(doc)
        page2, page3 = doc[1], doc[2]
        tree2, tree3 = self._fillsign_tree(doc, page2), self._fillsign_tree(doc, page3)
        self.assertEqual(len(tree2[2]), 1)
        self.assertEqual(len(tree3[0]), 1)
        self.assertEqual(len(tree3[2]), 2)
        self.assertNotEqual(tree2[0][0][0], tree3[0][0][0])
        doc.close()

    def test_encrypted_output_keeps_fillsign_objects(self):
        src = self.path("enc_src.pdf")
        make_pdf(src, pages=2)
        doc = fitz.open(src)
        draw_check(doc[0], (120, 120), size=18, colour=core.ADOBE_RED)
        doc.save(
            src + ".tmp",
            encryption=fitz.PDF_ENCRYPT_AES_256,
            owner_pw="owner",
            user_pw="user",
        )
        doc.close()
        os.replace(src + ".tmp", src)

        doc = fitz.open(src)
        self.assertTrue(doc.authenticate("user"))
        marks = core.detect_checks(doc[0])
        doc.close()
        self.assertEqual(len(marks), 1)

        out = self.path("enc_out.pdf")
        core.write_copy(src, out, 0, [1], marks, password="user")
        doc = fitz.open(out)
        self.assertTrue(doc.needs_pass)
        self.assertTrue(doc.authenticate("user"))
        copied = core.detect_checks(doc[1])
        self.assertEqual(len(copied), 1)
        content = doc[1].read_contents().decode("latin-1")
        self.assertIn("/ADBE_FillSign BMC", content)
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
