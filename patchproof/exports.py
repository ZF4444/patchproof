"""Engine export writers (and parsers, used by the tests for round-trips).

Formats:
  * Unity .meta sidecar  -- spriteBorder {x: left, y: bottom, z: right, w: top}
    (note: Unity's y is the BOTTOM border and w is the TOP border).
  * Godot .tres          -- a StyleBoxTexture resource with patch margins and
    content margins.
  * Android .9.png       -- the panel plus a 1px marker border: black pixels on
    the top/left edges mark the stretch regions; black pixels on the
    right/bottom edges mark the content (padding) box.
"""

import hashlib
import re

from PIL import Image, ImageDraw


def _guid(seed: str) -> str:
    return hashlib.md5(("patchproof:" + seed).encode("utf-8")).hexdigest()


def unity_meta_text(png_filename: str, det) -> str:
    """Unity TextureImporter .meta contents for a PROVEN detection."""
    b, p = det.borders, det.padding
    guid = _guid(png_filename)
    sprite_id = _guid(png_filename + ":sprite")
    return f"""fileFormatVersion: 2
guid: {guid}
TextureImporter:
  internalIDToNameTable: []
  externalObjects: {{}}
  serializedVersion: 13
  mipmaps:
    mipMapMode: 0
    enableMipMap: 1
    sRGBTexture: 1
    linearTexture: 0
    fadeOut: 0
    borderMipMap: 0
    mipMapsPreserveCoverage: 0
    alphaTestReferenceValue: 0.5
    mipMapFadeDistanceStart: 1
    mipMapFadeDistanceEnd: 3
  bumpmap:
    convertToNormalMap: 0
    externalNormalMap: 0
    heightScale: 0.25
    normalMapFilter: 0
    flipGreenChannel: 0
  isReadable: 0
  streamingMipmaps: 0
  streamingMipmapsPriority: 0
  vTOnly: 0
  ignoreMipmapLimit: 0
  grayScaleToAlpha: 0
  generateCubemap: 6
  cubemapConvolution: 0
  seamlessCubemap: 0
  textureFormat: 1
  maxTextureSize: 2048
  textureSettings:
    serializedVersion: 2
    filterMode: 1
    aniso: 1
    mipBias: 0
    wrapU: 0
    wrapV: 0
    wrapW: 0
  nPOTScale: 0
  lightmap: 0
  compressionQuality: 50
  spriteMode: 1
  spriteExtrude: 1
  spriteMeshType: 1
  alignment: 0
  spritePivot: {{x: 0.5, y: 0.5}}
  spritePixelsToUnits: 100
  spriteBorder: {{x: {b['left']}, y: {b['bottom']}, z: {b['right']}, w: {b['top']}}}
  spriteGenerateFallbackPhysicsShape: 1
  alphaUsage: 1
  alphaIsTransparency: 1
  spriteTessellationDetail: -1
  textureType: 8
  textureShape: 1
  singleChannelComponent: 0
  flipbookRows: 1
  flipbookColumns: 1
  maxTextureSizeSet: 0
  compressionQualitySet: 0
  textureFormatSet: 0
  ignorePngGamma: 0
  applyGammaDecoding: 0
  cookieLightType: 0
  platformSettings:
  - serializedVersion: 3
    buildTarget: DefaultTexturePlatform
    maxTextureSize: 2048
    resizeAlgorithm: 0
    textureFormat: -1
    textureCompression: 1
    compressionQuality: 50
    crunchedCompression: 0
    allowsAlphaSplitting: 0
    overridden: 0
    ignorePlatformSupport: 0
    androidETC2FallbackOverride: 0
    forceMaximumCompressionQuality_BC: 0
  spriteSheet:
    serializedVersion: 2
    sprites: []
    outline: []
    physicsShape: []
    bones: []
    spriteID: {sprite_id}
    internalID: 0
    vertices: []
    indices:
    edges: []
    weights: []
    secondaryTextures: []
    nameFileIdTable: {{}}
  spritePackingTag:
  pSDRemoveMatte: 0
  pSDShowRemoveMatteOption: 0
  userData:
  assetBundleName:
  assetBundleVariant:
"""


def parse_meta_border(text: str):
    """Read spriteBorder back out of .meta text -> (left, bottom, right, top)."""
    m = re.search(
        r"spriteBorder:\s*\{x:\s*(\d+),\s*y:\s*(\d+),\s*z:\s*(\d+),\s*w:\s*(\d+)\}",
        text)
    if not m:
        return None
    return tuple(int(g) for g in m.groups())


def godot_tres_text(png_filename: str, det) -> str:
    """Godot 4 StyleBoxTexture .tres contents for a PROVEN detection."""
    b, p = det.borders, det.padding
    return f"""[gd_resource type="StyleBoxTexture" format=3]

[ext_resource type="Texture2D" path="{png_filename}" id="1_pp"]

[resource]
texture = ExtResource("1_pp")
region_rect = Rect2(0, 0, {det.width}, {det.height})
patch_margin_left = {b['left']}
patch_margin_right = {b['right']}
patch_margin_top = {b['top']}
patch_margin_bottom = {b['bottom']}
content_margin_left = {p['left']}
content_margin_right = {p['right']}
content_margin_top = {p['top']}
content_margin_bottom = {p['bottom']}
axis_stretch_horizontal = 0
axis_stretch_vertical = 0
draw_center = true
"""


def parse_tres_margins(text: str):
    """Read the patch/content margins back out of .tres text -> dict or None."""
    out = {}
    for key in ("patch_margin_left", "patch_margin_right", "patch_margin_top",
                "patch_margin_bottom", "content_margin_left",
                "content_margin_right", "content_margin_top",
                "content_margin_bottom"):
        m = re.search(rf"^{key} = (\d+)$", text, flags=re.MULTILINE)
        if not m:
            return None
        out[key] = int(m.group(1))
    return out


def slice_preview_image(img: Image.Image, det) -> Image.Image:
    """Overlay the four slice guides on a copy of the original image."""
    out = img.convert("RGBA").copy()
    w, h = out.size
    b = det.borders
    draw = ImageDraw.Draw(out)
    color = (255, 0, 160, 255)
    for x in (b["left"], w - b["right"]):
        draw.line((x, 0, x, h - 1), fill=color)
    for y in (b["top"], h - b["bottom"]):
        draw.line((0, y, w - 1, y), fill=color)
    return out


def nine_patch_image(img: Image.Image, det) -> Image.Image:
    """Build the Android .9.png: panel + 1px marker border."""
    b, p = det.borders, det.padding
    w, h = img.size
    out = Image.new("RGBA", (w + 2, h + 2), (0, 0, 0, 0))
    out.paste(img.convert("RGBA"), (1, 1))
    px = out.load()
    black = (0, 0, 0, 255)
    # top edge: horizontal stretch region (the centre columns)
    for x in range(1 + b["left"], 1 + w - b["right"]):
        px[x, 0] = black
    # left edge: vertical stretch region (the centre rows)
    for y in range(1 + b["top"], 1 + h - b["bottom"]):
        px[0, y] = black
    # right edge: content box, vertically (inside the padding)
    for y in range(1 + p["top"], 1 + h - p["bottom"]):
        px[w + 1, y] = black
    # bottom edge: content box, horizontally (inside the padding)
    for x in range(1 + p["left"], 1 + w - p["right"]):
        px[x, h + 1] = black
    return out
