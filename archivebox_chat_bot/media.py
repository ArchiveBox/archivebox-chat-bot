"""Render chat preview images in the PNG format both Slack and Zulip display."""

import io
import re
from xml.etree.ElementTree import tostring

from defusedxml.ElementTree import fromstring
from PIL import Image
from resvg_py import svg_to_bytes


def normalize_image(data: bytes, kind: str) -> bytes:
    bounds = {"screenshot": (1200, 1200), "favicon": (32, 32)}[kind]
    if b"<svg" in data[:1024]:
        # Archive content must not read host files or load external SVG resources.
        root = fromstring(data, forbid_dtd=True)
        shapes = {
            "svg",
            "g",
            "defs",
            "path",
            "rect",
            "circle",
            "ellipse",
            "line",
            "polyline",
            "polygon",
            "use",
            "symbol",
            "linearGradient",
            "radialGradient",
            "stop",
            "clipPath",
            "mask",
            "title",
            "desc",
        }
        for parent in root.iter():
            for child in list(parent):
                if child.tag.rsplit("}", 1)[-1] not in shapes:
                    parent.remove(child)
            for key, value in list(parent.attrib.items()):
                if (
                    key.rsplit("}", 1)[-1] == "href"
                    and not value.startswith("#")
                    or "url" in value.lower()
                    and not re.fullmatch(r"url\(#[\w-]+\)", value)
                ):
                    del parent.attrib[key]
        data = svg_to_bytes(
            svg_string=tostring(root, encoding="unicode"), width=bounds[0], height=bounds[1], skip_system_fonts=True
        )
    with Image.open(io.BytesIO(data)) as source:
        image = source.convert("RGBA")
        image.thumbnail(bounds)
        output = io.BytesIO()
        image.save(output, format="PNG")
    return output.getvalue()
