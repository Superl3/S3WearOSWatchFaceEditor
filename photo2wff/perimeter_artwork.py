from __future__ import annotations

import json
import math
from collections import deque
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw

from .display_geometry import RoundedRect, _premultiplied_resize, angle_from_direction, direction_from_angle, map_element_preserving, map_local_similarity
from .perimeter_boundary_review import detect_ambiguous_adjacent_pairs, generate_manual_boundary_review, load_manual_boundary_ownership


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
    # Do not dilate this mask.  The padded crop is intentionally larger than
    # the artwork, but alpha must contain only pixels assigned to this slot;
    # dilation can pull a neighboring marker back into an otherwise correct
    # boundary split.
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


def _occupancy_histogram(sd_points: list[tuple[float, float]], bins: int = 720) -> list[int]:
    occupancy = [0] * bins
    for s, _ in sd_points:
        occupancy[min(bins - 1, max(0, int((s % 1.0) * bins)))] += 1
    return occupancy


def _hour_position_prior(
    sd_points: list[tuple[float, float]],
    shape: RoundedRect,
    lookup: tuple[list[float], list[float], float],
) -> list[dict[str, float]] | None:
    """Detect a generic 12-position analog face from perimeter occupancy.

    The prior is derived from the supplied display geometry, never from a reference
    image coordinate. It is used only to split marker slots before components are
    assembled, which is what lets a touching 0+9 pair become two slots.
    """

    prior = [
        {"index": float(index), "angle": index * 30.0, "s": _perimeter_position(index * 30.0, lookup)}
        for index in range(12)
    ]
    counts = [0] * len(prior)
    for s, _ in sd_points:
        nearest = min(prior, key=lambda candidate: _circular_distance(s, candidate["s"]))
        counts[int(nearest["index"])] += 1
    threshold = max(12, round(len(sd_points) * 0.0025))
    occupied = sum(count >= threshold for count in counts)
    if occupied < 8:
        return None
    for candidate, count in zip(prior, counts):
        candidate["occupancy"] = float(count)
    return prior


def _hour_slot_boundaries(sd_points: list[tuple[float, float]], prior: list[dict[str, float]], bins: int = 720) -> list[float]:
    """Find marker ends at valleys, penalizing a cut through a continuous stroke.

    A nearest-prior split is too permissive for glyphs that extend toward an
    adjacent hour position.  The boundary must be both locally empty and a
    discontinuity in the foreground run.  The latter term is deliberately strong:
    it prefers ending a slot before a continuous tail can leak into the next slot.
    """

    histogram = _occupancy_histogram(sd_points, bins)
    boundaries: list[float] = []
    for index, current in enumerate(prior):
        start = current["s"]
        end = prior[(index + 1) % len(prior)]["s"]
        gap = (end - start) % 1.0
        search_start = start + gap * 0.18
        search_end = start + gap * 0.82
        candidates: list[tuple[float, float]] = []
        steps = max(8, round(gap * bins))
        for step in range(steps + 1):
            s = (search_start + (search_end - search_start) * step / steps) % 1.0
            center_bin = int(s * bins) % bins
            # Penalize occupancy over a neighborhood, not a single empty pixel.
            center_cost = sum(
                histogram[(center_bin + offset) % bins] * (7.0 if abs(offset) <= 1 else 1.5)
                for offset in range(-4, 5)
            )
            left_support = sum(histogram[(center_bin - offset) % bins] for offset in range(2, 9))
            right_support = sum(histogram[(center_bin + offset) % bins] for offset in range(2, 9))
            # If both sides remain continuously occupied, the candidate is
            # likely cutting through a stroke instead of ending at a gap.
            continuity_penalty = 9.0 * min(left_support, right_support)
            candidates.append((center_cost + continuity_penalty, s))
        boundaries.append(min(candidates, key=lambda item: (item[0], _circular_distance(item[1], start + gap * 0.5)))[1])
    return boundaries


def _circular_interval_contains(value: float, start: float, end: float) -> bool:
    width = (end - start) % 1.0
    return (value - start) % 1.0 < width


def _connected_sd_groups(
    sd_points: list[tuple[float, float, tuple[int, int]]],
) -> list[list[tuple[float, float, tuple[int, int]]]]:
    """Return raster-connected groups without treating them as final artwork.

    Connected components are used only as a continuity guard.  Small coherent
    pieces are kept intact when a perimeter boundary passes through their s-span;
    unusually broad connected groups are still split by the slot boundaries so
    touching adjacent markers remain separable.
    """

    by_pixel = {point: (s, d, point) for s, d, point in sd_points}
    remaining = set(by_pixel)
    groups: list[list[tuple[float, float, tuple[int, int]]]] = []
    while remaining:
        seed = remaining.pop()
        queue = [seed]
        group = [by_pixel[seed]]
        while queue:
            x, y = queue.pop()
            for nx in range(x - 1, x + 2):
                for ny in range(y - 1, y + 2):
                    neighbor = (nx, ny)
                    if neighbor in remaining:
                        remaining.remove(neighbor)
                        queue.append(neighbor)
                        group.append(by_pixel[neighbor])
        groups.append(group)
    return groups


def _occupancy_slot_centers(sd_points: list[tuple[float, float]], bins: int = 720) -> list[float]:
    """Fallback marker centers for non-analog perimeter artwork."""

    occupancy = _occupancy_histogram(sd_points, bins)
    if not any(occupancy):
        return []
    threshold = max(1, round(max(occupancy) * 0.015))
    active = [count >= threshold for count in occupancy]
    # Rasterized artwork can leave sparse holes along a single perimeter marker.
    # Fill only short s-gaps; hour-sized gaps remain separate slots.
    max_gap = max(2, round(bins * 0.02))
    index = 0
    while index < bins:
        if active[index]:
            index += 1
            continue
        end = index
        while end < bins and not active[end]:
            end += 1
        if index > 0 and end < bins and end - index <= max_gap:
            active[index:end] = [True] * (end - index)
        index = max(index + 1, end)
    runs: list[list[int]] = []
    for index, is_active in enumerate(active):
        if not is_active:
            continue
        if runs and index == runs[-1][-1] + 1:
            runs[-1].append(index)
        else:
            runs.append([index])
    wrap_gap = runs[0][0] + bins - runs[-1][-1] - 1 if len(runs) > 1 else bins
    if len(runs) > 1 and wrap_gap <= max_gap:
        runs[0] = runs[-1] + runs[0]
        runs.pop()
    centers: list[float] = []
    for run in runs:
        total = sum(occupancy[index] for index in run)
        centers.append(sum((index + 0.5) * occupancy[index] for index in run) / max(1, total) / bins)
    return centers


def _slot_points(
    sd_points: list[tuple[float, float, tuple[int, int]]],
    prior: list[dict[str, float]] | None,
    boundaries: list[float] | None = None,
) -> dict[int, list[tuple[int, int]]]:
    centers = [candidate["s"] for candidate in prior] if prior else _occupancy_slot_centers([(s, d) for s, d, _ in sd_points])
    slots: dict[int, list[tuple[int, int]]] = {}

    def assign_by_s(s: float) -> int:
        if boundaries and len(boundaries) == len(centers):
            return next(
                (candidate for candidate in range(len(boundaries)) if _circular_interval_contains(s, boundaries[candidate - 1], boundaries[candidate])),
                min(range(len(centers)), key=lambda candidate: _circular_distance(s, centers[candidate])),
            )
        return min(range(len(centers)), key=lambda candidate: _circular_distance(s, centers[candidate]))

    if not centers:
        return slots
    groups = _connected_sd_groups(sd_points)
    if not boundaries or len(boundaries) != len(centers):
        for s, _, point in sd_points:
            slots.setdefault(assign_by_s(s), []).append(point)
        return slots

    center_gaps = [
        (centers[(index + 1) % len(centers)] - centers[index]) % 1.0
        for index in range(len(centers))
    ]
    intact_span_limit = min(center_gaps) * 0.62
    for group in groups:
        values = [s for s, _, _ in group]
        nearest_slots = [min(range(len(centers)), key=lambda candidate: _circular_distance(s, centers[candidate])) for s in values]
        majority_slot = max(set(nearest_slots), key=nearest_slots.count)
        majority_ratio = nearest_slots.count(majority_slot) / max(1, len(nearest_slots))
        if majority_ratio >= 0.78:
            # A component can cross an s boundary because the radial projection
            # bends around a rounded corner.  If nearly all of its pixels agree
            # on one hour prior, keep the complete component there.  This is the
            # key guard against harvesting a small endpoint from the neighbor.
            slots.setdefault(majority_slot, []).extend(point for _, _, point in group)
            continue
        span = _circular_span(values)
        if span <= intact_span_limit:
            # Keep a coherent marker fragment together.  This prevents a
            # boundary from slicing the endpoint of a curved/outlined glyph.
            group_slot = min(range(len(centers)), key=lambda candidate: _circular_distance(_circular_mean(values), centers[candidate]))
            slots.setdefault(group_slot, []).extend(point for _, _, point in group)
            continue
        for s, _, point in group:
            slots.setdefault(assign_by_s(s), []).append(point)

    # A connected adjacent-marker component can leave a tiny detached-looking
    # tail after the s-boundary split.  Reassign only small raster subgroups
    # whose own prior vote disagrees with the receiving slot; legitimate
    # disconnected parts remain in place when they agree with their slot.
    sd_by_point = {point: (s, d, point) for s, d, point in sd_points}
    moves: list[tuple[int, int, list[tuple[int, int]]]] = []
    for slot_index, points in list(slots.items()):
        records = [sd_by_point[point] for point in points if point in sd_by_point]
        micro_limit = max(8, round(len(points) * 0.02))
        for subgroup in _connected_sd_groups(records):
            if len(subgroup) >= micro_limit:
                continue
            nearest_slots = [min(range(len(centers)), key=lambda candidate: _circular_distance(s, centers[candidate])) for s, _, _ in subgroup]
            target_slot = max(set(nearest_slots), key=nearest_slots.count)
            if target_slot != slot_index:
                moves.append((slot_index, target_slot, [point for _, _, point in subgroup]))
    for source_slot, target_slot, points in moves:
        point_set = set(points)
        slots[source_slot] = [point for point in slots[source_slot] if point not in point_set]
        slots.setdefault(target_slot, []).extend(points)

    return slots


def _marker_anchor(
    points: list[tuple[int, int]],
    shape: RoundedRect,
    lookup: tuple[list[float], list[float], float],
    prior: dict[str, float] | None,
) -> tuple[float, float]:
    """Return a stable semantic anchor instead of the crop bbox center."""

    if not prior:
        return _centroid(points)
    angle = prior["angle"]
    direction = direction_from_angle(angle)
    d_values = [_point_sd((float(x), float(y)), shape, lookup)[1] for x, y in points]
    d_values.sort()
    median_d = d_values[len(d_values) // 2]
    radius = max(0.0, shape.boundary_distance(direction) - median_d)
    return shape.center_x + direction[0] * radius, shape.center_y + direction[1] * radius


def _slot_component_count(points: list[tuple[int, int]], size: tuple[int, int]) -> int:
    mask = Image.new("L", size, 0)
    pixels = mask.load()
    for x, y in points:
        pixels[x, y] = 255
    return len(_components(mask, 1))


def _mapping_primitive(points: list[tuple[int, int]], bbox: tuple[int, int, int, int], source_shape: RoundedRect, lookup: tuple[list[float], list[float], float]) -> str:
    """Choose a mapping primitive from geometry, not semantic digit recognition."""

    s_values = [_point_sd((float(x), float(y)), source_shape, lookup)[0] for x, y in points]
    span = _circular_span(s_values)
    width, height = bbox[2] - bbox[0], bbox[3] - bbox[1]
    normalized_extent = max(width / source_shape.width, height / source_shape.height)
    return "PERIMETER_SD_WARP" if span >= 0.14 or normalized_extent >= 0.28 else "LOCAL_SIMILARITY"


def _extract_marker_asset(
    image: Image.Image,
    points: list[tuple[int, int]],
    bbox: tuple[int, int, int, int],
    anchor: tuple[float, float],
    background: tuple[int, int, int],
    padding: int = 4,
) -> tuple[Image.Image, tuple[float, float]]:
    """Extract a padded composite and retain its anchor in local asset space."""

    crop = image.convert("RGB").crop(bbox)
    group_mask = Image.new("L", crop.size, 0)
    group_pixels = group_mask.load()
    for x, y in points:
        local_x, local_y = x - bbox[0], y - bbox[1]
        if 0 <= local_x < crop.width and 0 <= local_y < crop.height:
            group_pixels[local_x, local_y] = 255
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
    padded = Image.new("RGBA", (result.width + padding * 2, result.height + padding * 2), (0, 0, 0, 0))
    padded.alpha_composite(result, (padding, padding))
    return padded, (anchor[0] - bbox[0] + padding, anchor[1] - bbox[1] + padding)


def decompose_perimeter_artwork(
    image: Image.Image,
    source_shape: RoundedRect,
    output_root: Path,
    *,
    exclusion_mask: Image.Image | None = None,
    manual_boundary_root: Path | None = None,
    manual_review_pairs: list[tuple[int, int]] | None = None,
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
    sd_points: list[tuple[float, float, tuple[int, int]]] = []
    for y in range(foreground.height):
        for x in range(foreground.width):
            if not foreground.getpixel((x, y)):
                continue
            normalized_radius = _normalized_radius((float(x), float(y)), source_shape)
            if normalized_radius < minimum_normalized_radius:
                continue
            s, d = _point_sd((float(x), float(y)), source_shape, lookup)
            sd_points.append((s, d, (x, y)))

    prior = _hour_position_prior([(s, d) for s, d, _ in sd_points], source_shape, lookup)
    boundaries = _hour_slot_boundaries([(s, d) for s, d, _ in sd_points], prior) if prior else None
    slots = _slot_points(sd_points, prior, boundaries)
    point_s = {point: s for s, _, point in sd_points}
    centers = [candidate["s"] for candidate in prior] if prior else []
    ambiguous_pairs = detect_ambiguous_adjacent_pairs(slots, point_s, centers, _circular_distance) if centers else []
    ambiguity_by_pair = {(record["slotA"], record["slotB"]): record for record in ambiguous_pairs}
    requested_reviews = []
    for first, second in manual_review_pairs or []:
        slot_a, slot_b = sorted((int(first), int(second)))
        requested_reviews.append(ambiguity_by_pair.get((slot_a, slot_b), {"slotA": slot_a, "slotB": slot_b, "disputedPixelCount": 0, "disputedRatio": 0.0}))
    boundary_reviews = generate_manual_boundary_review(image, slots, requested_reviews, output_root / "manual-boundary-review")
    manual_overrides = load_manual_boundary_ownership(manual_boundary_root, image.size)
    manual_removed_points: set[tuple[int, int]] = set()
    manually_overridden_slots: set[int] = set()
    for override in manual_overrides:
        slot_a, slot_b = int(override["slotA"]), int(override["slotB"])
        ownership = override["image"]
        pair_points = set(slots.get(slot_a, [])) | set(slots.get(slot_b, []))
        slots[slot_a] = [point for point in slots.get(slot_a, []) if point not in pair_points]
        slots[slot_b] = [point for point in slots.get(slot_b, []) if point not in pair_points]
        pixels = ownership.load()
        for point in pair_points:
            red, green, blue = pixels[point[0], point[1]]
            if red >= 192 and green < 128 and blue < 128:
                slots.setdefault(slot_a, []).append(point)
            elif green >= 192 and blue >= 192 and red < 128:
                slots.setdefault(slot_b, []).append(point)
            else:
                manual_removed_points.add(point)
        manually_overridden_slots.update((slot_a, slot_b))
    elements: list[dict[str, Any]] = []
    accepted_mask = Image.new("L", image.size, 0)
    local_similarity_mask = Image.new("L", image.size, 0)
    sd_warp_mask = Image.new("L", image.size, 0)
    accepted_pixels = accepted_mask.load()
    local_similarity_pixels = local_similarity_mask.load()
    sd_warp_pixels = sd_warp_mask.load()
    unwrap = Image.new("RGB", (720, 260), "#101010")
    unwrap_draw = ImageDraw.Draw(unwrap)
    unwrap_draw.text((8, 8), "normalized perimeter (s,d)", fill=(255, 255, 255))
    all_d = [d for _, d, _ in sd_points] or [1.0]
    d_scale = 205.0 / max(1.0, max(all_d))
    for slot_index in sorted(slots):
        points = slots[slot_index]
        if len(points) < minimum_area:
            continue
        prior_record = prior[slot_index] if prior and slot_index < len(prior) else None
        center = _marker_anchor(points, source_shape, lookup, prior_record)
        bbox = _component_bbox(points, 4, image.size)
        width, height = bbox[2] - bbox[0], bbox[3] - bbox[1]
        asset_name = f"perimeter_slot_{slot_index:02d}.png"
        asset, asset_anchor = _extract_marker_asset(image, points, bbox, center, background)
        asset.save(asset_root / asset_name)
        mapping_primitive = _mapping_primitive(points, bbox, source_shape, lookup)
        direction = (center[0] - source_shape.center_x, center[1] - source_shape.center_y)
        point_sd = [_point_sd((float(x), float(y)), source_shape, lookup) for x, y in points]
        s_values = [value[0] for value in point_sd]
        d_values = [value[1] for value in point_sd]
        d_min, d_max = min(d_values), max(d_values)
        s_mean = prior_record["s"] if prior_record else _circular_mean(s_values)
        radial_position = sum(_normalized_radius((float(x), float(y)), source_shape) for x, y in points) / len(points)
        component_count = _slot_component_count(points, image.size)
        for x, y in points:
            accepted_pixels[x, y] = 255
            if mapping_primitive == "PERIMETER_SD_WARP":
                sd_warp_pixels[x, y] = 255
            else:
                local_similarity_pixels[x, y] = 255
        confidence = min(0.99, 0.62 + max(0.0, radial_position - minimum_normalized_radius) * 0.8)
        elements.append(
            {
                "id": f"perimeter_slot_{slot_index:02d}",
                "type": "STATIC_ARTWORK",
                "representation": "PERIMETER_MARKER_SLOT",
                "dynamic": False,
                "bbox": {"x": bbox[0], "y": bbox[1], "width": width, "height": height},
                "anchor": {"x": round(center[0], 4), "y": round(center[1], 4)},
                "assetAnchor": {"x": round(asset_anchor[0], 4), "y": round(asset_anchor[1], 4)},
                "sourceAngleDeg": round(angle_from_direction(direction), 4),
                "perimeterPosition": round(s_mean, 6),
                "rotation": round(_principal_rotation(points, center), 4),
                "scale": 1.0,
                "mappingMode": mapping_primitive,
                "opticalOffsetX": 0.0,
                "opticalOffsetY": 0.0,
                "opticalScale": 1.0,
                "opticalRotation": 0.0,
                "confidence": round(confidence, 4),
                "asset": f"assets/perimeter/{asset_name}",
                "assetInstruction": {"operation": "extract_from_reference"},
                "zIndex": 2,
                "relationships": {
                    "detector": "marker-first normalized perimeter occupancy grouping",
                    "slotIndex": slot_index,
                    "componentCount": component_count,
                    "componentIndices": [f"slot-{slot_index}-component-{index}" for index in range(component_count)],
                    "normalizedRadius": round(radial_position, 6),
                    "normalizedPerimeterRange": {"s": round(_circular_span(s_values), 6), "dMin": round(d_min, 4), "dMax": round(d_max, 4)},
                    "sourceAssetBBox": {"x": bbox[0], "y": bbox[1], "width": width, "height": height},
                    "assetPaddingPx": 4,
                    "hourPositionPrior": prior_record is not None,
                    "slotBoundaryS": {"start": round(boundaries[slot_index - 1], 6), "end": round(boundaries[slot_index], 6)} if boundaries else None,
                    "semanticIdentificationRequired": False,
                    "manualBoundaryOverride": slot_index in manually_overridden_slots,
                    "mappingPrimitive": mapping_primitive,
                },
            }
        )
        unwrap_box = (round(s_mean * 708), round(24 + d_min * d_scale), round(min(719, s_mean * 708 + max(10, _circular_span(s_values) * 708))), round(24 + max(25, d_max * d_scale)))
        unwrap_draw.rectangle(unwrap_box, outline=(70, 220, 255), width=2)
        unwrap_draw.text((unwrap_box[0], max(25, unwrap_box[1] - 14)), str(slot_index), fill=(255, 230, 80))
        unwrap_draw.ellipse((round(s_mean * 708) - 3, round(24 + (sum(d_values) / len(d_values)) * d_scale) - 3, round(s_mean * 708) + 3, round(24 + (sum(d_values) / len(d_values)) * d_scale) + 3), fill=(255, 90, 90))

    for x, y in manual_removed_points:
        accepted_pixels[x, y] = 255
    foreground.save(output_root / "perimeter-foreground-mask.png")
    accepted_mask.save(output_root / "perimeter-artwork-mask.png")
    local_similarity_mask.save(output_root / "perimeter-local-similarity-mask.png")
    sd_warp_mask.save(output_root / "perimeter-sd-warp-mask.png")
    unwrap.save(output_root / "perimeter-sd-unwrap.png")
    report = {
        "sourceShape": source_shape.as_dict(),
        "backgroundColor": "#%02X%02X%02X" % background,
        "minimumNormalizedRadius": minimum_normalized_radius,
        "grouping": {
            "coordinateSystem": "normalized_perimeter_s_d",
            "representation": "PerimeterMarkerSlot",
            "slotCount": len(elements),
            "foregroundPointCount": len(sd_points),
            "mode": "hour_position_prior" if prior else "occupancy_runs",
            "hourPositionPriorCount": len(prior) if prior else 0,
            "slotBoundaryStrategy": "low_occupancy_valley_with_continuity_penalty" if boundaries else "occupancy_run_edges",
            "occupancyBins": 720,
            "ambiguousAdjacentPairs": ambiguous_pairs,
            "manualBoundaryReviewCount": len(boundary_reviews),
            "manualBoundaryOverrideCount": len(manual_overrides),
            "manualBackgroundPixelCount": len(manual_removed_points),
        },
        "elementCount": len(elements),
        "elements": elements,
        "mask": str(output_root / "perimeter-artwork-mask.png"),
        "localSimilarityMask": str(output_root / "perimeter-local-similarity-mask.png"),
        "sdWarpMask": str(output_root / "perimeter-sd-warp-mask.png"),
        "unwrap": str(output_root / "perimeter-sd-unwrap.png"),
        "manualBoundaryReviews": boundary_reviews,
        "manualBoundaryOverrides": [{key: value for key, value in override.items() if key != "image"} for override in manual_overrides],
    }
    (output_root / "perimeter-artwork.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n")
    return report


def remove_perimeter_artwork(image: Image.Image, mask: Image.Image, background: tuple[int, int, int] | None = None) -> Image.Image:
    result = image.convert("RGB").copy()
    result.paste(background or _background_color(image), mask=mask.convert("L"))
    return result


def transform_asset_with_anchor(
    asset: Image.Image,
    asset_anchor: tuple[float, float],
    target_anchor: tuple[float, float],
    scale: float,
    rotation: float,
    canvas_size: tuple[int, int],
) -> dict[str, Any]:
    """Transform a marker around its local anchor and measure canvas clipping."""

    source = asset.convert("RGBA")
    scaled_size = (max(1, round(source.width * scale)), max(1, round(source.height * scale)))
    scaled = source.resize(scaled_size, Image.Resampling.LANCZOS)
    scaled_anchor = (
        asset_anchor[0] * scaled.width / max(1, source.width),
        asset_anchor[1] * scaled.height / max(1, source.height),
    )
    transformed = scaled.rotate(-rotation, expand=True, resample=Image.Resampling.BICUBIC)

    theta = math.radians(-rotation)
    source_center = ((scaled.width - 1) / 2.0, (scaled.height - 1) / 2.0)
    transformed_center = ((transformed.width - 1) / 2.0, (transformed.height - 1) / 2.0)
    delta = (scaled_anchor[0] - source_center[0], scaled_anchor[1] - source_center[1])
    transformed_anchor = (
        transformed_center[0] + math.cos(theta) * delta[0] + math.sin(theta) * delta[1],
        transformed_center[1] - math.sin(theta) * delta[0] + math.cos(theta) * delta[1],
    )
    left = round(target_anchor[0] - transformed_anchor[0])
    top = round(target_anchor[1] - transformed_anchor[1])
    residual = (
        left + transformed_anchor[0] - target_anchor[0],
        top + transformed_anchor[1] - target_anchor[1],
    )

    alpha = transformed.getchannel("A")
    alpha_pixels = alpha.load()
    total_alpha = 0
    clipped_alpha = 0
    for y in range(transformed.height):
        for x in range(transformed.width):
            if alpha_pixels[x, y] == 0:
                continue
            total_alpha += 1
            if x + left < 0 or y + top < 0 or x + left >= canvas_size[0] or y + top >= canvas_size[1]:
                clipped_alpha += 1
    visible_left = max(0, left)
    visible_top = max(0, top)
    visible_right = min(canvas_size[0], left + transformed.width)
    visible_bottom = min(canvas_size[1], top + transformed.height)
    visible = None
    if visible_left < visible_right and visible_top < visible_bottom:
        visible = transformed.crop((visible_left - left, visible_top - top, visible_right - left, visible_bottom - top))
    return {
        "image": visible,
        "left": visible_left,
        "top": visible_top,
        "transformedAnchor": {"x": transformed_anchor[0], "y": transformed_anchor[1]},
        # Raster placement is integer-addressed. Report the maximum axis error
        # as the pixel residual; retain Euclidean error for diagnostics too.
        "anchorResidualPx": max(abs(residual[0]), abs(residual[1])),
        "anchorResidualEuclideanPx": math.hypot(*residual),
        "clippedPixelCount": clipped_alpha,
        "transformedAlphaPixelCount": total_alpha,
        "clippingRatio": clipped_alpha / max(1, total_alpha),
        "pixelRetentionRatio": 1.0 - clipped_alpha / max(1, total_alpha),
    }


def transform_asset_with_single_affine(
    asset: Image.Image,
    asset_anchor: tuple[float, float],
    target_anchor: tuple[float, float],
    scale: float,
    rotation: float,
    canvas_size: tuple[int, int],
    *,
    supersample: int = 4,
) -> dict[str, Any]:
    """Place a local asset with one affine resample and one final downsample."""

    source = asset.convert("RGBA")
    theta = math.radians(rotation)
    cos_theta, sin_theta = math.cos(theta), math.sin(theta)
    corners = [(0.0, 0.0), (float(source.width), 0.0), (0.0, float(source.height)), (float(source.width), float(source.height))]
    transformed_corners = []
    for x, y in corners:
        dx, dy = x - asset_anchor[0], y - asset_anchor[1]
        transformed_corners.append((target_anchor[0] + scale * (cos_theta * dx - sin_theta * dy), target_anchor[1] + scale * (sin_theta * dx + cos_theta * dy)))
    left = math.floor(min(point[0] for point in transformed_corners))
    top = math.floor(min(point[1] for point in transformed_corners))
    right = math.ceil(max(point[0] for point in transformed_corners))
    bottom = math.ceil(max(point[1] for point in transformed_corners))
    logical_size = (max(1, right - left), max(1, bottom - top))
    supersample = max(1, int(supersample))
    work_size = (logical_size[0] * supersample, logical_size[1] * supersample)
    inverse_scale = 1.0 / max(1e-6, scale)
    affine = (
        cos_theta * inverse_scale / supersample,
        sin_theta * inverse_scale / supersample,
        asset_anchor[0] + (cos_theta * (left - target_anchor[0]) + sin_theta * (top - target_anchor[1])) * inverse_scale,
        -sin_theta * inverse_scale / supersample,
        cos_theta * inverse_scale / supersample,
        asset_anchor[1] + (-sin_theta * (left - target_anchor[0]) + cos_theta * (top - target_anchor[1])) * inverse_scale,
    )
    transformed = source.transform(work_size, Image.Transform.AFFINE, affine, resample=Image.Resampling.BICUBIC, fillcolor=(0, 0, 0, 0))
    transformed = _premultiplied_resize(transformed, logical_size) if supersample > 1 else transformed
    alpha = transformed.getchannel("A")
    alpha_pixels = alpha.load()
    total_alpha = 0
    clipped_alpha = 0
    for y in range(transformed.height):
        for x in range(transformed.width):
            if alpha_pixels[x, y] == 0:
                continue
            total_alpha += 1
            if x + left < 0 or y + top < 0 or x + left >= canvas_size[0] or y + top >= canvas_size[1]:
                clipped_alpha += 1
    visible_left, visible_top = max(0, left), max(0, top)
    visible_right, visible_bottom = min(canvas_size[0], right), min(canvas_size[1], bottom)
    visible = None
    if visible_left < visible_right and visible_top < visible_bottom:
        visible = transformed.crop((visible_left - left, visible_top - top, visible_right - left, visible_bottom - top))
    transformed_anchor = {"x": target_anchor[0] - left, "y": target_anchor[1] - top}
    residual = (round(target_anchor[0]) - target_anchor[0], round(target_anchor[1]) - target_anchor[1])
    return {
        "image": visible,
        "left": visible_left,
        "top": visible_top,
        "transformedAnchor": transformed_anchor,
        "anchorResidualPx": max(abs(residual[0]), abs(residual[1])),
        "anchorResidualEuclideanPx": math.hypot(*residual),
        "clippedPixelCount": clipped_alpha,
        "transformedAlphaPixelCount": total_alpha,
        "clippingRatio": clipped_alpha / max(1, total_alpha),
        "pixelRetentionRatio": 1.0 - clipped_alpha / max(1, total_alpha),
    }


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
        asset_anchor_value = element.get("assetAnchor") or {"x": asset.width / 2.0, "y": asset.height / 2.0}
        target_anchor = (float(mapping["targetAnchor"]["x"]), float(mapping["targetAnchor"]["y"]))
        transformed = transform_asset_with_anchor(
            asset,
            (float(asset_anchor_value["x"]), float(asset_anchor_value["y"])),
            target_anchor,
            float(mapping["uniformScale"]),
            float(mapping["rotation"]),
            canvas.size,
        )
        if transformed["image"] is not None:
            canvas.alpha_composite(transformed["image"], (transformed["left"], transformed["top"]))
        mapped_records.append(
            {
                **mapping,
                "asset": element["asset"],
                "assetAnchor": asset_anchor_value,
                "renderedBbox": {"x": transformed["left"], "y": transformed["top"], "width": transformed["image"].width if transformed["image"] else 0, "height": transformed["image"].height if transformed["image"] else 0},
                "transformedAnchor": transformed["transformedAnchor"],
                "anchorResidualPx": round(transformed["anchorResidualPx"], 6),
                "anchorResidualEuclideanPx": round(transformed["anchorResidualEuclideanPx"], 6),
                "clippedPixelCount": transformed["clippedPixelCount"],
                "transformedAlphaPixelCount": transformed["transformedAlphaPixelCount"],
                "clippingRatio": round(transformed["clippingRatio"], 8),
                "pixelRetentionRatio": round(transformed["pixelRetentionRatio"], 8),
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
