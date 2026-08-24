from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from .model import CANVAS_SIZE


def _font_path(scene_root: Path, family: str | None) -> Path | None:
    if family and family.lower() == "pretendard":
        candidate = scene_root / "assets" / "fonts" / "pretendard.ttf"
        if candidate.exists():
            return candidate
    for candidate in (Path("C:/Windows/Fonts/segoeui.ttf"), Path("C:/Windows/Fonts/arial.ttf"), Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")):
        if candidate.exists():
            return candidate
    return None


def _font(scene_root: Path, style: dict[str, Any]) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    size = max(1, int(style.get("fontSize", 20)))
    path = _font_path(scene_root, style.get("fontFamily"))
    return ImageFont.truetype(str(path), size) if path else ImageFont.load_default()


def _centered_text(draw: ImageDraw.ImageDraw, bbox: dict[str, int], text: str, font: ImageFont.ImageFont, fill: Any, spacing: int = 0) -> None:
    center = (bbox["x"] + bbox["width"] / 2, bbox["y"] + bbox["height"] / 2)
    draw.multiline_text(center, text, font=font, fill=fill, anchor="mm", align="center", spacing=spacing)


def _rgba(style: dict[str, Any]) -> tuple[int, int, int, int]:
    value = style.get("color", "#FFFFFF").lstrip("#")
    if len(value) == 6:
        value += "FF"
    if len(value) != 8:
        return (255, 255, 255, 255)
    red, green, blue, alpha = (int(value[index:index + 2], 16) for index in (0, 2, 4, 6))
    return red, green, blue, int(alpha * float(style.get("alpha", 255)) / 255)


def _clock_seconds(value: str) -> tuple[int, int, int]:
    parts = [int(part) for part in str(value).split(":")[:3]]
    while len(parts) < 3:
        parts.append(0)
    return parts[0] % 24, parts[1] % 60, parts[2] % 60


def _day_of_month(value: str) -> int:
    text = str(value)
    if "-" in text:
        text = text.rsplit("-", 1)[-1]
    elif "." in text:
        text = text.rsplit(".", 1)[-1]
    try:
        return max(1, min(31, int(text)))
    except ValueError:
        return 1


def _analog_angle(role: str, hour: int, minute: int, second: int) -> float:
    if role == "HOUR":
        return ((hour % 12) + minute / 60 + second / 3600) * 30
    if role == "MINUTE":
        return (minute + second / 60) * 6
    return second * 6


def _render_analog_hand(base: Image.Image, element: dict[str, Any], scene_root: Path, angle: float) -> None:
    asset_path = element.get("asset")
    if not asset_path:
        return
    source_path = scene_root / asset_path
    if not source_path.exists():
        return
    bbox = element["bbox"]
    hand = Image.open(source_path).convert("RGBA")
    if hand.size != (bbox["width"], bbox["height"]):
        hand = hand.resize((bbox["width"], bbox["height"]), Image.Resampling.LANCZOS)
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    layer.alpha_composite(hand, (bbox["x"], bbox["y"]))
    rotated = layer.rotate(-angle, resample=Image.Resampling.BICUBIC, center=(219, 219))
    base.alpha_composite(rotated)


def render_scene(scene: dict[str, Any], output_path: Path, scene_root: Path, mode: str = "interactive") -> None:
    background = scene["background"]
    if str(background["type"]).upper() == "IMAGE":
        base = Image.open(scene_root / background["asset"]).convert("RGBA").resize((CANVAS_SIZE, CANVAS_SIZE))
    else:
        color = background["color"].lstrip("#")
        if len(color) == 8:
            color = color[:6]
        base = Image.new("RGBA", (CANVAS_SIZE, CANVAS_SIZE), tuple(int(color[i:i + 2], 16) for i in (0, 2, 4)) + (255,))
    draw = ImageDraw.Draw(base)
    preview = scene.get("preview", {})
    sample_time = preview.get("time", "10:08:00")
    time_parts = str(sample_time).split(":")
    sample_hour, sample_minute, sample_second = _clock_seconds(str(sample_time))
    sample_date = preview.get("date", "08.20")
    sample_weekday = preview.get("weekday", "THU")
    dynamic_values = {
        "BATTERY": f"{preview.get('battery', 82)}%",
        "STEPS": str(preview.get("steps", 5240)),
        "HEART_RATE": str(preview.get("heartRate", 68)),
    }
    ordered_elements = sorted(scene["elements"], key=lambda element: int(element.get("zIndex", 0)))
    for element in ordered_elements:
        element_type = element["type"]
        bbox = element["bbox"]
        style = element.get("style", {})
        fill = _rgba(style)
        if element_type == "TIME":
            _centered_text(draw, bbox, sample_time, _font(scene_root, style), fill)
        elif element_type == "HOUR":
            _centered_text(draw, bbox, time_parts[0], _font(scene_root, style), fill)
        elif element_type == "MINUTE":
            _centered_text(draw, bbox, time_parts[1] if len(time_parts) > 1 else "00", _font(scene_root, style), fill)
        elif element_type == "SECOND":
            _centered_text(draw, bbox, time_parts[2] if len(time_parts) > 2 else "00", _font(scene_root, style), fill)
        elif element_type == "DATE":
            _centered_text(draw, bbox, sample_date, _font(scene_root, style), fill)
        elif element_type == "DYNAMIC_SLOT":
            if element.get("slotType") != "DATE_DAY_OF_MONTH":
                raise ValueError(f"unsupported DYNAMIC_SLOT type: {element.get('slotType')}")
            day = _day_of_month(sample_date)
            value = f"{day:02d}" if element.get("format", "d") == "dd" else str(day)
            _centered_text(draw, bbox, value, _font(scene_root, style), fill)
        elif element_type == "WEEKDAY":
            _centered_text(draw, bbox, sample_weekday, _font(scene_root, style), fill)
        elif element_type in dynamic_values:
            _centered_text(draw, bbox, dynamic_values[element_type], _font(scene_root, style), fill)
        elif element_type == "TEXT":
            asset = element.get("asset")
            if asset and (scene_root / asset).exists():
                overlay = Image.open(scene_root / asset).convert("RGBA")
                base.alpha_composite(overlay, (bbox["x"], bbox["y"]))
            else:
                _centered_text(draw, bbox, element.get("text", ""), _font(scene_root, style), fill)
        elif element_type in {"IMAGE", "ICON", "STATIC_IMAGE"}:
            asset = element.get("asset")
            if asset and (scene_root / asset).exists():
                overlay = Image.open(scene_root / asset).convert("RGBA")
                if overlay.size != (bbox["width"], bbox["height"]):
                    overlay = overlay.resize((bbox["width"], bbox["height"]), Image.Resampling.LANCZOS)
                base.alpha_composite(overlay, (bbox["x"], bbox["y"]))
        elif element_type == "ANALOG_HAND":
            _render_analog_hand(base, element, scene_root, _analog_angle(element["role"], sample_hour, sample_minute, sample_second))
        elif element_type == "RECTANGLE":
            draw.rectangle((bbox["x"], bbox["y"], bbox["x"] + bbox["width"], bbox["y"] + bbox["height"]), fill=fill, outline=fill, width=max(1, int(style.get("strokeWidth", 1))))
        elif element_type == "CIRCLE":
            draw.ellipse((bbox["x"], bbox["y"], bbox["x"] + bbox["width"], bbox["y"] + bbox["height"]), fill=fill, outline=fill, width=max(1, int(style.get("strokeWidth", 1))))
        elif element_type == "LINE":
            draw.line((bbox["x"], bbox["y"], bbox["x"] + bbox["width"], bbox["y"] + bbox["height"]), fill=fill, width=max(1, int(style.get("strokeWidth", 1))))
        elif element_type == "RING":
            draw.arc((bbox["x"], bbox["y"], bbox["x"] + bbox["width"], bbox["y"] + bbox["height"]), 0, 300, fill=fill, width=max(1, int(style.get("strokeWidth", 5))))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    base.convert("RGB").save(output_path)


def render_shape_asset(element: dict[str, Any], path: Path) -> None:
    bbox = element["bbox"]
    image = Image.new("RGBA", (bbox["width"], bbox["height"]), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    style = element.get("style", {})
    fill = _rgba(style)
    if element["type"] == "RECTANGLE":
        draw.rectangle((0, 0, bbox["width"] - 1, bbox["height"] - 1), fill=fill)
    elif element["type"] == "CIRCLE":
        draw.ellipse((0, 0, bbox["width"] - 1, bbox["height"] - 1), fill=fill)
    elif element["type"] == "LINE":
        draw.line((0, 0, bbox["width"] - 1, bbox["height"] - 1), fill=fill, width=max(1, int(style.get("strokeWidth", 1))))
    elif element["type"] == "RING":
        draw.arc((0, 0, bbox["width"] - 1, bbox["height"] - 1), 0, 300, fill=fill, width=max(1, int(style.get("strokeWidth", 5))))
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
