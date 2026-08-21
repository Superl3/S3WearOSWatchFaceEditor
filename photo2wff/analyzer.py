from __future__ import annotations

from collections import deque
import math
from pathlib import Path
from typing import Any

from PIL import Image
from PIL import ImageDraw

from .model import CANVAS_SIZE


def _background_color(image: Image.Image) -> tuple[int, int, int]:
    pixels = image.convert("RGB")
    samples: list[tuple[int, int, int]] = []
    for x0, y0 in ((0, 0), (CANVAS_SIZE - 8, 0), (0, CANVAS_SIZE - 8), (CANVAS_SIZE - 8, CANVAS_SIZE - 8)):
        for y in range(y0, y0 + 8):
            for x in range(x0, x0 + 8):
                samples.append(pixels.getpixel((x, y)))
    return tuple(sorted(channel)[len(samples) // 2] for channel in zip(*samples))


def _components(image: Image.Image, background: tuple[int, int, int], threshold: int = 26) -> list[dict[str, int]]:
    rgb = image.convert("RGB")
    width, height = rgb.size
    mask = bytearray(width * height)
    for y in range(height):
        for x in range(width):
            r, g, b = rgb.getpixel((x, y))
            distance = abs(r - background[0]) + abs(g - background[1]) + abs(b - background[2])
            if distance >= threshold:
                mask[y * width + x] = 1
    components: list[dict[str, int]] = []
    for start in range(width * height):
        if not mask[start]:
            continue
        mask[start] = 0
        queue = deque([start])
        area = 0
        min_x = max_x = start % width
        min_y = max_y = start // width
        while queue:
            position = queue.popleft()
            x = position % width
            y = position // width
            area += 1
            min_x = min(min_x, x)
            max_x = max(max_x, x)
            min_y = min(min_y, y)
            max_y = max(max_y, y)
            for next_position in (position - 1, position + 1, position - width, position + width):
                if next_position < 0 or next_position >= width * height or not mask[next_position]:
                    continue
                if next_position == position - 1 and x == 0:
                    continue
                if next_position == position + 1 and x == width - 1:
                    continue
                mask[next_position] = 0
                queue.append(next_position)
        if area >= 5:
            components.append({"x": min_x, "y": min_y, "width": max_x - min_x + 1, "height": max_y - min_y + 1, "area": area})
    return components


def _line_groups(components: list[dict[str, int]]) -> list[dict[str, Any]]:
    groups: list[list[dict[str, int]]] = []
    for component in sorted(components, key=lambda item: (item["y"], item["x"])):
        center = component["y"] + component["height"] / 2
        placed = False
        for group in groups:
            group_center = sum(item["y"] + item["height"] / 2 for item in group) / len(group)
            if abs(center - group_center) <= max(9, min(component["height"], 48) * 0.75):
                group.append(component)
                placed = True
                break
        if not placed:
            groups.append([component])
    result = []
    for group in groups:
        x = min(item["x"] for item in group)
        y = min(item["y"] for item in group)
        right = max(item["x"] + item["width"] for item in group)
        bottom = max(item["y"] + item["height"] for item in group)
        result.append({"x": x, "y": y, "width": right - x, "height": bottom - y, "area": sum(item["area"] for item in group)})
    return sorted(result, key=lambda item: item["y"])


def _save_alpha_crop(image: Image.Image, background: tuple[int, int, int], bbox: dict[str, int], path: Path) -> None:
    rgb = image.convert("RGB")
    left, top = bbox["x"], bbox["y"]
    right, bottom = left + bbox["width"], top + bbox["height"]
    crop = rgb.crop((left, top, right, bottom)).convert("RGBA")
    data = []
    for y in range(crop.height):
        for x in range(crop.width):
            red, green, blue, _ = crop.getpixel((x, y))
            distance = abs(red - background[0]) + abs(green - background[1]) + abs(blue - background[2])
            data.append((red, green, blue, min(255, max(0, (distance - 12) * 12))))
    crop.putdata(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    crop.save(path)


def _normalization(source_size: list[int]) -> dict[str, Any]:
    width, height = source_size
    if (width, height) == (CANVAS_SIZE, CANVAS_SIZE):
        input_type = "SCREENSHOT"
        confidence = 0.98
    elif width > 0 and height > 0 and abs(width / height - 1.0) <= 0.03:
        input_type = "CROPPED_SCREEN"
        confidence = 0.86
    else:
        input_type = "UNCERTAIN"
        confidence = 0.45
    return {
        "inputType": input_type,
        "rotationDegrees": 0.0,
        "confidence": confidence,
        "requiresPerspectiveCorrection": input_type == "UNCERTAIN",
        "sourceSize": source_size,
    }


def analyze_image(reference_path: Path, output_dir: Path) -> dict[str, Any]:
    image = Image.open(reference_path).convert("RGB")
    if image.size != (CANVAS_SIZE, CANVAS_SIZE):
        image = image.resize((CANVAS_SIZE, CANVAS_SIZE), Image.Resampling.LANCZOS)
    output_dir.mkdir(parents=True, exist_ok=True)
    assets_dir = output_dir / "assets"
    assets_dir.mkdir(exist_ok=True)
    normalized_reference = output_dir / "reference.png"
    image.save(normalized_reference)

    background = _background_color(image)
    components = _components(image, background)
    groups = _line_groups(components)
    time_candidate = next(
        (
            group
            for group in sorted(groups, key=lambda item: item["area"], reverse=True)
            if group["width"] >= 130 and group["height"] >= 28 and 55 <= group["y"] <= 285
        ),
        None,
    )
    elements: list[dict[str, Any]] = []
    used_group_ids: set[int] = set()
    if time_candidate is not None:
        index = groups.index(time_candidate)
        used_group_ids.add(index)
        bbox = {
            "x": max(0, time_candidate["x"] - 8),
            "y": max(0, time_candidate["y"] - 8),
            "width": min(CANVAS_SIZE - max(0, time_candidate["x"] - 8), time_candidate["width"] + 16),
            "height": min(CANVAS_SIZE - max(0, time_candidate["y"] - 8), time_candidate["height"] + 16),
        }
        elements.append(
            {
                "id": "time_primary",
                "type": "TIME",
                "dynamic": True,
                "bbox": bbox,
                "format": "HH:mm",
                "style": {
                    "fontFamily": "Pretendard",
                    "fontWeight": 400,
                    "fontSize": max(24, min(132, int(time_candidate["height"] * 1.25))),
                    "letterSpacing": 0,
                    "alignment": "center",
                    "color": "#FFFFFF",
                },
                "confidence": 0.86,
                "relationships": {"centeredToCanvas": True, "group": "primary_time_cluster"},
            }
        )

    time_bounds = None
    if time_candidate is not None:
        time_bounds = (
            time_candidate["x"] - 4,
            time_candidate["y"] - 4,
            time_candidate["x"] + time_candidate["width"] + 4,
            time_candidate["y"] + time_candidate["height"] + 4,
        )
    remaining = [(index, group) for index, group in enumerate(groups) if index not in used_group_ids]
    inferred_secondary = 0
    for ordinal, (index, group) in enumerate(remaining):
        if group["width"] < 4 or group["height"] < 4:
            continue
        if time_bounds is not None:
            left, top, right, bottom = time_bounds
            overlaps_time = group["x"] < right and group["x"] + group["width"] > left and group["y"] < bottom and group["y"] + group["height"] > top
            if overlaps_time:
                continue
        if time_candidate is not None and group["y"] > time_candidate["y"] + time_candidate["height"] and group["width"] <= 180 and group["height"] <= 42 and inferred_secondary < 2:
            inferred_secondary += 1
            inferred_type = "DATE" if inferred_secondary == 1 else "WEEKDAY"
            inferred_id = "date_secondary" if inferred_secondary == 1 else "weekday_secondary"
            elements.append(
                {
                    "id": inferred_id,
                    "type": inferred_type,
                    "dynamic": True,
                    "bbox": {key: group[key] for key in ("x", "y", "width", "height")},
                    "format": "MM.dd" if inferred_type == "DATE" else "EEE",
                    "style": {
                        "fontFamily": "Pretendard",
                        "fontWeight": 400,
                        "fontSize": max(12, int(group["height"] * 1.35)),
                        "color": "#FFFFFF",
                        "alignment": "center",
                    },
                    "confidence": 0.52,
                    "relationships": {"centeredToCanvas": True, "alignedWith": ["time_primary"], "group": "primary_time_cluster"},
                    "uncertainty": ["Could be date or another short numeric field" if inferred_type == "DATE" else "Could be weekday or another short label"],
                }
            )
            continue
        asset_name = f"static_{ordinal:02d}.png"
        _save_alpha_crop(image, background, group, assets_dir / asset_name)
        if group["width"] <= 220 and group["y"] > (time_candidate["y"] + time_candidate["height"] if time_candidate else 180):
            element_type = "STATIC_IMAGE"
            element_id = "secondary_text" if ordinal == 0 else f"secondary_text_{ordinal:02d}"
            elements.append(
                {
                    "id": element_id,
                    "type": element_type,
                    "dynamic": False,
                    "bbox": {key: group[key] for key in ("x", "y", "width", "height")},
                    "style": {"fontFamily": "Pretendard", "fontSize": max(12, int(group["height"] * 1.35)), "color": "#FFFFFF", "alignment": "center"},
                    "asset": f"assets/{asset_name}",
                    "assetInstruction": {"operation": "extract_from_reference"},
                    "confidence": 0.44,
                    "uncertainty": ["Text is visible but semantic content was not recovered"],
                }
            )
        else:
            elements.append(
                {
                    "id": f"static_{ordinal:02d}",
                    "type": "STATIC_IMAGE",
                    "dynamic": False,
                    "bbox": {key: group[key] for key in ("x", "y", "width", "height")},
                    "asset": f"assets/{asset_name}",
                    "assetInstruction": {"operation": "extract_from_reference"},
                    "confidence": 0.58,
                }
            )

    if time_candidate is None and not elements:
        image.save(assets_dir / "reference_full.png")
        elements.append(
            {
                "id": "reference_full",
                "type": "STATIC_IMAGE",
                "dynamic": False,
                "bbox": {"x": 0, "y": 0, "width": 438, "height": 438},
                "asset": "assets/reference_full.png",
                "assetInstruction": {"operation": "extract_from_reference"},
                "confidence": 0.35,
                "uncertainty": ["No reliable semantic element was detected"],
            }
        )

    with Image.open(reference_path) as source_image:
        source_size = list(source_image.size)
    for element in elements:
        element.setdefault("rotation", 0)
    scene = {
        "schemaVersion": "1.0",
        "canvas": {"width": CANVAS_SIZE, "height": CANVAS_SIZE, "shape": "CIRCLE", "centerX": 219, "centerY": 219},
        "normalization": _normalization(source_size),
        "background": {"type": "SOLID", "color": "#%02X%02X%02X" % background},
        "preview": {"time": "10:08", "date": "08.20", "weekday": "THU", "battery": 82, "steps": 5240, "heartRate": 68},
        "elements": elements,
        "analysis": {
            "watchFaceCategory": "MINIMAL_DIGITAL",
            "overallConfidence": round(sum(element["confidence"] for element in elements) / len(elements), 2) if elements else 0.0,
            "requiresStaticAssetExtraction": any(element["type"] == "STATIC_IMAGE" for element in elements),
            "requiresHumanReview": any(element["confidence"] < 0.65 or element.get("uncertainty") for element in elements),
            "method": "dominant-corner-background + connected-components + horizontal-band heuristic",
            "componentCount": len(components),
            "groupCount": len(groups),
        },
    }
    return scene


def _largest_dark_component(image: Image.Image, threshold: int = 75) -> tuple[int, int, int, int] | None:
    """Find the connected dark watch body/display region in a product photograph."""
    rgb = image.convert("RGB")
    width, height = rgb.size
    mask = bytearray(width * height)
    pixels = rgb.load()
    for y in range(height):
        for x in range(width):
            if max(pixels[x, y]) < threshold:
                mask[y * width + x] = 1
    best: tuple[int, int, int, int, int] | None = None
    for start in range(width * height):
        if not mask[start]:
            continue
        queue = deque([start])
        mask[start] = 0
        count = 0
        min_x = max_x = start % width
        min_y = max_y = start // width
        while queue:
            position = queue.popleft()
            x = position % width
            y = position // width
            count += 1
            min_x = min(min_x, x)
            max_x = max(max_x, x)
            min_y = min(min_y, y)
            max_y = max(max_y, y)
            for next_position in (position - 1, position + 1, position - width, position + width):
                if next_position < 0 or next_position >= width * height or not mask[next_position]:
                    continue
                if next_position == position - 1 and x == 0:
                    continue
                if next_position == position + 1 and x == width - 1:
                    continue
                mask[next_position] = 0
                queue.append(next_position)
        candidate = (count, min_x, min_y, max_x + 1, max_y + 1)
        if best is None or candidate[0] > best[0]:
            best = candidate
    return None if best is None else best[1:]


def _clock_endpoint(center: tuple[float, float], angle_degrees: float, length: float) -> tuple[int, int]:
    radians = math.radians(angle_degrees)
    return (
        round(center[0] + math.sin(radians) * length),
        round(center[1] - math.cos(radians) * length),
    )


def _write_a1_assets(canvas: Image.Image, assets_dir: Path) -> dict[str, dict[str, Any]]:
    """Create conservative, adjustable A1 hand assets from the normalized reference.

    The geometry is deliberately explicit for A1a. A1b can replace these values with
    detected masks without changing the scene or compiler contract.
    """
    center = (219.0, 219.0)
    hands = {
        "HOUR": {"angle": 306.0, "length": 77.0, "thickness": 8.0, "tail": 12.0, "width": 24, "color": (244, 238, 235, 255)},
        "MINUTE": {"angle": 54.0, "length": 142.0, "thickness": 8.0, "tail": 10.0, "width": 20, "color": (245, 245, 245, 255)},
        "SECOND": {"angle": 180.0, "length": 178.0, "thickness": 3.0, "tail": 9.0, "width": 8, "color": (175, 18, 45, 255)},
    }
    assets_dir.mkdir(parents=True, exist_ok=True)
    dial_clean = canvas.convert("RGBA")
    dial_draw = ImageDraw.Draw(dial_clean)
    for hand in hands.values():
        endpoint = _clock_endpoint(center, hand["angle"], hand["length"])
        erase_width = round(hand["thickness"] + (6 if hand["thickness"] >= 8 else 3))
        dial_draw.line((center[0], center[1], endpoint[0], endpoint[1]), fill=(0, 0, 0, 255), width=erase_width)
        tail_endpoint = _clock_endpoint(center, (hand["angle"] + 180.0) % 360.0, hand["tail"])
        dial_draw.line((tail_endpoint[0], tail_endpoint[1], center[0], center[1]), fill=(0, 0, 0, 255), width=erase_width)
    dial_clean.save(assets_dir / "dial_clean.png")

    cap_size = 24
    left = round(center[0] - cap_size / 2)
    top = round(center[1] - cap_size / 2)
    cap = canvas.crop((left, top, left + cap_size, top + cap_size)).convert("RGBA")
    cap_alpha = Image.new("L", cap.size, 0)
    ImageDraw.Draw(cap_alpha).ellipse((2, 2, cap_size - 3, cap_size - 3), fill=255)
    cap.putalpha(cap_alpha)
    cap.save(assets_dir / "center_cap.png")

    metadata: dict[str, dict[str, Any]] = {}
    for role, hand in hands.items():
        width = int(hand["width"])
        height = round(hand["length"] + hand["tail"])
        image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        pivot_x = width / 2
        pivot_y = hand["length"]
        tip_y = 1
        tail_y = min(height - 1, round(pivot_y + hand["tail"]))
        if role == "SECOND":
            draw.line((pivot_x, tip_y, pivot_x, tail_y), fill=hand["color"], width=round(hand["thickness"]))
        else:
            outer_width = round(hand["thickness"] + 5)
            draw.line((pivot_x, tip_y, pivot_x, tail_y), fill=(0, 0, 0, 255), width=outer_width)
            draw.line((pivot_x, tip_y, pivot_x, tail_y), fill=hand["color"], width=round(hand["thickness"]))
            draw.line((pivot_x, tip_y + 2, pivot_x, tail_y - 2), fill=(0, 0, 0, 255), width=max(2, round(hand["thickness"] / 2)))
        asset_name = f"{role.lower()}_hand.png"
        image.save(assets_dir / asset_name)
        metadata[role] = {
            "asset": f"assets/{asset_name}",
            "bbox": {"x": round(center[0] - width / 2), "y": round(center[1] - hand["length"]), "width": width, "height": height},
            "pivotX": 0.5,
            "pivotY": round(hand["length"] / height, 6),
            "observedAngleDeg": hand["angle"],
            "length": hand["length"],
            "thickness": hand["thickness"],
        }
    return metadata


def analyze_product_photo(reference_path: Path, output_dir: Path) -> dict[str, Any]:
    """Extract a frontal dark display from a product photo and preserve it as static artwork."""
    source = Image.open(reference_path).convert("RGB")
    source_size = list(source.size)
    body = _largest_dark_component(source)
    if body is None:
        raise ValueError("could not locate a dark watch display in the product photo")
    min_x, min_y, max_x, max_y = body
    body_width = max_x - min_x
    body_height = max_y - min_y
    # Remove the chrome around the black display while retaining the full dial vertically.
    crop_box = (
        min_x + round(body_width * 0.05),
        min_y + round(body_height * 0.10),
        max_x - round(body_width * 0.10),
        max_y - round(body_height * 0.10),
    )
    crop = source.crop(crop_box)
    scale = CANVAS_SIZE / crop.height
    resized = crop.resize((round(crop.width * scale), CANVAS_SIZE), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (CANVAS_SIZE, CANVAS_SIZE), "#000000")
    paste_x = max(0, (CANVAS_SIZE - resized.width) // 2)
    if resized.width > CANVAS_SIZE:
        resized = resized.crop(((resized.width - CANVAS_SIZE) // 2, 0, (resized.width + CANVAS_SIZE) // 2, CANVAS_SIZE))
        paste_x = 0
    # Keep the dial but remove the remaining bright bezel fragments at the crop edge.
    display_mask = Image.new("L", resized.size, 0)
    mask_draw = ImageDraw.Draw(display_mask)
    inset = max(8, round(min(resized.size) * 0.025))
    mask_draw.rounded_rectangle((inset, inset, resized.width - inset - 1, resized.height - inset - 1), radius=round(resized.height * 0.10), fill=255)
    canvas.paste(resized, (paste_x, 0), display_mask)

    output_dir.mkdir(parents=True, exist_ok=True)
    assets_dir = output_dir / "assets"
    assets_dir.mkdir(exist_ok=True)
    canvas.save(output_dir / "reference.png")
    canvas.save(assets_dir / "display_reference.png")
    crop.save(assets_dir / "display_crop.png")
    hand_metadata = _write_a1_assets(canvas, assets_dir)
    elements: list[dict[str, Any]] = [
        {
            "id": "dial_clean",
            "type": "STATIC_IMAGE",
            "dynamic": False,
            "bbox": {"x": 0, "y": 0, "width": 438, "height": 438},
            "asset": "assets/dial_clean.png",
            "assetInstruction": {"operation": "extract_from_reference"},
            "confidence": 0.68,
            "zIndex": 0,
            "uncertainty": ["A1a uses a conservative geometric hand mask; automatic segmentation is deferred to A1b"],
        }
    ]
    role_ids = {"HOUR": "hour_hand", "MINUTE": "minute_hand", "SECOND": "second_hand"}
    for role, metadata in hand_metadata.items():
        elements.append(
            {
                "id": role_ids[role],
                "type": "ANALOG_HAND",
                "role": role,
                "dynamic": True,
                "bbox": metadata["bbox"],
                "asset": metadata["asset"],
                "observedAngleDeg": metadata["observedAngleDeg"],
                "length": metadata["length"],
                "thickness": metadata["thickness"],
                "pivotX": metadata["pivotX"],
                "pivotY": metadata["pivotY"],
                "confidence": 0.61 if role != "SECOND" else 0.74,
                "zIndex": {"HOUR": 10, "MINUTE": 20, "SECOND": 30}[role],
                "uncertainty": ["Hand geometry is manually adjustable in A1a", "Observed angle is from the supplied product photograph"],
            }
        )
        if role == "SECOND":
            elements[-1]["sweepFrequency"] = 15
    elements.append(
        {
            "id": "center_cap",
            "type": "STATIC_IMAGE",
            "dynamic": False,
            "bbox": {"x": 207, "y": 207, "width": 24, "height": 24},
            "asset": "assets/center_cap.png",
            "assetInstruction": {"operation": "extract_from_reference"},
            "confidence": 0.72,
            "zIndex": 100,
            "uncertainty": ["Center cap is preserved as a static foreground layer"],
        }
    )
    scene = {
        "schemaVersion": "1.0",
        "canvas": {"width": 438, "height": 438, "shape": "CIRCLE", "centerX": 219, "centerY": 219},
        "normalization": {
            "inputType": "PHOTOGRAPH",
            "rotationDegrees": 0.0,
            "confidence": 0.78,
            "requiresPerspectiveCorrection": False,
            "sourceSize": source_size,
            "displayBounds": {"x": crop_box[0], "y": crop_box[1], "width": crop_box[2] - crop_box[0], "height": crop_box[3] - crop_box[1]},
        },
        "background": {"type": "SOLID", "color": "#000000"},
        "preview": {"time": "10:08:30", "date": "08.20", "weekday": "THU", "battery": 82, "steps": 5240, "heartRate": 68},
        "clock": {"type": "ANALOG", "centerX": 219, "centerY": 219, "confidence": 0.97, "method": "A1a fixed pivot from normalized display"},
        "elements": elements,
        "analysis": {
            "watchFaceCategory": "MINIMAL_ANALOG",
            "overallConfidence": 0.68,
            "requiresStaticAssetExtraction": True,
            "requiresHumanReview": True,
            "method": "A1a dark-display crop + explicit clock pivot + adjustable hand assets",
            "componentCount": 1,
            "groupCount": 4,
        },
    }
    return scene
