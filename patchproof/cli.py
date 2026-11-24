"""Command line: patchproof scan <file-or-folder> --out <dir>."""

import argparse
import json
import shutil
import sys
from pathlib import Path

from PIL import Image

from . import __version__
from .core import DEFAULT_THRESHOLD, detect, render_9slice
from .exports import godot_tres_text, nine_patch_image, slice_preview_image, unity_meta_text


def _process(path: Path, out_dir: Path, threshold: float) -> dict:
    entry = {"file": path.name}
    try:
        img = Image.open(path)
        img.load()
    except Exception as exc:  # unreadable / corrupt file: verdict, not a crash
        entry.update(verdict="UNCERTAIN", score=None,
                     reason=f"unreadable image ({type(exc).__name__})")
        return entry
    det = detect(img, threshold=threshold)
    entry.update(
        width=det.width, height=det.height,
        # UNCERTAIN panels get no border numbers in the report: the
        # candidate borders from a failed detection are not real values
        # and must not be mistaken for them (README: "never given
        # made-up numbers"). Exports are likewise only written for
        # PROVEN panels (below).
        borders=det.borders if det.verdict == "PROVEN" else None,
        padding=det.padding,
        verdict=det.verdict,
        score=None if det.score < 0 else round(det.score, 3),
        sizes_checked=[list(s) for s in det.sizes_checked],
        corners_identical=det.corners_identical,
    )
    if det.reason:
        entry["reason"] = det.reason
    if det.verdict == "PROVEN":
        stem = path.stem
        shutil.copyfile(path, out_dir / path.name)
        (out_dir / (path.name + ".meta")).write_text(
            unity_meta_text(path.name, det), encoding="utf-8")
        (out_dir / (stem + ".tres")).write_text(
            godot_tres_text(path.name, det), encoding="utf-8")
        nine_patch_image(img, det).save(out_dir / (stem + ".9.png"))
        slice_preview_image(img, det).save(out_dir / (stem + ".slices.png"))
        b = det.borders
        compact_w = max(det.width // 2, b["left"] + b["right"] + 1)
        compact_h = max(det.height // 2, b["top"] + b["bottom"] + 1)
        render_9slice(img, (b["left"], b["right"], b["top"], b["bottom"]),
                      compact_w, compact_h).save(out_dir / (stem + ".compact.png"))
        entry["compact_size"] = [compact_w, compact_h]
        entry["exports"] = [path.name + ".meta", stem + ".tres", stem + ".9.png",
                            stem + ".slices.png", stem + ".compact.png"]
    return entry


def cmd_scan(args) -> int:
    target = Path(args.path)
    out_dir = Path(args.out)
    if target.is_file():
        if target.suffix.lower() != ".png":
            print(f"error: not a PNG file: {target}", file=sys.stderr)
            return 2
        files = [target]
    elif target.is_dir():
        files = sorted(p for p in target.iterdir()
                       if p.is_file() and p.suffix.lower() == ".png")
        if not files:
            print(f"error: no PNG files found in {target}", file=sys.stderr)
            return 2
    else:
        print(f"error: no such file or folder: {target}", file=sys.stderr)
        return 2

    out_dir.mkdir(parents=True, exist_ok=True)
    entries = [_process(p, out_dir, args.threshold) for p in files]

    report = {"tool": "patchproof", "version": __version__,
              "threshold": args.threshold, "panels": entries}
    (out_dir / "report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8")

    print(f"{'file':<28} {'verdict':<10} {'L':>4} {'R':>4} {'T':>4} {'B':>4}  score")
    proven = 0
    for e in entries:
        if e["verdict"] == "PROVEN":
            proven += 1
            b = e["borders"]
            print(f"{e['file']:<28} {'PROVEN':<10} {b['left']:>4} {b['right']:>4} "
                  f"{b['top']:>4} {b['bottom']:>4}  {e['score']:.2f}")
        else:
            print(f"{e['file']:<28} {'UNCERTAIN':<10} {'-':>4} {'-':>4} "
                  f"{'-':>4} {'-':>4}  {e.get('reason', '')}")
    print(f"{proven} proven, {len(entries) - proven} uncertain "
          f"-- report: {out_dir / 'report.json'}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="patchproof",
        description="Detect 9-slice borders of game UI panels, prove each "
                    "detection by resynthesis, and export engine files.")
    parser.add_argument("--version", action="version",
                        version=f"patchproof {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    scan = sub.add_parser("scan", help="scan one PNG or a folder of PNGs")
    scan.add_argument("path", help="a PNG file or a folder containing PNGs")
    scan.add_argument("--out", required=True, help="output folder")
    scan.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                      help="distortion score at or below which a detection is "
                           f"PROVEN (default {DEFAULT_THRESHOLD})")
    scan.set_defaults(func=cmd_scan)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
