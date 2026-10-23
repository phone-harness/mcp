"""Screenshots sized for a model, and the sentence that maps them back to the screen.

A phone's capture is a PNG of a few megabytes. The model does not need that:
following mobile-mcp, the image goes out as JPEG with its long side at most
`max_size` pixels, and a line of text says how to turn a point seen in the
image into a tap on the screen. Taps always use screen coordinates.
"""
from __future__ import annotations

from io import BytesIO

from PIL import Image as PILImage

DEFAULT_MAX_SIZE = 1024
JPEG_QUALITY = 75


def image_size(data: bytes) -> tuple[int, int]:
    with PILImage.open(BytesIO(data)) as im:
        return im.size


def shrink(png: bytes, max_size: int | None = DEFAULT_MAX_SIZE, scale: float | None = None,
           fmt: str = "JPEG") -> tuple[bytes, tuple[int, int], str]:
    """-> (bytes, (w, h), mime). `scale` wins over `max_size`; neither means the original size."""
    with PILImage.open(BytesIO(png)) as im:
        im.load()
        w, h = im.size
        if scale and scale > 0:
            factor = min(float(scale), 1.0)
        elif max_size and max_size > 0 and max(w, h) > max_size:
            factor = max_size / max(w, h)
        else:
            factor = 1.0
        if factor < 1.0:
            im = im.resize((max(1, round(w * factor)), max(1, round(h * factor))), PILImage.LANCZOS)
        fmt = fmt.upper()
        if fmt == "JPEG" and im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        out = BytesIO()
        if fmt == "JPEG":
            im.save(out, "JPEG", quality=JPEG_QUALITY, optimize=True)
            return out.getvalue(), im.size, "image/jpeg"
        im.save(out, "PNG", optimize=True)
        return out.getvalue(), im.size, "image/png"


def describe_mapping(image: tuple[int, int], screen: tuple[int, int]) -> str | None:
    """mobile-mcp's wording, so an agent that learned it there needs no new rule."""
    iw, ih = image
    sw, sh = screen
    if min(iw, ih, sw, sh) <= 0:
        return None
    if (iw, ih) == (sw, sh):
        return f"Screenshot is {iw}x{ih} and its coordinates match the screen."
    return (f"Screenshot is {iw}x{ih}. Screen coordinates are {sw}x{sh}. To tap something you see in this "
            f"screenshot, multiply its x by {sw / iw:.3g} and y by {sh / ih:.3g}.")
