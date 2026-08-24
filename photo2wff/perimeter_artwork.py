from __future__ import annotations

import json
import math
from collections import deque
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw

from .display_geometry import RoundedRect, angle_from_direction, map_element_preserving


def _background_color(image: Image.Image) -> tuple[int, int, int]:
    rgb = image.convert("RGB")
    inset_x = max(1, rgb.width // 16)
    inset_y = max(1, rgb.height // 16)
    samples = (
        rgb.getpixel((inset_x, inset_y)),
        rgb.getpixel((rgb.width - inset_x - 1, inset_y)),
        rgb.getpixel((inset_x, rgb.height - inset_y - 1)),
        rgb.getpixel((rgb.width - inset_x - 1, rgb.height - inset_y - 1)),
    )
    return tuple(sorted(pixel[channel] for pixel in samples)[len(samples) // 2] for channel in range(3))


def _color_distance(pixel: tuple[int, int, int], background: tuple[int, int, int]) -> float:
    return math.sqrt(sum((pixel[channel] - background[channel]) ** 2 for channel in range(3)))


def _foreground_mask(image: Image.Image, background: tuple[int, int, int], threshold: float = 34.0) -> Image.Image:
    rgb = image.convert("RGB")
    mask = Image.new("L", rgb.size, 0)
    source_pixels = rgb.load()
    mask_pixels = mask.load()
    for y in range(rgb.height):
        for x in range(rgb.width):
            if _color_distance(source_pixels[x, y], background) >= threshold:
                mask_pixels[x, y] = 255
    return mask


def _components(mask: Image.Image, minimum_area: int) -> list[list[tuple[int, int]]]:
    pixels = mask.load()
    visited: set[tuple[int, int]] = set()
    components: list[list[tuple[int, int]]] = []
    for y in range(mask.height):
        for x in range(mask.width):
            if not pixels[x, y] or (x, y) in visited:
                continue
            queue = deque([(x, y)])
            visited.add((x, y))
            points: list[tuple[int, int]] = []
            while queue:
                point = queue.popleft()
                points.append(point)
                px, py = point
                for nx in range(max(0, px - 1), min(mask.width, px + 2)):
                    for ny in range(max(0, py - 1), min(mask.height, py + 2)):
                        neighbor = (nx, ny)
                        if neighbor not in visited and pixels[nx, ny]:
                            visited.add(neighbor)
                            queue.append(neighbor)
            if len(points) >= minimum_area:
                components.append(points)
    return components


def _component_bbox(points: list[tuple[int, int]], padding: int, size: tuple[int, int]) -> tuple[int, int, int, int]:
    left = max(0, min(point[0] for point in points) - padding)
    top = max(0, min(point[1] for point in points) - padding)
    right = min(size[0], max(point[0] for point in points) + padding + 1)
    bottom = min(size[1], max(point[1] for point in points) + padding + 1)
    return left, top, right, bottom


def _centroid(points: list[tuple[int, int]]) -> tuple[float, float]:
    return (
        sum(point[0] for point in points) / len(points),
        sum(point[1] for point in points) / len(points),
    )


def _principal_rotation(points: list[tuple[int, int]], center: tuple[float, float]) -> float:
    xx = sum((x - center[0]) ** 2 for x, _ in points)
    yy = sum((y - center[1]) ** 2 for _, y in points)
    xy = sum((x - center[0]) * (y - center[1]) for x, y in points)
    if math.isclose(xx, yy, abs_tol=max(1.0, len(points) * 0.1)) and abs(xy) < len(points):
        return 0.0
    return math.degrees(0.5 * math.atan2(2 * xy, xx - yy))


def _normalized_radius(point: tuple[float, float], shape: RoundedRect) -> float:
    vector = (point[0] - shape.center_x, point[1] - shape.center_y)
    distance = math.hypot(*vector)
    if distance == 0:
        return 0.0
    direction = (vector[0] / distance, vector[1] / distance)
    return distance / shape.boundary_distance(direction)


def _extract_asset(
    image: Image.Image,
    points: list[tuple[int, int]],
    bbox: tuple[int, int, int, int],
    background: tuple[int, int, int],
) -> Image.Image:
    crop = image.convert("RGB").crop(bbox)
    alpha = Image.new("L", crop.size, 0)
    alpha_pixels = alpha.load()
    crop_pixels = crop.load()
    point_set = {(x - bbox[0], y - bbox[1]) for x, y in points}
    for y in range(crop.height):
        for x in range(crop.width):
            distance = _color_distance(crop_pixels[x, y], background)
            if (x, y) in point_set or distance >= 10:
                alpha_pixels[x, y] = max(0, min(255, round((distance - 6) * 5)))
    asset = crop.convert("RGBA")
    asset.putalpha(alpha)
    return asset


def decompose_perimeter_artwork(
    image: Image.Image,
    source_shape: RoundedRect,
    output_root: Path,
    *,
    exclusion_mask: Image.Image | None = None,
    minimum_area: int = 12,
    minimum_normalized_radius: float = 0.62,
) -> dict[str, Any]:
    """Detect foreground artwork near a rounded-rectangle perimeter without OCR."""

    output_root.mkdir(parents=True, exist_ok=True)
    asset_root = output_root / "assets" / "perimeter"
    asset_root.mkdir(parents=True, exist_ok=True)
    background = _background_color(image)
    foreground = _foreground_mask(image, background)
    if exclusion_mask is not None:
        foreground = ImageChops.subtract(foreground, exclusion_mask.convert("L"))

    elements: list[dict[str, Any]] = []
    accepted_mask = Image.new("L", image.size, 0)
    accepted_pixels = accepted_mask.load()
    for points in _components(foreground, minimum_area):
        center = _centroid(points)
        radii = [_normalized_radius((float(x), float(y)), source_shape) for x, y in points]
        radial_position = sum(radii) / len(radii)
        if radial_position < minimum_normalized_radius or max(radii) < minimum_normalized_radius + 0.08:
            continue
        bbox = _component_bbox(points, 2, image.size)
        width, height = bbox[2] - bbox[0], bbox[3] - bbox[1]
        if width > source_shape.width * 0.48 or height > source_shape.height * 0.48:
            continue
        index = len(elements)
        asset_name = f"perimeter_{index:02d}.png"
        _extract_asset(image, points, bbox, background).save(asset_root / asset_name)
        direction = (center[0] - source_shape.center_x, center[1] - source_shape.center_y)
        source_angle = angle_from_direction(direction)
        for x, y in points:
            accepted_pixels[x, y] = 255
        confidence = min(0.99, 0.58 + max(0.0, radial_position - minimum_normalized_radius) * 0.8)
        elements.append(
            {
                "id": f"perimeter_artwork_{index:02d}",
                "type": "STATIC_ARTWORK",
                "dynamic": False,
                "bbox": {"x": bbox[0], "y": bbox[1], "width": width, "height": height},
                "anchor": {"x": round(center[0], 4), "y": round(center[1], 4)},
                "sourceAngleDeg": round(source_angle, 4),
                "perimeterPosition": round(source_angle / 360.0, 6),
                "rotation": round(_principal_rotation(points, center), 4),
                "scale": 1.0,
                "mappingMode": "ELEMENT_PRESERVING",
                "opticalOffsetX": 0.0,
                "opticalOffsetY": 0.0,
                "opticalScale": 1.0,
                "opticalRotation": 0.0,
                "confidence": round(confidence, 4),
                "asset": f"assets/perimeter/{asset_name}",
                "assetInstruction": {"operation": "extract_from_reference"},
                "zIndex": 2,
                "relationships": {
                    "detector": "foreground connected component in normalized rounded-rectangle perimeter band",
                    "normalizedRadius": round(radial_position, 6),
                    "semanticIdentificationRequired": False,
                },
            }
        )

    foreground.save(output_root / "perimeter-foreground-mask.png")
    accepted_mask.save(output_root / "perimeter-artwork-mask.png")
    report = {
        "sourceShape": source_shape.as_dict(),
        "backgroundColor": "#%02X%02X%02X" % background,
        "minimumNormalizedRadius": minimum_normalized_radius,
        "elementCount": len(elements),
        "elements": elements,
        "mask": str(output_root / "perimeter-artwork-mask.png"),
    }
    (output_root / "perimeter-artwork.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n")
    return report


def remove_perimeter_artwork(image: Image.Image, mask: Image.Image, background: tuple[int, int, int] | None = None) -> Image.Image:
    result = image.convert("RGB").copy()
    result.paste(background or _background_color(image), mask=mask.convert("L"))
    return result


def render_element_preserving_mapping(
    background: Image.Image,
    elements: list[dict[str, Any]],
    source_shape: RoundedRect,
    target_shape: RoundedRect,
    source_root: Path,
) -> tuple[Image.Image, list[dict[str, Any]]]:
    canvas = background.convert("RGBA")
    mapped_records: list[dict[str, Any]] = []
    for element in elements:
        mapping = map_element_preserving(element, source_shape, target_shape)
        asset = Image.open(source_root / element["asset"]).convert("RGBA")
        scale = float(mapping["uniformScale"])
        scaled = asset.resize(
            (max(1, round(asset.width * scale)), max(1, round(asset.height * scale))),
            Image.Resampling.LANCZOS,
        )
        rotation = float(mapping["rotation"])
        transformed = scaled.rotate(-rotation, expand=True, resample=Image.Resampling.BICUBIC)
        anchor = mapping["targetAnchor"]
        left = round(float(anchor["x"]) - transformed.width / 2)
        top = round(float(anchor["y"]) - transformed.height / 2)
        canvas.alpha_composite(transformed, (left, top))
        mapped_records.append(
            {
                **mapping,
                "asset": element["asset"],
                "renderedBbox": {"x": left, "y": top, "width": transformed.width, "height": transformed.height},
            }
        )
    return canvas, mapped_records


def draw_perimeter_overlay(image: Image.Image, elements: list[dict[str, Any]], destination: Path) -> None:
    overlay = image.convert("RGBA")
    draw = ImageDraw.Draw(overlay)
    for index, element in enumerate(elements):
        bbox = element["bbox"]
        box = (bbox["x"], bbox["y"], bbox["x"] + bbox["width"], bbox["y"] + bbox["height"])
        draw.rectangle(box, outline=(70, 220, 255, 255), width=2)
        anchor = element["anchor"]
        draw.ellipse((anchor["x"] - 3, anchor["y"] - 3, anchor["x"] + 3, anchor["y"] + 3), fill=(255, 80, 90, 255))
        draw.text((bbox["x"], max(0, bbox["y"] - 12)), str(index), fill=(255, 230, 80, 255))
    destination.parent.mkdir(parents=True, exist_ok=True)
    overlay.convert("RGB").save(destination)
