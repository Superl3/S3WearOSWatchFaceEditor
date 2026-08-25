from __future__ import annotations

from collections import deque
import math
from pathlib import Path
from typing import Any

from PIL import Image
from PIL import ImageDraw

from .date_window import extract_date_day_of_month_window
from .manual_glyphs import import_manual_glyphs
from .model import CANVAS_SIZE
from .occlusion import reconstruct_occluded_dial


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


def _is_light(pixel: tuple[int, int, int]) -> bool:
    high = max(pixel)
    low = min(pixel)
    return high >= 135 and high - low <= 110


def _is_red(pixel: tuple[int, int, int]) -> bool:
    red, green, blue = pixel
    return red >= 85 and red >= green * 1.35 and red >= blue * 1.15


def _detect_clock_center(image: Image.Image) -> tuple[float, float, float]:
    pixels = image.convert("RGB").load()
    red_points = [(x, y) for y in range(170, 270) for x in range(185, 255) if _is_red(pixels[x, y])]
    rows: list[tuple[int, int, int]] = []
    for y in sorted({point[1] for point in red_points}):
        xs = [x for x, row in red_points if row == y]
        if xs and max(xs) - min(xs) >= 5:
            rows.append((y, min(xs), max(xs)))
    if rows:
        center_x = sum((left + right) / 2 for _, left, right in rows) / len(rows)
        center_y = sum(row for row, _, _ in rows) / len(rows)
        confidence = min(0.99, 0.82 + len(rows) / 100)
        return round(center_x, 2), round(center_y, 2), round(confidence, 2)
    return 219.0, 219.0, 0.55


def _ray_point(center: tuple[float, float], angle: float, radius: float, perpendicular: float = 0.0) -> tuple[int, int]:
    radians = math.radians(angle)
    ux, uy = math.sin(radians), -math.cos(radians)
    vx, vy = math.cos(radians), math.sin(radians)
    return round(center[0] + ux * radius + vx * perpendicular), round(center[1] + uy * radius + vy * perpendicular)


def _ray_present(image: Image.Image, center: tuple[float, float], angle: float, radius: float, predicate: Any, spread: int = 4) -> bool:
    pixels = image.convert("RGB").load()
    for perpendicular in range(-spread, spread + 1):
        x, y = _ray_point(center, angle, radius, perpendicular)
        if 0 <= x < image.width and 0 <= y < image.height and predicate(pixels[x, y]):
            return True
    return False


def _ray_extent(image: Image.Image, center: tuple[float, float], angle: float, predicate: Any) -> float:
    last_present = 18
    started = False
    gap = 0
    for radius in range(18, 211):
        present = _ray_present(image, center, angle, radius, predicate, spread=8)
        if present:
            started = True
            last_present = radius
            gap = 0
        elif started:
            gap += 1
            if gap >= 3:
                break
    return float(last_present)


def _cluster_angle(scores: list[float], peak: int, radius: int = 20) -> float:
    local = [scores[(peak + delta) % 360] for delta in range(-radius, radius + 1)]
    threshold = max(local) * 0.8
    candidates = [peak + delta for delta in range(-radius, radius + 1) if scores[(peak + delta) % 360] >= threshold]
    if not candidates:
        return float(peak)
    return round(sum(candidates) / len(candidates), 2) % 360


def _detect_hand_geometry(image: Image.Image, center: tuple[float, float]) -> dict[str, dict[str, float]]:
    light_scores: list[float] = []
    for angle in range(360):
        score = 0.0
        for radius in range(20, 91):
            if _ray_present(image, center, angle, radius, _is_light):
                score += 1
        light_scores.append(score)
    peaks: list[int] = []
    for peak, _ in sorted(enumerate(light_scores), key=lambda item: item[1], reverse=True):
        if light_scores[peak] < 20:
            break
        if all(min((peak - other) % 360, (other - peak) % 360) > 28 for other in peaks):
            peaks.append(peak)
        if len(peaks) == 2:
            break
    if len(peaks) < 2:
        peaks = [306, 54]
    detected = []
    for peak in peaks:
        angle = _cluster_angle(light_scores, peak)
        length = _ray_extent(image, center, angle, _is_light)
        detected.append((angle, length))
    detected.sort(key=lambda item: item[1])
    red_points = []
    pixels = image.convert("RGB").load()
    for y in range(image.height):
        for x in range(image.width):
            if _is_red(pixels[x, y]) and abs(x - center[0]) <= 10:
                dx, dy = x - center[0], y - center[1]
                distance = math.hypot(dx, dy)
                if 18 < distance < 220:
                    red_points.append((distance, math.degrees(math.atan2(dx, -dy)) % 360))
    second_angle, second_length = (max(red_points) if red_points else (178.0, 180.0))[1], (max(red_points) if red_points else (178.0, 180.0))[0]
    return {
        "HOUR": {"angle": detected[0][0], "length": detected[0][1], "thickness": 8.0, "tail": 12.0},
        "MINUTE": {"angle": detected[1][0], "length": detected[1][1], "thickness": 8.0, "tail": 10.0},
        "SECOND": {"angle": round(second_angle, 2), "length": round(second_length, 2), "thickness": 3.0, "tail": 9.0},
    }


def _extract_hand_asset(image: Image.Image, center: tuple[float, float], geometry: dict[str, float], role: str, path: Path) -> None:
    length = geometry["length"]
    tail = geometry["tail"]
    angle = geometry["angle"]
    thickness = geometry["thickness"]
    width = max(10, round(thickness * 3.5))
    height = round(length + tail)
    asset = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    source = image.convert("RGB")
    source_pixels = source.load()
    target_pixels = asset.load()
    sampled_colors: list[tuple[int, int, int]] = []
    radians = math.radians(angle)
    ux, uy = math.sin(radians), -math.cos(radians)
    vx, vy = math.cos(radians), math.sin(radians)
    predicate = _is_red if role == "SECOND" else _is_light
    radius = int(length + tail + 12)
    for y in range(max(0, round(center[1] - radius)), min(image.height, round(center[1] + radius + 1))):
        for x in range(max(0, round(center[0] - radius)), min(image.width, round(center[0] + radius + 1))):
            dx, dy = x - center[0], y - center[1]
            along = dx * ux + dy * uy
            perpendicular = dx * vx + dy * vy
            if -tail <= along <= length + 2 and abs(perpendicular) <= width / 2 and predicate(source_pixels[x, y]):
                ax = round(width / 2 + perpendicular)
                ay = round(length - along)
                if 0 <= ax < width and 0 <= ay < height:
                    target_pixels[ax, ay] = (*source_pixels[x, y], 255)
                    sampled_colors.append(source_pixels[x, y])
    original_alpha = asset.getchannel("A")
    rows = {y: [x for x in range(width) if original_alpha.getpixel((x, y)) > 0] for y in range(height)}
    populated_rows = [y for y, xs in rows.items() if xs]
    if populated_rows:
        average = tuple(round(sum(pixel[channel] for pixel in sampled_colors) / len(sampled_colors)) for channel in range(3))
        smooth = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        smooth_draw = ImageDraw.Draw(smooth)
        edge_color = (*average, 255)
        if role == "SECOND":
            smooth_draw.line((width / 2, 0, width / 2, height - 1), fill=edge_color, width=max(2, round(thickness)))
        else:
            outer_width = round(thickness + 5)
            smooth_draw.line((width / 2, 0, width / 2, height - 1), fill=(0, 0, 0, 255), width=outer_width)
            smooth_draw.line((width / 2, 0, width / 2, height - 1), fill=edge_color, width=round(thickness))
            smooth_draw.line((width / 2, 2, width / 2, height - 3), fill=(0, 0, 0, 255), width=max(2, round(thickness / 2)))
        asset = smooth
    if asset.getbbox() is None:
        draw = ImageDraw.Draw(asset)
        draw.line((width / 2, 0, width / 2, height - 1), fill=(175, 18, 45, 255) if role == "SECOND" else (245, 245, 245, 255), width=max(2, round(thickness)))
    path.parent.mkdir(parents=True, exist_ok=True)
    asset.save(path)


def _remove_hand_from_dial(image: Image.Image, center: tuple[float, float], geometry: dict[str, float], role: str) -> None:
    draw = ImageDraw.Draw(image)
    width = round(geometry["thickness"] + (8 if role != "SECOND" else 5))
    start = -round(geometry["tail"]) - 2
    end = round(geometry["length"]) + 5
    endpoint = _clock_endpoint(center, geometry["angle"], end)
    draw.line((center[0], center[1], endpoint[0], endpoint[1]), fill=(0, 0, 0, 255), width=width)
    tail_endpoint = _clock_endpoint(center, (geometry["angle"] + 180) % 360, -start)
    draw.line((tail_endpoint[0], tail_endpoint[1], center[0], center[1]), fill=(0, 0, 0, 255), width=width)


def _write_a1_assets(canvas: Image.Image, assets_dir: Path) -> tuple[dict[str, dict[str, Any]], tuple[float, float, float]]:
    """A1b reference-to-assets extraction; A1a compiler/renderer consume its output."""
    center_x, center_y, center_confidence = _detect_clock_center(canvas)
    center = (center_x, center_y)
    hands = _detect_hand_geometry(canvas, center)
    assets_dir.mkdir(parents=True, exist_ok=True)
    dial_clean = canvas.convert("RGBA")
    for role in ("HOUR", "MINUTE", "SECOND"):
        _remove_hand_from_dial(dial_clean, center, hands[role], role)
    dial_clean.save(assets_dir / "dial_clean.png")

    cap_size = 24
    left = round(center[0] - cap_size / 2)
    top = round(center[1] - cap_size / 2)
    cap = canvas.crop((left, top, left + cap_size, top + cap_size)).convert("RGBA")
    cap_alpha = Image.new("L", cap.size, 0)
    ImageDraw.Draw(cap_alpha).ellipse((3, 3, cap_size - 4, cap_size - 4), fill=255)
    cap.putalpha(cap_alpha)
    cap.save(assets_dir / "center_cap.png")

    metadata: dict[str, dict[str, Any]] = {}
    for role, hand in hands.items():
        asset_name = f"{role.lower()}_hand.png"
        _extract_hand_asset(canvas, center, hand, role, assets_dir / asset_name)
        width = max(10, round(hand["thickness"] * 3.5))
        height = round(hand["length"] + hand["tail"])
        metadata[role] = {
            "asset": f"assets/{asset_name}",
            "bbox": {"x": round(center[0] - width / 2), "y": round(center[1] - hand["length"]), "width": width, "height": height},
            "pivotX": 0.5,
            "pivotY": round(hand["length"] / height, 6),
            "observedAngleDeg": hand["angle"],
            "length": hand["length"],
            "thickness": hand["thickness"],
            "extractionMethod": "radial-light-ray + red-axis mask",
        }
    return metadata, (center_x, center_y, center_confidence)


def analyze_product_photo(
    reference_path: Path,
    output_dir: Path,
    generative_fallback_path: Path | None = None,
    manual_glyph_dir: Path | None = None,
    display_roi: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Extract a frontal dark display from a product photo and preserve it as static artwork."""
    source = Image.open(reference_path).convert("RGB")
    source_size = list(source.size)
    confirmed_roi: dict[str, int] | None = None
    if display_roi is not None:
        from .display_roi import _validate_roi

        confirmed_roi = _validate_roi(display_roi, source.size)
    if confirmed_roi is not None:
        crop_box = (
            confirmed_roi["x"],
            confirmed_roi["y"],
            confirmed_roi["x"] + confirmed_roi["width"],
            confirmed_roi["y"] + confirmed_roi["height"],
        )
        crop = source.crop(crop_box)
        scale = min(CANVAS_SIZE / crop.width, CANVAS_SIZE / crop.height)
        resized = crop.resize((round(crop.width * scale), round(crop.height * scale)), Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", (CANVAS_SIZE, CANVAS_SIZE), "#000000")
        paste_x = (CANVAS_SIZE - resized.width) // 2
        paste_y = (CANVAS_SIZE - resized.height) // 2
        display_mask = Image.new("L", resized.size, 0)
        mask_draw = ImageDraw.Draw(display_mask)
        display_radius = confirmed_roi["radius"] * scale
        mask_draw.rounded_rectangle((0, 0, resized.width - 1, resized.height - 1), radius=round(display_radius), fill=255)
        canvas.paste(resized, (paste_x, paste_y), display_mask)
        source_display_geometry = {
            "shape": "ROUNDED_RECT",
            "width": float(resized.width),
            "height": float(resized.height),
            "radius": float(display_radius),
            "centerX": float(paste_x + resized.width / 2),
            "centerY": float(paste_y + resized.height / 2),
            "isCircleSpecialCase": False,
        }
    else:
        # Legacy automatic proposal remains available to non-perimeter callers.
        # The perimeter benchmark itself requires a confirmed ROI before it enters
        # this branch.
        body = _largest_dark_component(source, threshold=75)
        strict_display = _largest_dark_component(source, threshold=10)
        if body is None or strict_display is None:
            raise ValueError("could not locate a dark watch display in the product photo")
        min_x, _, max_x, _ = body
        _, min_y, _, max_y = strict_display
        body_width = max_x - min_x
        crop_box = (
            min_x + round(body_width * 0.05),
            min_y,
            max_x - round(body_width * 0.10),
            max_y,
        )
        crop = source.crop(crop_box)
        scale = CANVAS_SIZE / crop.height
        resized = crop.resize((round(crop.width * scale), CANVAS_SIZE), Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", (CANVAS_SIZE, CANVAS_SIZE), "#000000")
        paste_x = max(0, (CANVAS_SIZE - resized.width) // 2)
        if resized.width > CANVAS_SIZE:
            resized = resized.crop(((resized.width - CANVAS_SIZE) // 2, 0, (resized.width + CANVAS_SIZE) // 2, CANVAS_SIZE))
            paste_x = 0
        display_mask = Image.new("L", resized.size, 0)
        mask_draw = ImageDraw.Draw(display_mask)
        inset = max(8, round(min(resized.size) * 0.025))
        display_radius = min(round(resized.height * 0.10), (min(resized.width, resized.height) - 2 * inset) / 2)
        mask_draw.rounded_rectangle((inset, inset, resized.width - inset - 1, resized.height - inset - 1), radius=round(display_radius), fill=255)
        canvas.paste(resized, (paste_x, 0), display_mask)
        source_display_geometry = {
            "shape": "ROUNDED_RECT",
            "width": float(resized.width - 2 * inset),
            "height": float(resized.height - 2 * inset),
            "radius": float(display_radius),
            "centerX": float(paste_x + resized.width / 2),
            "centerY": float(CANVAS_SIZE / 2),
            "isCircleSpecialCase": False,
        }
    target_display_geometry = {
        "shape": "CIRCLE",
        "width": float(CANVAS_SIZE),
        "height": float(CANVAS_SIZE),
        "radius": float(CANVAS_SIZE / 2),
        "centerX": float(CANVAS_SIZE / 2),
        "centerY": float(CANVAS_SIZE / 2),
        "isCircleSpecialCase": True,
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    assets_dir = output_dir / "assets"
    assets_dir.mkdir(exist_ok=True)
    canvas.save(output_dir / "reference.png")
    canvas.save(assets_dir / "display_reference.png")
    crop.save(assets_dir / "display_crop.png")
    hand_metadata, detected_clock = _write_a1_assets(canvas, assets_dir)
    detected_center_x, detected_center_y, detected_center_confidence = detected_clock
    reconstruct_occluded_dial(
        reference_path=output_dir / "reference.png",
        assets_dir=assets_dir,
        output_dir=output_dir,
        center=(detected_center_x, detected_center_y),
        hands=hand_metadata,
        generative_fallback_path=generative_fallback_path,
        margin=4,
    )
    date_window_metadata = extract_date_day_of_month_window(
        reference_path=output_dir / "reference.png",
        dial_path=assets_dir / "dial_clean.png",
        output_dir=output_dir,
    )
    manual_glyphs = import_manual_glyphs(manual_glyph_dir, output_dir)
    dial_asset = date_window_metadata["emptyDialAsset"] if date_window_metadata else "assets/dial_clean.png"
    elements: list[dict[str, Any]] = [
        {
            "id": "dial_clean",
            "type": "STATIC_IMAGE",
            "dynamic": False,
            "bbox": {"x": 0, "y": 0, "width": 438, "height": 438},
            "asset": dial_asset,
            "assetInstruction": {"operation": "extract_from_reference"},
            "confidence": 0.78,
            "zIndex": 0,
            "uncertainty": ["A1b uses a conservative radial-light mask; segmentation remains adjustable for difficult references"],
        }
    ]
    if date_window_metadata:
        inner = date_window_metadata["innerBbox"]
        date_element = {
            "id": "date_day_of_month",
            "type": "DYNAMIC_SLOT",
            "slotType": "DATE_DAY_OF_MONTH",
            "dynamic": True,
            "bbox": inner,
            "format": "d",
            "style": {
                "fontFamily": "Pretendard",
                "fontWeight": 400,
                "fontSize": 24,
                "alignment": "center",
                "color": date_window_metadata["glyphColor"],
            },
            "confidence": date_window_metadata["confidence"],
            "zIndex": 5,
            "relationships": {
                "layoutReplacement": True,
                "replacesSourceElement": "hour_index_3",
                "staticFramePreserved": True,
                "frameBbox": date_window_metadata["frameBbox"],
                "innerBbox": date_window_metadata["innerBbox"],
                "padding": date_window_metadata["padding"],
            },
            "uncertainty": ["Source absence of hour numeral 3 is intentional layout replacement; numeral 3 is not reconstructed"],
        }
        if manual_glyphs:
            date_element["manualGlyphs"] = manual_glyphs
            date_element["uncertainty"].append("Only user-supplied manual glyph PNGs override Pretendard; missing digits use Pretendard fallback")
        elements.append(date_element)
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
                "confidence": 0.79 if role != "SECOND" else 0.84,
                "zIndex": {"HOUR": 10, "MINUTE": 20, "SECOND": 30}[role],
                "uncertainty": ["Radial-ray extraction is conservative and remains adjustable", "Observed angle is from the supplied product photograph"],
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
            "confidence": 0.78,
            "zIndex": 100,
            "uncertainty": ["Center cap is preserved as a static foreground layer"],
        }
    )
    scene = {
        "schemaVersion": "1.0",
        "canvas": {"width": 438, "height": 438, "shape": "CIRCLE", "centerX": 219, "centerY": 219},
        "displayGeometry": {
            "source": source_display_geometry,
            "target": target_display_geometry,
            "mappingPolicy": "HYBRID_PERIMETER_MAPPING" if confirmed_roi is not None else "CENTER_PRESERVING_BOUNDARY_NORMALIZED",
            "availableMappings": ["NAIVE_XY_STRETCH", "CENTER_PRESERVING_BOUNDARY_NORMALIZED", "PERIMETER_SD_WARP", "LOCAL_SIMILARITY", "HYBRID_PERIMETER_MAPPING", "INVERSE_RASTER_MAPPING"],
        },
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
        "clock": {"type": "ANALOG", "centerX": detected_center_x, "centerY": detected_center_y, "confidence": detected_center_confidence, "method": "A1b red pivot blob detection"},
        "elements": elements,
        "analysis": {
            "watchFaceCategory": "MINIMAL_ANALOG",
            "overallConfidence": 0.68,
            "requiresStaticAssetExtraction": True,
            "requiresHumanReview": True,
            "method": "USER_CONFIRMED_DISPLAY_ROI + A1d occlusion completion + A2 dynamic date-window slot" if confirmed_roi is not None else "A1b dark-display crop + A1d occlusion completion + A2 dynamic date-window slot",
            "componentCount": 1,
            "groupCount": 5 if date_window_metadata else 4,
            "manualGlyphOverride": manual_glyphs,
        },
    }
    return scene
