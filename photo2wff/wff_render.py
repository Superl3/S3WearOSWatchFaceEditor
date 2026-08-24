from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

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


def _day_value(fixed_date: str, padded: bool = False) -> str:
    value = str(fixed_date)
    if "-" in value:
        value = value.rsplit("-", 1)[-1]
    elif "." in value:
        value = value.rsplit(".", 1)[-1]
    try:
        day = max(1, min(31, int(value)))
    except ValueError:
        day = 1
    return f"{day:02d}" if padded else str(day)


def _parameter_value(expression: str, fixed_date: str) -> str:
    if expression == "[DAY]":
        return _day_value(fixed_date)
    if expression == "[DAY_Z]":
        return _day_value(fixed_date, padded=True)
    if expression == "[MONTH_Z]":
        normalized = str(fixed_date).replace("/", "-")
        parts = normalized.split("-")
        value = parts[-2] if len(parts) >= 3 else parts[0].split(".")[0]
        try:
            return f"{max(1, min(12, int(value))):02d}"
        except ValueError:
            return "01"
    return expression


def _font_for(xml_path: Path, attributes: dict[str, str]) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    family = attributes.get("family", "").lower()
    candidates = []
    if family == "pretendard":
        candidates.append(xml_path.parent.parent / "font" / "pretendard.ttf")
    candidates.extend((Path("C:/Windows/Fonts/segoeui.ttf"), Path("C:/Windows/Fonts/arial.ttf")))
    for candidate in candidates:
        if candidate.exists():
            return ImageFont.truetype(str(candidate), max(1, int(float(attributes.get("size", "20")))))
    return ImageFont.load_default()


def _color(value: str) -> tuple[int, int, int, int]:
    raw = str(value or "#FFFFFF").lstrip("#")
    if len(raw) == 8:
        alpha = int(raw[:2], 16)
        raw = raw[2:]
    else:
        alpha = 255
    if len(raw) != 6:
        return 255, 255, 255, alpha
    return int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16), alpha


def _part_text(base: Image.Image, node: ET.Element, xml_path: Path, fixed_date: str, bitmap_fonts: dict[str, dict[str, dict[str, str]]]) -> None:
    text_node = node.find("Text")
    if text_node is None:
        return
    font_node = text_node.find("Font")
    bitmap_font_node = text_node.find("BitmapFont")
    if font_node is None and bitmap_font_node is None:
        return
    if bitmap_font_node is not None:
        font_node = bitmap_font_node
    template = font_node.find("Template")
    if template is not None:
        value = str(template.text or "").strip()
        for parameter in template.findall("Parameter"):
            parameter_value = _parameter_value(parameter.attrib.get("expression", ""), fixed_date)
            if "%d" in value:
                value = value.replace("%d", str(int(parameter_value)), 1)
            else:
                value = value.replace("%s", parameter_value, 1)
        text = value
    else:
        text = str(font_node.text or "")
    width = int(float(node.attrib.get("width", "0")))
    height = int(float(node.attrib.get("height", "0")))
    if width <= 0 or height <= 0:
        return
    align = text_node.attrib.get("align", "CENTER").upper()
    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    if bitmap_font_node is not None:
        family = bitmap_font_node.attrib.get("family", "")
        catalog = bitmap_fonts.get(family, {})
        glyphs: list[Image.Image] = []
        font_size = max(1, round(float(bitmap_font_node.attrib.get("size", "20"))))
        for character in text:
            definition = catalog.get(character)
            if definition is None:
                raise ValueError(f"BitmapFont '{family}' has no glyph for '{character}'")
            glyph = _resource(xml_path.parent.parent / "drawable", definition["resource"])
            metric_width = max(1, int(definition.get("width", glyph.width)))
            metric_height = max(1, int(definition.get("height", glyph.height)))
            target_width = max(1, round(metric_width * font_size / metric_height))
            glyphs.append(glyph.resize((target_width, font_size), Image.Resampling.LANCZOS))
        total_width = sum(glyph.width for glyph in glyphs)
        x = 0 if align in {"LEFT", "START"} else width - total_width if align in {"RIGHT", "END"} else (width - total_width) / 2
        y = max(0, (height - font_size) // 2)
        for glyph in glyphs:
            layer.alpha_composite(glyph, (round(x), y))
            x += glyph.width
    else:
        draw = ImageDraw.Draw(layer)
        font = _font_for(xml_path, font_node.attrib)
        x = 0 if align in {"LEFT", "START"} else width if align in {"RIGHT", "END"} else width / 2
        anchor = "lm" if align in {"LEFT", "START"} else "rm" if align in {"RIGHT", "END"} else "mm"
        draw.text((x, height / 2), text, font=font, fill=_color(font_node.attrib.get("color", "#FFFFFF")), anchor=anchor)
    base.alpha_composite(layer, (int(float(node.attrib.get("x", "0"))), int(float(node.attrib.get("y", "0")))))


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


def _condition_matches(expression: str, fixed_date: str) -> bool:
    day = int(_day_value(fixed_date))
    clauses = [clause.strip() for clause in expression.split("||")]
    for clause in clauses:
        if clause.startswith("[DAY] ==") and day == int(clause.split("==", 1)[1].strip()):
            return True
    return False


def _condition_child(node: ET.Element, fixed_date: str) -> ET.Element | None:
    expressions_node = node.find("Expressions")
    expressions = {
        expression.attrib.get("name", ""): str(expression.text or "").strip()
        for expression in expressions_node.findall("Expression")
    } if expressions_node is not None else {}
    for compare in node.findall("Compare"):
        if _condition_matches(expressions.get(compare.attrib.get("expression", ""), ""), fixed_date):
            return next(iter(compare), None)
    default = node.find("Default")
    return next(iter(default), None) if default is not None else None


def _render_node(
    base: Image.Image,
    node: ET.Element,
    xml_path: Path,
    drawable: Path,
    fixed_time: str,
    fixed_date: str,
    bitmap_fonts: dict[str, dict[str, dict[str, str]]],
) -> None:
    if node.tag == "PartImage":
        _part_image(base, node, drawable)
    elif node.tag == "PartText":
        _part_text(base, node, xml_path, fixed_date, bitmap_fonts)
    elif node.tag == "AnalogClock":
        for hand in node:
            if hand.tag in {"HourHand", "MinuteHand", "SecondHand"}:
                _hand(base, hand, drawable, fixed_time)
    elif node.tag == "Condition":
        selected = _condition_child(node, fixed_date)
        if selected is not None:
            _render_node(base, selected, xml_path, drawable, fixed_time, fixed_date, bitmap_fonts)


def render_wff_xml(xml_path: Path, output_path: Path, fixed_time: str | None = None, fixed_date: str | None = None) -> dict[str, str]:
    """Render the compiled WFF XML using only its Scene and drawable resources.

    This is a deterministic format-level renderer for CI. A Wear OS device/emulator
    capture remains a separate verification tier.
    """
    root = ET.parse(xml_path).getroot()
    drawable = xml_path.parent.parent / "drawable"
    metadata = {node.attrib.get("key"): node.attrib.get("value", "") for node in root.findall("Metadata")}
    bitmap_fonts: dict[str, dict[str, dict[str, str]]] = {}
    bitmap_fonts_node = root.find("BitmapFonts")
    if bitmap_fonts_node is not None:
        for bitmap_font in bitmap_fonts_node.findall("BitmapFont"):
            bitmap_fonts[bitmap_font.attrib["name"]] = {character.attrib["name"]: dict(character.attrib) for character in bitmap_font.findall("Character")}
    render_time = fixed_time or metadata.get("PREVIEW_TIME", "10:08:30")
    render_date = fixed_date or metadata.get("PREVIEW_DATE", "08.20")
    background = root.find("Scene")
    if background is None:
        raise ValueError("WFF XML has no Scene")
    color = background.attrib.get("backgroundColor", "#000000").lstrip("#")
    if len(color) == 6:
        base = Image.new("RGBA", (CANVAS_SIZE, CANVAS_SIZE), tuple(int(color[index:index + 2], 16) for index in (0, 2, 4)) + (255,))
    else:
        base = Image.new("RGBA", (CANVAS_SIZE, CANVAS_SIZE), "black")
    for node in background:
        _render_node(base, node, xml_path, drawable, render_time, render_date, bitmap_fonts)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    base.convert("RGB").save(output_path)
    return {"renderer": "photo2wff-wff-xml", "fixedTime": render_time, "sourceXml": str(xml_path)}
