![patchproof — find the 9-slice borders of a whole UI kit, and prove each one](docs/banner.png)

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-19%20passing-brightgreen.svg)](#how-it-works)
[![Version](https://img.shields.io/badge/version-0.1.0-blue.svg)](https://github.com/RakTikoo/patchproof/releases)

# patchproof — find the 9-slice borders of game UI panels, and prove each one

[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/RakTikoo/patchproof)

**Find the 9-slice borders of a whole folder of game UI panels, prove each one by re-rendering it, and write the engine files — or honestly say "uncertain" when a panel can't be sliced cleanly.**

Unity, Godot and Unreal all *use* 9-slice borders, but none of them works the numbers out: you drag handles or type four values per panel, by hand, for every panel in a kit. patchproof does the whole kit in one run. Every detection is verified by resynthesis — the panel is re-rendered 9-sliced at several sizes (including odd sizes) and its corners must come out pixel-identical — and every panel gets a distortion score and a verdict. Panels that don't prove out are reported `UNCERTAIN` with a reason, never given made-up numbers.

![Naive stretch (left) smears the corner rivets; the detected 9-slice (right) keeps them round](examples/demo_compare.png)

## Install

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then
install the standalone CLI:

```sh
uv tool install git+https://github.com/RakTikoo/patchproof
```

For development, clone this repository and use its locked environment:

```sh
uv sync --locked --all-extras
uv run --locked patchproof scan examples --out out
uv run --locked --all-extras pytest -q
```

uv manages Python 3.12 and `.venv` automatically. `pyproject.toml` declares
dependencies and `uv.lock` fixes their resolved versions. See
[CONTRIBUTING.md](CONTRIBUTING.md) for dependency updates and builds.
The library supports Python 3.10+; detection runs locally on CPU.

With Homebrew (macOS/Linux):

```
brew tap RakTikoo/tools
brew install RakTikoo/tools/patchproof
```

With Scoop (Windows):

```powershell
scoop bucket add RakTikoo https://github.com/RakTikoo/scoop-bucket
scoop install patchproof
```

**For agent users:** this repository ships a `SKILL.md` for AI agents. Install it with `npx skills add RakTikoo/patchproof`.

## Demo

```
$ patchproof scan examples --out out
file                         verdict       L    R    T    B  score
demo_compare.png             PROVEN      252   18   28   28  1.78
panel_asym.png               PROVEN       10   22    8   16  0.00
panel_flat.png               PROVEN       14   14   14   14  0.55
panel_gradient.png           PROVEN       12   12   12   12  0.56
panel_ornament.png           PROVEN       18   18   18   18  0.00
panel_padded.png             PROVEN       22   22   22   22  0.55
panel_photo.png              UNCERTAIN     -    -    -    -  no cleanly stretchable centre found: best distortion score 29.2 is above the threshold 6.0 (content is likely photo-like, patterned, or ornamented through its middle)
6 proven, 1 uncertain -- report: out/report.json
```

(`demo_compare.png` is the comparison picture above, not a panel — it still slices cleanly because both halves share the same flat centre. `panel_photo.png` is a photo-like image with no valid slice, and patchproof abstains on it.)

For every `PROVEN` panel you get, in the output folder:

- the panel PNG, plus a **Unity `.meta` sidecar** (`panel.png.meta`) with the `spriteBorder` filled in,
- a **Godot `.tres`** `StyleBoxTexture` with the patch margins and content margins set,
- an **Android `.9.png`** with the marker border drawn,
- a **`.slices.png` preview** with two magenta horizontal and two vertical slice guides overlaid on the original image at its original size (for visual inspection, not engine import),
- a **`.compact.png` preview** rendered with 9-slice at half the original width and height, clamped to preserve the fixed borders plus at least one centre pixel on each axis. Corners retain their original size; no guide lines are drawn. The actual dimensions are recorded as `compact_size` in the report. This reduces display dimensions, not PNG file size,
- and `report.json` covering every panel: borders, content padding, verdict, score, sizes checked.

## Usage

```
patchproof scan <file-or-folder> --out <dir> [--threshold 6.0]
```

- A folder is scanned non-recursively for PNGs; a single PNG file works too.
- Content padding (transparent margins around the panel) is detected and reported separately from the borders, and lands in the Godot content margins and the `.9.png` content box.
- `--threshold` sets how clean a slice must be to earn `PROVEN` (lower = stricter).

## Use with an AI agent

This repo ships a [SKILL.md](SKILL.md) written to the Agent Skills open standard. If your coding agent supports skills, point it at this repo and it can install patchproof, run it on your panel PNGs, and read the per-panel verdicts for you. The skill works when the repo is cloned under its real name (`patchproof`) — a skill's name must match its folder.

## How it works

1. **Stretchability profile.** A column can sit in the stretched centre if the image is locally *linear* along x there — flat fills and smooth gradients stretch losslessly, ornaments don't. Second-difference profiles give a first guess at the borders.
2. **Settle.** Borders are then adjusted against a distortion score: the worst per-row/per-column linear-fit residual over the centre and edge strips. Borders shrink while the score stays under the threshold (minimal borders) and expand while it's over.
3. **Cross-check + texture path.** A second, independent estimator finds the borders from the step in the per-column/per-row *mean* colour profile (grain averages out of a mean; a frame's mean colour does not). A borderline pass from steps 1–2 that this estimator disputes is not trusted. Its borders can still be proven for **stationary-texture centres** (parchment-style grain): the centre's mean profile must be flat within the same threshold, and the centre must not have a strong repeating period — stretching a regular pattern would rescale the pattern itself, so patterned centres still abstain.
4. **Proof by resynthesis.** The panel is re-rendered 9-sliced at 5+ sizes, odd sizes included. Corners must be pixel-identical to the original; the distortion score must be under the threshold. Both, or the verdict is `UNCERTAIN`.

On the synthetic ground-truth suite in `tests/` (flat, gradient, ornamented, asymmetric, transparent-padded, and small/large panels), patchproof recovers **28 of 28 border sides exactly** (100% exact, 100% within 1px). Run it yourself:

```
uv run --locked --all-extras pytest -q
```

19 tests (including HTTP authentication, MCP tool discovery and persistent detection jobs): border recovery, the resynthesis corner invariant, Unity/Godot/Android export round-trips, abstention on photo-like and diagonally-striped images, flip symmetry, and malformed-input handling (empty folder, non-PNG, 1×1, fully transparent, huge flat image — clean one-line errors or sensible verdicts, never a traceback).

## Honest limits

- **Centres must be flat, smoothly gradient, or stationary grain.** Parchment-style texture (grain whose average colour is the same everywhere) can now be proven, under a statistical criterion: stretching preserves the centre's mean structure, though the grain itself rescales — as it does with any stretch of a real texture. **Repeating patterns and directional grain still abstain**: a checker, brick or diamond fill, or wood grain with a visible period, would have its pattern period changed by stretching, so such panels (correctly) come back `UNCERTAIN` rather than get numbers that would distort them. Panels whose centre should be *tiled* rather than stretched are not modelled yet.
- **One panel per image.** Sprite sheets / atlases with several panels in one PNG are not segmented yet.
- **Detection is a judgement call on ornate panels.** The score tells you how clean the slice is; a low score is a proof about stretching, not about taste. Borderline kits should use a stricter `--threshold` and eyeball the `UNCERTAIN` pile.
- **Exports are written from the detection, not validated inside the engines.** The `.meta` and `.tres` files are format-faithful (and round-trip tested), but the first import into a real Unity/Godot project is still worth a look.
- Panels are processed in memory as full images; a 2000×1500 panel is fine, a folder of 8K textures will be slow.
- PNG only for now.

## License

MIT — see `LICENSE`. Free for science, games, and everything else.

## Local HTTP MCP (optional)

From a repository checkout, install and start once before connecting your MCP client:

```sh
uv sync --locked --extra http --no-dev
uv run --locked --extra http --no-dev patchproof-http --allow-root /absolute/path/to/panels
```

The service listens on `http://127.0.0.1:18741/mcp`. Configure your client with
that URL and `Authorization: Bearer <token>`. The generated token is stored in
`~/.patchproof-http/config.json`; do not put it in prompts. `--data-dir` and
`--port` override defaults. Only one process may own a data directory.
HTTP MCP uses the pinned official MCP Python SDK 1.26.0 in stateless mode;
protocol support is determined by that SDK, not by application job IDs.
This release targets the 2025-11-25 protocol; 2026-07-28 is not claimed.

Tools: `patchproof_get_capabilities`, `patchproof_analyze`,
`patchproof_get_job`, `patchproof_get_preview`, `patchproof_cancel_job`.
Analyze accepts `requestKey`, `items` (each with `clientItemId` and exactly one
of `assetId` / `localPath`), and optional `options` (`threshold`, `previewSizes`).
Submit returns a persistent job; poll get_job with `offset`/`limit`.
Identical request keys and payloads reuse the job; changed payloads conflict.
Completed results survive disconnects; unfinished jobs become `interrupted`
on restart. Use a new request key to retry interrupted work.

`POST /v1/assets` accepts a raw PNG body (not multipart) and returns its asset ID,
dimensions and pixel fingerprint. Uploads are limited to 20 MiB / 4 million
pixels; batches to 100 items; previews to four sizes, each at most 2048×2048.
Local paths must resolve under an explicit `--allow-root`; they are snapshotted
before queueing. No arbitrary URL downloads are supported.
`GET /v1/artifacts/{id}` downloads previews with the same Bearer token.
Preview tools return artifact metadata; images are downloaded separately.
`/healthz` is public; `/readyz` requires authentication.

This is a local, single-caller service with one sequential detection worker,
60-second per-image timeout and persistent SQLite jobs. Cancellation kills the
active image process and preserves completed results. `UNCERTAIN` results have
null borders. Pixel fingerprints hash big-endian uint64 width and height followed
by full RGBA8 bytes. Detection does not validate PSD2UI's one-pixel collapse.
PSD2UI integration, automated retention, remote multi-user authentication,
Windows installer/autostart and clean-machine executable packaging remain
separate work; do not expose this local profile to a network.
