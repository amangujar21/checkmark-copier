from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest

import fitz

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import app as app_module
import checkmark_core as core


class AppSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = app_module.tk.Tk()
        cls.root.withdraw()
        cls.app = app_module.CheckmarkCopierApp(cls.root)
        cls.demo = os.path.join(ROOT, "samples", "demo.pdf")
        cls.app.load_pdf(cls.demo)
        cls.root.update()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.destroy()
        except Exception:
            pass

    def test_demo_detection_and_targets(self):
        self.assertEqual(self.app.doc.page_count, 6)
        self.assertEqual(len(self.app.marks), 4)
        self.assertEqual(self.app.target_pages, [1, 2, 3, 4, 5])

    def test_add_and_undo(self):
        before = len(self.app.marks)
        self.app._add_mark_at(fitz.Point(200, 200))
        self.assertEqual(len(self.app.marks), before + 1)
        self.app.undo()
        self.assertEqual(len(self.app.marks), before)

    def test_recolour_and_resize(self):
        self.app.selected = 0
        self.app._choose_colour(core.ADOBE_RED)
        self.assertEqual(self.app.marks[0].color, core.ADOBE_RED)
        self.app.size_var.set(20)
        self.app._apply_size_now()
        self.assertAlmostEqual(self.app.marks[0].size, 20.0, places=1)

    def test_invalid_range(self):
        self.app.target_var.set("99-100")
        self.assertEqual(self.app._targets_changed(), [])

    def test_save_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "saved.pdf")
            self.app.target_var.set("2-3")
            self.app._targets_changed()
            self.app.output_var = out
            captured = {}
            self.app.show_success = lambda stats: captured.setdefault("stats", stats)
            self.app.save_copy()
            deadline = time.time() + 10
            while time.time() < deadline and "stats" not in captured:
                self.root.update()
                time.sleep(0.05)
            self.root.update()
            self.assertTrue(os.path.exists(out), "output was not written")
            self.assertIn("stats", captured)
            self.assertEqual(captured["stats"].page_count, 2)
            doc = fitz.open(out)
            try:
                self.assertEqual(len(core.detect_checks(doc[1])), 4)
                self.assertEqual(len(core.detect_checks(doc[2])), 4)
            finally:
                doc.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
