from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from PIL import Image

from .model import CANVAS_SIZE


def _time_parts(value: str) -> tuple[int, int, int]:
    parts = [int(part) for part in value.split(":")[:3]]
    while len(parts) < 3:
        parts.append(0)
    return parts[0] % 24, parts[1] % 60, parts[2] % 60


def _angle(tag: str, value: str) -> float:
    hour, minute, second = _time_parts(value)
    if tag == "HourHand":
        return ((hour % 12) + minute / 60 + second / 3600) * 30
    if tag == "MinuteHand":
        return (minute + second / 60) * 6
    return second * 6


def _resource(drawable: Path, name: str) -> Image.Image:
    path = drawable / f"{name}.png"
    if not path.exists():
        raise FileNotFoundError(f"WFF resource not found: {path}")
    return Image.open(path).convert("RGBA")


def _part_image(base: Image.Image, node: ET.Element, drawable: Path) -> None:
    image_node = node.find("Image")
    if image_node is None:
        return
    image = _resource(drawable, image_node.attrib["resource"])
    width = int(float(node.attrib["width"]))
    height = int(float(node.attrib["height"]))
    if image.size != (width, height):
        image = image.resize((width, height), Image.Resampling.LANCZOS)
    base.alpha_composite(image, (int(float(node.attrib["x"])), int(float(node.attrib["y"]))))


def _hand(base: Image.Image, node: ET.Element, drawable: Path, fixed_time: str) -> None:
    width = int(float(node.attrib["width"]))
    height = int(float(node.attrib["height"]))
    x = int(float(node.attrib["x"]))
    y = int(float(node.attrib["y"]))
    pivot_x = float(node.attrib.get("pivotX", "0.5"))
    pivot_y = float(node.attrib.get("pivotY", "0.5"))
    image = _resource(drawable, node.attrib["resource"])
    if image.size != (width, height):
        image = image.resize((width, height), Image.Resampling.LANCZOS)
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    layer.alpha_composite(image, (x, y))
    pivot = (x + width * pivot_x, y + height * pivot_y)
    rotated = layer.rotate(-_angle(node.tag, fixed_time), resample=Image.Resampling.BICUBIC, center=pivot)
    base.alpha_composite(rotated)


def render_wff_xml(xml_path: Path, output_path: Path, fixed_time: str | None = None) -> dict[str, str]:
    """Render the compiled WFF XML using only its Scene and drawable resources.

    This is a deterministic format-level renderer for CI. A Wear OS device/emulator
    capture remains a separate verification tier.
    """
    root = ET.parse(xml_path).getroot()
    drawable = xml_path.parent.parent / "drawable"
    metadata = {node.attrib.get("key"): node.attrib.get("value", "") for node in root.findall("Metadata")}
    render_time = fixed_time or metadata.get("PREVIEW_TIME", "10:08:30")
    background = root.find("Scene")
    if background is None:
        raise ValueError("WFF XML has no Scene")
    color = background.attrib.get("backgroundColor", "#000000").lstrip("#")
    if len(color) == 6:
        base = Image.new("RGBA", (CANVAS_SIZE, CANVAS_SIZE), tuple(int(color[index:index + 2], 16) for index in (0, 2, 4)) + (255,))
    else:
        base = Image.new("RGBA", (CANVAS_SIZE, CANVAS_SIZE), "black")
    for node in background:
        if node.tag == "PartImage":
            _part_image(base, node, drawable)
        elif node.tag == "AnalogClock":
            for hand in node:
                if hand.tag in {"HourHand", "MinuteHand", "SecondHand"}:
                    _hand(base, hand, drawable, render_time)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    base.convert("RGB").save(output_path)
    return {"renderer": "photo2wff-wff-xml", "fixedTime": render_time, "sourceXml": str(xml_path)}
