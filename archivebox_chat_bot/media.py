"""Render chat preview images in the PNG format both Slack and Zulip display."""

import io

from PIL import Image


def normalize_image(data: bytes, kind: str) -> bytes:
    bounds = {"screenshot": (1200, 1200), "favicon": (32, 32)}[kind]
    with Image.open(io.BytesIO(data)) as source:
        image = source.convert("RGBA")
        image.thumbnail(bounds)
        output = io.BytesIO()
        image.save(output, format="PNG")
    return output.getvalue()
