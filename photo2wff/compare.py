from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops


def compare_images(reference_path: Path, preview_path: Path, threshold: int = 10) -> dict[str, Any]:
    reference = Image.open(reference_path).convert("RGB")
    preview = Image.open(preview_path).convert("RGB").resize(reference.size)
    difference = ImageChops.difference(reference, preview)
    total = reference.width * reference.height
    histogram = difference.histogram()
    absolute_error = sum(index * count for channel in range(3) for index, count in enumerate(histogram[channel * 256:(channel + 1) * 256])) / (total * 3 * 255)
    mask = difference.convert("L").point(lambda value: 255 if value > threshold else 0)
    changed = sum(1 for y in range(mask.height) for x in range(mask.width) if mask.getpixel((x, y))) / total
    bbox = mask.getbbox()
    return {
        "reference": str(reference_path.name),
        "preview": str(preview_path.name),
        "size": [reference.width, reference.height],
        "meanAbsoluteError": round(absolute_error, 6),
        "changedPixelFraction": round(changed, 6),
        "differenceBoundingBox": list(bbox) if bbox else None,
        "threshold": threshold,
    }


def suggest_patches(scene: dict[str, Any], reference_path: Path, preview_path: Path) -> list[dict[str, Any]]:
    """Suggest only cheap, geometric corrections for the primary time band."""
    time_element = next((element for element in scene["elements"] if element["type"] == "TIME"), None)
    if time_element is None:
        return []
    reference = Image.open(reference_path).convert("RGB")
    preview = Image.open(preview_path).convert("RGB")
    bbox = time_element["bbox"]
    region = (bbox["x"], bbox["y"], bbox["x"] + bbox["width"], bbox["y"] + bbox["height"])
    ref_region = reference.crop(region)
    prev_region = preview.crop(region)

    def bounds(image: Image.Image) -> tuple[int, int, int, int] | None:
        pixels = image.load()
        points = []
        for y in range(image.height):
            for x in range(image.width):
                if sum(pixels[x, y]) > 150:
                    points.append((x, y))
        if not points:
            return None
        xs, ys = zip(*points)
        return min(xs), min(ys), max(xs) + 1, max(ys) + 1

    ref_bounds = bounds(ref_region)
    prev_bounds = bounds(prev_region)
    if not ref_bounds or not prev_bounds:
        return []
    ref_center = ((ref_bounds[0] + ref_bounds[2]) / 2, (ref_bounds[1] + ref_bounds[3]) / 2)
    prev_center = ((prev_bounds[0] + prev_bounds[2]) / 2, (prev_bounds[1] + prev_bounds[3]) / 2)
    patches = []
    dx = round(ref_center[0] - prev_center[0])
    dy = round(ref_center[1] - prev_center[1])
    if dx:
        patches.append({"element": time_element["id"], "property": "bbox.x", "delta": dx, "reason": "foreground centroid alignment"})
    if dy:
        patches.append({"element": time_element["id"], "property": "bbox.y", "delta": dy, "reason": "foreground centroid alignment"})
    return patches


def save_report(report: dict[str, Any], path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
