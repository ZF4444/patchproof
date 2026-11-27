"""patchproof test suite (stdlib unittest).

Fixtures are synthetic panels with known ground-truth borders, built by
examples/make_examples.py so the shipped examples and the tests share one
source of truth.
"""

import io
import json
import sys
import tempfile
import unittest
from types import SimpleNamespace
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples"))

from make_examples import make_diagonal, make_panel, make_photo  # noqa: E402
from patchproof import __version__, detect, render_9slice  # noqa: E402
from patchproof.cli import main as cli_main  # noqa: E402
from patchproof.core import _corners_identical, verify_sizes  # noqa: E402
from patchproof.exports import (  # noqa: E402
    godot_tres_text,
    nine_patch_image,
    parse_meta_border,
    parse_tres_margins,
    unity_meta_text,
    slice_preview_image,
)

PANEL_CASES = [
    ("flat", dict(kind="flat", w=120, h=96, borders=(14, 14, 14, 14), noise=1.0)),
    ("gradient", dict(kind="gradient", w=128, h=96, borders=(12, 12, 12, 12),
                      noise=1.0)),
    ("ornament", dict(kind="ornament", w=132, h=100, borders=(18, 18, 18, 18))),
    ("asym", dict(kind="asym", w=120, h=96, borders=(10, 22, 8, 16))),
    ("padded", dict(kind="flat", w=140, h=116, borders=(12, 12, 12, 12),
                    padding=(10, 10, 10, 10), noise=1.0)),
    ("flat_big", dict(kind="flat", w=200, h=150, borders=(20, 20, 24, 24),
                      noise=1.0)),
    ("gradient_small", dict(kind="gradient", w=64, h=52, borders=(9, 9, 9, 9),
                            noise=1.0)),
]


class TestRecovery(unittest.TestCase):
    def test_hidden_rgb_does_not_affect_detection(self):
        pixels = np.full((96, 80, 4), (7, 9, 11, 255), dtype=np.uint8)
        pixels[14:78, 12:68, 3] = 0
        original = pixels.copy()
        expected = detect(Image.fromarray(pixels))
        self.assertEqual(expected.verdict, "PROVEN")
        rng = np.random.default_rng(42)
        for mode in ("white_rectangle", "random"):
            changed = original.copy()
            if mode == "white_rectangle":
                changed[30:60, 24:50, :3] = 255
            else:
                hidden = changed[:, :, 3] == 0
                changed[hidden, :3] = rng.integers(
                    0, 256, size=(hidden.sum(), 3), dtype=np.uint8)
            image = Image.fromarray(changed)
            with self.subTest(mode=mode):
                self.assertEqual(detect(image), expected)
                np.testing.assert_array_equal(np.asarray(image), changed)

    def test_recovery_rate(self):
        exact = within1 = total = 0
        for name, kwargs in PANEL_CASES:
            img, truth = make_panel(**kwargs)
            det = detect(img)
            self.assertEqual(det.verdict, "PROVEN", f"{name}: {det.reason}")
            self.assertEqual(det.padding, truth["padding"], name)
            for side in ("left", "right", "top", "bottom"):
                total += 1
                err = abs(det.borders[side] - truth["borders"][side])
                exact += err == 0
                within1 += err <= 1
        print(f"\nborder recovery over {total} sides: "
              f"exact {exact} ({exact / total:.1%}), "
              f"within 1px {within1} ({within1 / total:.1%})")
        self.assertEqual(within1, total, "every side must land within 1px")

    def test_each_case_within_1px(self):
        for name, kwargs in PANEL_CASES:
            img, truth = make_panel(**kwargs)
            det = detect(img)
            for side in ("left", "right", "top", "bottom"):
                self.assertLessEqual(
                    abs(det.borders[side] - truth["borders"][side]), 1,
                    f"{name}.{side}: got {det.borders[side]}, "
                    f"truth {truth['borders'][side]}")


class TestResynthesis(unittest.TestCase):
    def test_corners_identical_at_five_plus_sizes(self):
        img, _ = make_panel(kind="ornament", w=132, h=100,
                            borders=(18, 18, 18, 18))
        det = detect(img)
        self.assertEqual(det.verdict, "PROVEN")
        b = (det.borders["left"], det.borders["right"],
             det.borders["top"], det.borders["bottom"])
        sizes = verify_sizes(img.width, img.height, b)
        self.assertGreaterEqual(len(sizes), 5)
        self.assertTrue(any(w % 2 == 1 or h % 2 == 1 for w, h in sizes),
                        "size list must include odd sizes")
        for (ow, oh) in sizes:
            rendered = render_9slice(img, b, ow, oh)
            self.assertEqual(rendered.size, (ow, oh))
            self.assertTrue(_corners_identical(img, rendered, b),
                            f"corners differ at {ow}x{oh}")
        self.assertTrue(det.corners_identical)
        self.assertGreaterEqual(len(det.sizes_checked), 5)


class TestExports(unittest.TestCase):
    def test_preview_draws_only_internal_boundaries(self):
        image = Image.new('RGBA', (20, 16), (40, 50, 60, 255))
        det = SimpleNamespace(borders=dict(left=0, right=0, top=0, bottom=0))
        np.testing.assert_array_equal(slice_preview_image(image, det), image)
        det.borders = dict(left=2, right=0, top=0, bottom=3)
        preview = np.array(slice_preview_image(image, det))
        expected = np.array(image)
        expected[:, 2] = (255, 0, 160, 255)
        expected[13, :] = (255, 0, 160, 255)
        np.testing.assert_array_equal(preview, expected)

    def setUp(self):
        self.img, self.truth = make_panel(kind="asym", w=120, h=96,
                                          borders=(10, 22, 8, 16))
        self.det = detect(self.img)
        self.assertEqual(self.det.verdict, "PROVEN")

    def test_meta_round_trip(self):
        text = unity_meta_text("panel_asym.png", self.det)
        got = parse_meta_border(text)
        b = self.det.borders
        # Unity order is (x=left, y=bottom, z=right, w=top)
        self.assertEqual(got, (b["left"], b["bottom"], b["right"], b["top"]))
        self.assertIn("guid: ", text)

    def test_tres_round_trip(self):
        text = godot_tres_text("panel_asym.png", self.det)
        got = parse_tres_margins(text)
        b, p = self.det.borders, self.det.padding
        self.assertEqual(got["patch_margin_left"], b["left"])
        self.assertEqual(got["patch_margin_right"], b["right"])
        self.assertEqual(got["patch_margin_top"], b["top"])
        self.assertEqual(got["patch_margin_bottom"], b["bottom"])
        self.assertEqual(got["content_margin_left"], p["left"])
        self.assertEqual(got["content_margin_top"], p["top"])

    def test_nine_patch_markers(self):
        img, truth = make_panel(kind="flat", w=140, h=116,
                                borders=(12, 12, 12, 12),
                                padding=(10, 10, 10, 10), noise=1.0)
        det = detect(img)
        self.assertEqual(det.verdict, "PROVEN")
        nine = nine_patch_image(img, det)
        self.assertEqual(nine.size, (img.width + 2, img.height + 2))
        px = np.asarray(nine)
        b = det.borders
        # top marker row covers exactly the centre columns
        top_row = px[0, :, 3] > 0
        expect_top = np.zeros(img.width + 2, dtype=bool)
        expect_top[1 + b["left"]:1 + img.width - b["right"]] = True
        self.assertTrue(np.array_equal(top_row, expect_top))
        # left marker column covers exactly the centre rows
        left_col = px[:, 0, 3] > 0
        expect_left = np.zeros(img.height + 2, dtype=bool)
        expect_left[1 + b["top"]:1 + img.height - b["bottom"]] = True
        self.assertTrue(np.array_equal(left_col, expect_left))
        # marker pixels are solid black
        self.assertTrue(np.all(px[0, expect_top, :3] == 0))


class TestAbstention(unittest.TestCase):
    def test_photo_is_uncertain(self):
        det = detect(make_photo())
        self.assertEqual(det.verdict, "UNCERTAIN")
        self.assertTrue(det.reason)

    def test_diagonal_is_uncertain(self):
        det = detect(make_diagonal())
        self.assertEqual(det.verdict, "UNCERTAIN")


class TestSymmetry(unittest.TestCase):
    def test_flip_swaps_left_right(self):
        img, _ = make_panel(kind="asym", w=120, h=96, borders=(10, 22, 8, 16))
        det = detect(img)
        flipped = img.transpose(Image.FLIP_LEFT_RIGHT)
        fdet = detect(flipped)
        self.assertEqual(fdet.verdict, "PROVEN")
        self.assertEqual(fdet.borders["left"], det.borders["right"])
        self.assertEqual(fdet.borders["right"], det.borders["left"])
        self.assertEqual(fdet.borders["top"], det.borders["top"])
        self.assertEqual(fdet.borders["bottom"], det.borders["bottom"])


class TestAdversarial(unittest.TestCase):
    def test_one_pixel_image(self):
        det = detect(Image.new("RGBA", (1, 1), (255, 0, 0, 255)))
        self.assertEqual(det.verdict, "UNCERTAIN")

    def test_fully_transparent(self):
        det = detect(Image.new("RGBA", (64, 64), (0, 0, 0, 0)))
        self.assertEqual(det.verdict, "UNCERTAIN")
        self.assertIn("transparent", det.reason)

    def test_huge_flat_image(self):
        img = Image.new("RGBA", (2000, 1500), (120, 130, 140, 255))
        det = detect(img)
        self.assertEqual(det.verdict, "PROVEN")
        self.assertEqual(det.borders, {"left": 0, "right": 0,
                                       "top": 0, "bottom": 0})

    def test_cli_empty_folder(self):
        with tempfile.TemporaryDirectory() as td:
            err = io.StringIO()
            with redirect_stderr(err):
                code = cli_main(["scan", td, "--out", str(Path(td) / "out")])
            self.assertEqual(code, 2)
            self.assertIn("no PNG files", err.getvalue())
            self.assertNotIn("Traceback", err.getvalue())

    def test_cli_non_png_file(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "notes.txt"
            f.write_text("hello")
            err = io.StringIO()
            with redirect_stderr(err):
                code = cli_main(["scan", str(f), "--out", str(Path(td) / "o")])
            self.assertEqual(code, 2)
            self.assertIn("not a PNG", err.getvalue())

    def test_cli_missing_path(self):
        err = io.StringIO()
        with redirect_stderr(err):
            code = cli_main(["scan", "/nonexistent/nowhere", "--out", "/tmp/o"])
        self.assertEqual(code, 2)

    def test_cli_corrupt_png_is_a_verdict_not_a_crash(self):
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "bad.png"
            bad.write_bytes(b"this is not really a png")
            out = Path(td) / "out"
            with redirect_stdout(io.StringIO()):
                code = cli_main(["scan", str(bad), "--out", str(out)])
            self.assertEqual(code, 0)
            report = json.loads((out / "report.json").read_text())
            self.assertEqual(report["panels"][0]["verdict"], "UNCERTAIN")


class TestCliScan(unittest.TestCase):
    def test_folder_scan_writes_exports_and_report(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "in"
            src.mkdir()
            img, _ = make_panel(kind="ornament", w=132, h=100,
                                borders=(18, 18, 18, 18))
            img.save(src / "panel_ornament.png")
            make_photo().save(src / "panel_photo.png")
            out = Path(td) / "out"
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = cli_main(["scan", str(src), "--out", str(out)])
            self.assertEqual(code, 0)
            self.assertTrue((out / "panel_ornament.png.meta").exists())
            self.assertTrue((out / "panel_ornament.tres").exists())
            self.assertTrue((out / "panel_ornament.9.png").exists())
            self.assertFalse((out / "panel_photo.png.meta").exists())
            report = json.loads((out / "report.json").read_text())
            self.assertEqual(report["version"], __version__)
            verdicts = {p["file"]: p["verdict"] for p in report["panels"]}
            self.assertEqual(verdicts["panel_ornament.png"], "PROVEN")
            self.assertEqual(verdicts["panel_photo.png"], "UNCERTAIN")
            self.assertIn("1 proven, 1 uncertain", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
