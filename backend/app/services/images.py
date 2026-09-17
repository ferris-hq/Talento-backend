"""Flyer images: validate, orient, shrink and re-encode (which also drops EXIF/GPS metadata)."""

import io
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_SIDE = 1600
MAX_PIXELS = 40_000_000  # refuse decompression bombs early
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}

Image.MAX_IMAGE_PIXELS = MAX_PIXELS


class UnusableImage(ValueError):
    """The upload isn't an image we accept; the message is safe to show the coach."""


def process_flyer(src: Path) -> tuple[bytes, int, int]:
    """Returns (jpeg bytes, width, height)."""
    try:
        with Image.open(src) as probe:
            fmt = probe.format
            probe.verify()
        if fmt not in ALLOWED_FORMATS:
            raise UnusableImage("Upload the flyer as a JPG, PNG or WebP image.")
        with Image.open(src) as img:
            img = ImageOps.exif_transpose(img)
            if img.mode in ("RGBA", "LA", "P"):
                rgba = img.convert("RGBA")
                flat = Image.new("RGB", rgba.size, (255, 255, 255))
                flat.paste(rgba, mask=rgba.getchannel("A"))
                img = flat
            else:
                img = img.convert("RGB")
            if min(img.size) < 200:
                raise UnusableImage("The flyer is too small. Use an image at least 200 px wide.")
            img.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
            out = io.BytesIO()
            img.save(out, "JPEG", quality=85, optimize=True, progressive=True)
            return out.getvalue(), img.width, img.height
    except UnusableImage:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, SyntaxError) as exc:
        raise UnusableImage("We couldn't read this image. Try a different JPG or PNG.") from exc
