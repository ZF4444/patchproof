"""Spawn-safe detection entry point."""
import hashlib
import math
from pathlib import Path
from PIL import Image
from patchproof import __version__
from patchproof.core import detect, render_9slice
from patchproof.exports import slice_preview_image


def analyze(path, directory, threshold, sizes, connection):
    try:
        with Image.open(path) as source:
            image = source.convert('RGBA')
        result = detect(image, threshold)
        artifacts = {}
        if result.verdict == 'PROVEN':
            b = result.borders
            preview = slice_preview_image(image, result)
            preview.thumbnail((512, 512))
            preview.save(Path(directory) / 'border.png')
            artifacts['borderPreviewId'] = 'border'
            artifacts['stretchPreviewIds'] = []
            for i, size in enumerate(sizes):
                rendered = render_9slice(image, tuple(b[k] for k in ('left','right','top','bottom')), size['width'], size['height'])
                rendered.save(Path(directory) / f'stretch{i}.png')
                artifacts['stretchPreviewIds'].append(f'stretch{i}')
        connection.send({'input': {'width': image.width, 'height': image.height,
            'pixelFingerprint': 'rgba8-sha256:' + hashlib.sha256(image.width.to_bytes(8,'big') + image.height.to_bytes(8,'big') + image.tobytes()).hexdigest()},
            'detection': {'verdict': result.verdict, 'borders': result.borders if result.verdict == 'PROVEN' else None,
            'transparentPadding': result.padding, 'score': result.score if math.isfinite(result.score) else None,
            'threshold': threshold, 'reason': result.reason or None, 'sizesChecked': result.sizes_checked},
            'engine': {'name':'patchproof','version':__version__}, 'artifacts': artifacts})
    except Exception as exc:
        connection.send({'error': {'code':'DETECTION_FAILED','message':str(exc)}})
    finally:
        connection.close()
