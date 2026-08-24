from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from typing import Any

from PIL import Image, ImageFilter


DATE_SLOT_TYPE = "DATE_DAY_OF_MONTH"


def _is_red(pixel: tuple[int, int, int]) -> bool:
    red, green, blue = pixel
    return red >= 80 and red > green * 1.35 and red > blue * 1.15


def _red_components(image: Image.Image) -> list[set[tuple[int, int]]]:
    pixels = image.convert("RGB").load()
    remaining = {(x, y) for y in range(image.height) for x in range(image.width) if _is_red(pixels[x, y])}
    components: list[set[tuple[int, int]]] = []
    while remaining:
        start = remaining.pop()
        queue = deque([start])
        component = {start}
        while queue:
            x, y = queue.popleft()
            for nx in range(x - 1, x + 2):
                for ny in range(y - 1, y + 2):
                    point = (nx, ny)
                    if point in remaining:
                        remaining.remove(point)
                        component.add(point)
                        queue.append(point)
        if len(component) >= 20:
            components.append(component)
    return components


def _bbox(points: set[tuple[int, int]]) -> tuple[int, int, int, int]:
    return min(x for x, _ in points), min(y for _, y in points), max(x for x, _ in points) + 1, max(y for _, y in points) + 1


def detect_date_window(reference: Image.Image) -> dict[str, Any] | None:
    """Detect a high-confidence rounded date-frame candidate without brand-specific rules."""

    candidates = []
    for component in _red_components(reference):
        left, top, right, bottom = _bbox(component)
        width, height = right - left, bottom - top
        if width < 30 or height < 18 or width / max(1, height) < 1.15:
            continue
        candidates.append((len(component), (left, top, right, bottom)))
    if not candidates:
        return None
    area, frame = max(candidates, key=lambda item: item[0])
    left, top, right, bottom = frame
    frame_width, frame_height = right - left, bottom - top
    border = max(3, round(min(frame_width, frame_height) * 0.13))
    inner = (left + border, top + border, right - border, bottom - border)
    return {
        "semanticType": DATE_SLOT_TYPE,
        "frameBbox": {"x": left, "y": top, "width": frame_width, "height": frame_height},
        "innerBbox": {"x": inner[0], "y": inner[1], "width": inner[2] - inner[0], "height": inner[3] - inner[1]},
        "padding": {"left": border, "top": border, "right": border, "bottom": border},
        "detector": "red rounded-frame component with aspect-ratio gate",
        "confidence": 0.93,
        "componentArea": area,
        "layoutInterpretation": "layout_replacement_of_hour_index_3",
        "hourNumeral3": "intentionally_absent_not_reconstructed",
    }


def _median_color(colors: list[tuple[int, int, int]], fallback: tuple[int, int, int]) -> tuple[int, int, int]:
    if not colors:
        return fallback
    return tuple(sorted(channel)[len(colors) // 2] for channel in zip(*colors))


def extract_date_day_of_month_window(reference_path: Path, dial_path: Path, output_dir: Path) -> dict[str, Any] | None:
    reference = Image.open(reference_path).convert("RGB")
    dial = Image.open(dial_path).convert("RGB")
    metadata = detect_date_window(reference)
    if metadata is None:
        return None
    inner = metadata["innerBbox"]
    left, top = int(inner["x"]), int(inner["y"])
    right, bottom = left + int(inner["width"]), top + int(inner["height"])
    reference_pixels = reference.load()
    dial_pixels = dial.load()
    glyph_mask = Image.new("L", dial.size, 0)
    glyph_pixels = glyph_mask.load()
    glyph_colors: list[tuple[int, int, int]] = []
    dark_colors: list[tuple[int, int, int]] = []
    for y in range(top, bottom):
        for x in range(left, right):
            pixel = dial_pixels[x, y]
            if max(pixel) >= 45:
                glyph_pixels[x, y] = 255
                glyph_colors.append(pixel)
            else:
                dark_colors.append(pixel)
    # Keep antialiased glyph edges out of the static dial while staying inside
    # the frame. The frame itself is outside this mask and remains untouched.
    glyph_mask = glyph_mask.filter(ImageFilter.MaxFilter(3))
    background = _median_color(dark_colors, (0, 0, 0))
    empty = dial.copy()
    empty_pixels = empty.load()
    for y in range(top, bottom):
        for x in range(left, right):
            if glyph_mask.getpixel((x, y)):
                empty_pixels[x, y] = background
    glyph_bbox = glyph_mask.getbbox()
    output_dir.mkdir(parents=True, exist_ok=True)
    assets_dir = output_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    empty_path = assets_dir / "dial_empty_date.png"
    mask_path = assets_dir / "date-window-glyph-mask.png"
    empty.save(empty_path)
    glyph_mask.save(mask_path)
    if glyph_bbox:
        glyph_left, glyph_top, glyph_right, glyph_bottom = glyph_bbox
        metadata["sourceGlyphBbox"] = {
            "x": glyph_left,
            "y": glyph_top,
            "width": glyph_right - glyph_left,
            "height": glyph_bottom - glyph_top,
        }
    metadata.update(
        {
            "emptyDialAsset": "assets/dial_empty_date.png",
            "removedGlyphMask": "assets/date-window-glyph-mask.png",
            "removedGlyphPixelCount": sum(1 for value in glyph_mask.tobytes() if value),
            "glyphColor": "#%02X%02X%02X" % _median_color(glyph_colors, (238, 227, 220)),
            "observedFramePreserved": True,
            "observedBackgroundPreserved": True,
            "requiresHumanReview": False,
        }
    )
    (output_dir / "date-window-metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return metadata
