from __future__ import annotations

import json
import math
from collections import deque
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from .display_geometry import RoundedRect, angle_from_direction, direction_from_angle, map_element_preserving


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


def _perimeter_lookup(shape: RoundedRect, sample_count: int = 1440) -> tuple[list[float], list[float], float]:
    angles = [index * 360.0 / sample_count for index in range(sample_count + 1)]
    distances = [shape.boundary_distance(direction_from_angle(angle)) for angle in angles]
    lengths = [0.0]
    for first, second, angle in zip(distances, distances[1:], angles[1:]):
        previous = direction_from_angle(angle - 360.0 / sample_count)
        current = direction_from_angle(angle)
        lengths.append(lengths[-1] + math.hypot(second * current[0] - first * previous[0], second * current[1] - first * previous[1]))
    return angles, lengths, lengths[-1]


def _perimeter_position(angle: float, lookup: tuple[list[float], list[float], float]) -> float:
    angles, lengths, total = lookup
    normalized = angle % 360.0
    step = 360.0 / (len(angles) - 1)
    index = min(len(angles) - 2, max(0, math.floor(normalized / step)))
    fraction = normalized / step - index
    distance = lengths[index] + (lengths[index + 1] - lengths[index]) * fraction
    return distance / total if total else 0.0


def _point_sd(point: tuple[float, float], shape: RoundedRect, lookup: tuple[list[float], list[float], float]) -> tuple[float, float]:
    vector = (point[0] - shape.center_x, point[1] - shape.center_y)
    radius = math.hypot(*vector)
    if radius == 0:
        return 0.0, 0.0
    direction = (vector[0] / radius, vector[1] / radius)
    boundary = shape.boundary_distance(direction)
    return _perimeter_position(angle_from_direction(direction), lookup), max(0.0, boundary - radius)


def _circular_distance(first: float, second: float) -> float:
    distance = abs(first - second) % 1.0
    return min(distance, 1.0 - distance)


def _circular_mean(values: list[float]) -> float:
    if not values:
        return 0.0
    x = sum(math.cos(value * math.tau) for value in values)
    y = sum(math.sin(value * math.tau) for value in values)
    return math.atan2(y, x) / math.tau % 1.0


def _circular_span(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    ordered = sorted(value % 1.0 for value in values)
    largest_gap = max((ordered[index + 1] - ordered[index] for index in range(len(ordered) - 1)), default=0.0)
    largest_gap = max(largest_gap, ordered[0] + 1.0 - ordered[-1])
    return 1.0 - largest_gap


def _angle_difference(first: float, second: float) -> float:
    return abs((first - second + 90.0) % 180.0 - 90.0)


def _group_components(records: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    parent = list(range(len(records)))
    if not records:
        return []

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(first: int, second: int) -> None:
        first_root, second_root = find(first), find(second)
        if first_root != second_root:
            parent[second_root] = first_root

    spans = [max(record["sSpan"], 0.004) for record in records]
    gap_limit = min(0.022, max(0.008, 1.0 * sorted(spans)[len(spans) // 2]))
    d_limit = max(8.0, min(22.0, 0.18 * max(record["bbox"][3] - record["bbox"][1] for record in records)))
    for first in range(len(records)):
        for second in range(first + 1, len(records)):
            left, right = records[first], records[second]
            if abs(left["dMean"] - right["dMean"]) > d_limit:
                continue
            if _angle_difference(left["rotation"], right["rotation"]) > 55.0:
                continue
            separation = _circular_distance(left["sMean"], right["sMean"])
            if separation <= gap_limit:
                union(first, second)
    groups: dict[int, list[dict[str, Any]]] = {}
    for index, record in enumerate(records):
        groups.setdefault(find(index), []).append(record)
    return sorted(groups.values(), key=lambda group: _circular_mean([record["sMean"] for record in group]))


def _split_component_record(record: dict[str, Any], shape: RoundedRect, lookup: tuple[list[float], list[float], float]) -> list[dict[str, Any]]:
    """Split a touching raster component when its perimeter density has clear gaps."""

    points = record["points"]
    sd_points = [(_point_sd((float(x), float(y)), shape, lookup), (x, y)) for x, y in points]
    ordered = sorted(sd_points, key=lambda item: item[0][0])
    if not ordered or (record["sSpan"] <= 0.045 and record["dMax"] - record["dMin"] <= 45.0):
        return [record]
    gaps = [ordered[index + 1][0][0] - ordered[index][0][0] for index in range(len(ordered) - 1)]
    gaps.append(ordered[0][0][0] + 1.0 - ordered[-1][0][0])
    split_gap = 0.012
    cut_indices = [index for index, gap in enumerate(gaps) if gap > split_gap]
    if not cut_indices:
        cut_indices = []

    if record["sSpan"] > 0.045 or record["dMax"] - record["dMin"] > 45.0:
        bins: dict[tuple[int, int], list[tuple[tuple[float, float], tuple[int, int]]]] = {}
        for item in sd_points:
            sd, point = item
            bins.setdefault((math.floor(sd[0] / 0.008), math.floor(sd[1] / 6.0)), []).append(item)
        occupied = set(bins)
        bin_groups: list[set[tuple[int, int]]] = []
        while occupied:
            seed = occupied.pop()
            queue = [seed]
            group = {seed}
            while queue:
                current = queue.pop()
                for neighbor in ((current[0] + dx, current[1] + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1) if dx or dy):
                    if neighbor in occupied:
                        occupied.remove(neighbor)
                        group.add(neighbor)
                        queue.append(neighbor)
            bin_groups.append(group)
        dense_clusters = [[item for key in group for item in bins[key]] for group in bin_groups]
        dense_clusters = [cluster for cluster in dense_clusters if len(cluster) >= max(6, len(points) // 100)]
        if len(dense_clusters) > 1:
            clusters = dense_clusters
        else:
            clusters = []
    else:
        clusters = []
    if not clusters and not cut_indices:
        return [record]
    if not clusters:
        start = (cut_indices[0] + 1) % len(ordered)
        rotated = ordered[start:] + ordered[:start]
        clusters = [[]]
        for index, item in enumerate(rotated):
            if index and rotated[index][0][0] - rotated[index - 1][0][0] > split_gap:
                clusters.append([])
            clusters[-1].append(item)
    result: list[dict[str, Any]] = []
    for cluster_index, cluster in enumerate(clusters):
        cluster_points = [item[1] for item in cluster]
        if len(cluster_points) < max(6, len(points) // 80):
            return [record]
        values = [item[0] for item in cluster]
        center = _centroid(cluster_points)
        result.append(
            {
                **record,
                "componentIndex": f"{record['componentIndex']}.{cluster_index}",
                "points": cluster_points,
                "bbox": _component_bbox(cluster_points, 1, (round(shape.width + shape.center_x * 2), round(shape.height + shape.center_y * 2))),
                "sMean": _circular_mean([value[0] for value in values]),
                "sSpan": _circular_span([value[0] for value in values]),
                "dMean": sum(value[1] for value in values) / len(values),
                "dMin": min(value[1] for value in values),
                "dMax": max(value[1] for value in values),
                "rotation": _principal_rotation(cluster_points, center),
            }
        )
    return result


def _extract_group_asset(image: Image.Image, points: list[tuple[int, int]], bbox: tuple[int, int, int, int], background: tuple[int, int, int]) -> Image.Image:
    crop = image.convert("RGB").crop(bbox)
    group_mask = Image.new("L", crop.size, 0)
    group_pixels = group_mask.load()
    for x, y in points:
        local_x, local_y = x - bbox[0], y - bbox[1]
        if 0 <= local_x < crop.width and 0 <= local_y < crop.height:
            group_pixels[local_x, local_y] = 255
    group_mask = group_mask.filter(ImageFilter.MaxFilter(3))
    alpha = Image.new("L", crop.size, 0)
    alpha_pixels = alpha.load()
    crop_pixels = crop.load()
    for y in range(crop.height):
        for x in range(crop.width):
            if not group_mask.getpixel((x, y)):
                continue
            distance = _color_distance(crop_pixels[x, y], background)
            alpha_pixels[x, y] = max(0, min(255, round((distance - 4) * 6)))
    result = crop.convert("RGBA")
    result.putalpha(alpha)
    return result


def decompose_perimeter_artwork(
    image: Image.Image,
    source_shape: RoundedRect,
    output_root: Path,
    *,
    exclusion_mask: Image.Image | None = None,
    minimum_area: int = 12,
    minimum_normalized_radius: float = 0.62,
) -> dict[str, Any]:
    """Detect perimeter artwork, then merge components in normalized perimeter space."""

    output_root.mkdir(parents=True, exist_ok=True)
    asset_root = output_root / "assets" / "perimeter"
    asset_root.mkdir(parents=True, exist_ok=True)
    background = _background_color(image)
    foreground = _foreground_mask(image, background)
    if exclusion_mask is not None:
        foreground = ImageChops.subtract(foreground, exclusion_mask.convert("L"))
    lookup = _perimeter_lookup(source_shape)
    records: list[dict[str, Any]] = []
    for component_index, points in enumerate(_components(foreground, minimum_area)):
        center = _centroid(points)
        sd_points = [_point_sd((float(x), float(y)), source_shape, lookup) for x, y in points]
        radii = [_normalized_radius((float(x), float(y)), source_shape) for x, y in points]
        radial_position = sum(radii) / len(radii)
        if radial_position < minimum_normalized_radius or max(radii) < minimum_normalized_radius + 0.08:
            continue
        bbox = _component_bbox(points, 1, image.size)
        record = {
                "componentIndex": component_index,
                "points": points,
                "bbox": bbox,
                "sMean": _circular_mean([value[0] for value in sd_points]),
                "sSpan": _circular_span([value[0] for value in sd_points]),
                "dMean": sum(value[1] for value in sd_points) / len(sd_points),
                "dMin": min(value[1] for value in sd_points),
                "dMax": max(value[1] for value in sd_points),
                "radialPosition": radial_position,
                "rotation": _principal_rotation(points, center),
            }
        records.extend(_split_component_record(record, source_shape, lookup))

    groups = _group_components(records)
    elements: list[dict[str, Any]] = []
    accepted_mask = Image.new("L", image.size, 0)
    accepted_pixels = accepted_mask.load()
    unwrap = Image.new("RGB", (720, 260), "#101010")
    unwrap_draw = ImageDraw.Draw(unwrap)
    unwrap_draw.text((8, 8), "normalized perimeter (s,d)", fill=(255, 255, 255))
    all_d = [record["dMax"] for record in records] or [1.0]
    d_scale = 205.0 / max(1.0, max(all_d))
    for slot_index, group in enumerate(groups):
        points = [point for record in group for point in record["points"]]
        center = _centroid(points)
        bbox = _component_bbox(points, 2, image.size)
        width, height = bbox[2] - bbox[0], bbox[3] - bbox[1]
        asset_name = f"perimeter_slot_{slot_index:02d}.png"
        _extract_group_asset(image, points, bbox, background).save(asset_root / asset_name)
        direction = (center[0] - source_shape.center_x, center[1] - source_shape.center_y)
        s_values = [record["sMean"] for record in group]
        d_min, d_max = min(record["dMin"] for record in group), max(record["dMax"] for record in group)
        s_mean = _circular_mean(s_values)
        radial_position = sum(record["radialPosition"] for record in group) / len(group)
        for x, y in points:
            accepted_pixels[x, y] = 255
        confidence = min(0.99, 0.62 + max(0.0, radial_position - minimum_normalized_radius) * 0.8)
        elements.append(
            {
                "id": f"perimeter_slot_{slot_index:02d}",
                "type": "STATIC_ARTWORK",
                "dynamic": False,
                "bbox": {"x": bbox[0], "y": bbox[1], "width": width, "height": height},
                "anchor": {"x": round(center[0], 4), "y": round(center[1], 4)},
                "sourceAngleDeg": round(angle_from_direction(direction), 4),
                "perimeterPosition": round(s_mean, 6),
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
                    "detector": "normalized rounded-rectangle perimeter slot grouping",
                    "componentCount": len(group),
                    "componentIndices": [record["componentIndex"] for record in group],
                    "normalizedRadius": round(radial_position, 6),
                    "normalizedPerimeterRange": {"s": round(_circular_span(s_values), 6), "dMin": round(d_min, 4), "dMax": round(d_max, 4)},
                    "semanticIdentificationRequired": False,
                },
            }
        )
        unwrap_box = (round(s_mean * 708), round(24 + d_min * d_scale), round(min(719, s_mean * 708 + max(10, _circular_span(s_values) * 708))), round(24 + max(25, d_max * d_scale)))
        unwrap_draw.rectangle(unwrap_box, outline=(70, 220, 255), width=2)
        unwrap_draw.text((unwrap_box[0], max(25, unwrap_box[1] - 14)), str(slot_index), fill=(255, 230, 80))
        for record in group:
            unwrap_draw.ellipse((round(record["sMean"] * 708) - 2, round(24 + record["dMean"] * d_scale) - 2, round(record["sMean"] * 708) + 2, round(24 + record["dMean"] * d_scale) + 2), fill=(255, 90, 90))

    foreground.save(output_root / "perimeter-foreground-mask.png")
    accepted_mask.save(output_root / "perimeter-artwork-mask.png")
    unwrap.save(output_root / "perimeter-sd-unwrap.png")
    report = {
        "sourceShape": source_shape.as_dict(),
        "backgroundColor": "#%02X%02X%02X" % background,
        "minimumNormalizedRadius": minimum_normalized_radius,
        "grouping": {"coordinateSystem": "normalized_perimeter_s_d", "slotCount": len(elements), "componentCount": len(records), "sGapLimit": min(0.022, max(0.008, sorted([max(record["sSpan"], 0.004) for record in records])[len(records) // 2])) if records else 0.0},
        "elementCount": len(elements),
        "elements": elements,
        "mask": str(output_root / "perimeter-artwork-mask.png"),
        "unwrap": str(output_root / "perimeter-sd-unwrap.png"),
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
