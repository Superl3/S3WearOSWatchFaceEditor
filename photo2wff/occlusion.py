from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFilter


REGION_CLASSES = ("solid_background", "line_geometry", "numeral_text_logo", "complex_artwork")


def _count_mask_pixels(mask: Image.Image) -> int:
    return sum(1 for value in mask.tobytes() if value)


def _clock_point(center: tuple[float, float], angle: float, radius: float) -> tuple[int, int]:
    radians = math.radians(angle)
    return round(center[0] + math.sin(radians) * radius), round(center[1] - math.cos(radians) * radius)


def _pixel_strength(pixel: tuple[int, int, int]) -> int:
    return max(pixel)


def _color_distance(first: tuple[int, int, int], second: tuple[int, int, int]) -> int:
    return sum(abs(first[channel] - second[channel]) for channel in range(3))


def _is_second_hand_red(pixel: tuple[int, int, int]) -> bool:
    red, green, blue = pixel
    return red >= 85 and red >= green * 1.35 and red >= blue * 1.15


def _hand_masks(reference: Image.Image, center: tuple[float, float], hands: dict[str, dict[str, Any]], margin: int = 4) -> dict[str, Image.Image]:
    masks: dict[str, Image.Image] = {}
    for role, hand in hands.items():
        mask = Image.new("L", reference.size, 0)
        draw = ImageDraw.Draw(mask)
        thickness = max(2, round(float(hand.get("thickness", 1))))
        width = thickness + margin * 2
        length = float(hand.get("length", 0)) + margin
        bbox = hand.get("bbox", {})
        asset_height = float(bbox.get("height", length))
        tail = max(0.0, asset_height - float(hand.get("length", length))) + margin
        start = -round(tail)
        end = round(length)
        angle = float(hand.get("observedAngleDeg", 0))
        endpoint = _clock_point(center, angle, end)
        draw.line((center[0], center[1], endpoint[0], endpoint[1]), fill=255, width=width)
        tail_endpoint = _clock_point(center, (angle + 180) % 360, -start)
        draw.line((tail_endpoint[0], tail_endpoint[1], center[0], center[1]), fill=255, width=width)
        # One-pixel max filter accounts for antialiased hand edges without
        # reopening the broad corridor used by the old A1 cleanup pass.
        masks[role] = mask.filter(ImageFilter.MaxFilter(3))
    return masks


def _union_masks(masks: dict[str, Image.Image]) -> Image.Image:
    result: Image.Image | None = None
    for mask in masks.values():
        result = mask.copy() if result is None else ImageChops.lighter(result, mask)
    return result or Image.new("L", (438, 438), 0)


def _region_bbox(mask: Image.Image) -> tuple[int, int, int, int] | None:
    return mask.getbbox()


def _region_radius(mask: Image.Image, center: tuple[float, float]) -> float:
    bbox = mask.getbbox()
    if not bbox:
        return 0.0
    left, top, right, bottom = bbox
    points = (
        (left, top),
        (right, top),
        (left, bottom),
        (right, bottom),
    )
    return max(math.hypot(point[0] - center[0], point[1] - center[1]) for point in points)


def _classify_region(reference: Image.Image, mask: Image.Image, center: tuple[float, float]) -> tuple[str, float, str]:
    bbox = mask.getbbox()
    if not bbox:
        return "solid_background", 1.0, "empty_mask"
    pixels = reference.convert("RGB").load()
    mask_pixels = mask.load()
    bright_border = 0
    total_border = 0
    left, top, right, bottom = bbox
    for y in range(max(0, top - 3), min(reference.height, bottom + 3)):
        for x in range(max(0, left - 3), min(reference.width, right + 3)):
            if mask_pixels[x, y]:
                continue
            near_mask = any(
                0 <= x + dx < reference.width
                and 0 <= y + dy < reference.height
                and mask_pixels[x + dx, y + dy]
                for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1))
            )
            if near_mask:
                total_border += 1
                bright_border += _pixel_strength(pixels[x, y]) >= 35
    if bright_border == 0:
        return "solid_background", 0.94, "no_visible_static_stroke_near_occlusion"
    radius = _region_radius(mask, center)
    if radius < 120:
        return "line_geometry", 0.68, "bright_contour_near_inner_hand_corridor"
    if radius < 205:
        return "numeral_text_logo", 0.48, "outer_dial_glyph_band_intersection"
    confidence = 0.28 if total_border else 0.15
    return "complex_artwork", confidence, "outer_region_requires_nonlocal_structure"


def _sample_unmasked(pixels: Any, mask_pixels: Any, size: tuple[int, int], x: float, y: float) -> tuple[int, int, int] | None:
    ix, iy = round(x), round(y)
    if not (0 <= ix < size[0] and 0 <= iy < size[1]) or mask_pixels[ix, iy]:
        return None
    return pixels[ix, iy]


def _fill_simple_background(reference: Image.Image, before: Image.Image, mask: Image.Image, center: tuple[float, float]) -> tuple[Image.Image, Image.Image]:
    """Restore pixels whose two sides agree on a low-contrast local background."""

    source = reference.convert("RGB")
    result = before.convert("RGB").copy()
    source_pixels = source.load()
    mask_pixels = mask.load()
    result_pixels = result.load()
    changed = Image.new("L", reference.size, 0)
    changed_pixels = changed.load()
    for y in range(reference.height):
        for x in range(reference.width):
            if not mask_pixels[x, y]:
                continue
            dx, dy = x - center[0], y - center[1]
            radius = max(1.0, math.hypot(dx, dy))
            normal = (-dy / radius, dx / radius)
            pair: tuple[tuple[int, int, int], tuple[int, int, int]] | None = None
            for distance in (2, 3, 4, 6, 8, 12, 16):
                first = _sample_unmasked(source_pixels, mask_pixels, source.size, x + normal[0] * distance, y + normal[1] * distance)
                second = _sample_unmasked(source_pixels, mask_pixels, source.size, x - normal[0] * distance, y - normal[1] * distance)
                if first is None or second is None:
                    continue
                if _pixel_strength(first) <= 35 and _pixel_strength(second) <= 35 and _color_distance(first, second) <= 60:
                    pair = (first, second)
                    break
            if pair is None and _pixel_strength(result_pixels[x, y]) <= 35:
                nearby: list[tuple[int, int, int]] = []
                for distance in (6, 8, 10, 12, 16, 20):
                    for side in (-1, 1):
                        sample = _sample_unmasked(
                            source_pixels,
                            mask_pixels,
                            source.size,
                            x + normal[0] * distance * side,
                            y + normal[1] * distance * side,
                        )
                        if sample is not None and _pixel_strength(sample) <= 35:
                            nearby.append(sample)
                if nearby:
                    background = min(nearby, key=_pixel_strength)
                    result_pixels[x, y] = background
                    changed_pixels[x, y] = 255
                    continue
            if pair is not None and _pixel_strength(result_pixels[x, y]) <= 35:
                result_pixels[x, y] = tuple(round((pair[0][channel] + pair[1][channel]) / 2) for channel in range(3))
                changed_pixels[x, y] = 255
                continue
            if pair is None:
                continue
    return result, changed


def _bridge_strokes(
    reference: Image.Image,
    before: Image.Image,
    mask: Image.Image,
    center: tuple[float, float],
    max_offset: int = 18,
    role: str | None = None,
) -> tuple[Image.Image, Image.Image]:
    source = reference.convert("RGB")
    result = before.convert("RGB").copy()
    source_pixels = source.load()
    mask_pixels = mask.load()
    result_pixels = result.load()
    changed = Image.new("L", reference.size, 0)
    changed_pixels = changed.load()
    for y in range(reference.height):
        for x in range(reference.width):
            if not mask_pixels[x, y]:
                continue
            dx, dy = x - center[0], y - center[1]
            radius = math.hypot(dx, dy)
            if radius < 3:
                continue
            angle = math.atan2(dx, -dy)
            angular_step = max(0.7, 1.25 / radius)
            pairs: list[tuple[tuple[int, int, int], tuple[int, int, int]]] = []
            for distance in range(1, max_offset + 1):
                offset = angular_step * distance
                pair = (
                    _sample_unmasked(source_pixels, mask_pixels, source.size, center[0] + math.sin(angle - offset) * radius, center[1] - math.cos(angle - offset) * radius),
                    _sample_unmasked(source_pixels, mask_pixels, source.size, center[0] + math.sin(angle + offset) * radius, center[1] - math.cos(angle + offset) * radius),
                )
                if pair[0] is not None and pair[1] is not None:
                    pairs.append((pair[0], pair[1]))
                    break
            candidate = next(
                (
                    pair
                    for pair in pairs
                    if _pixel_strength(pair[0]) >= 35
                    and _pixel_strength(pair[1]) >= 35
                    and _color_distance(pair[0], pair[1]) <= 180
                    and not (role == "SECOND" and (_is_second_hand_red(pair[0]) or _is_second_hand_red(pair[1])))
                ),
                None,
            )
            if candidate is None:
                continue
            result_pixels[x, y] = tuple(round((candidate[0][channel] + candidate[1][channel]) / 2) for channel in range(3))
            changed_pixels[x, y] = 255
    return result, changed


def _apply_fallback(candidate_path: Path | None, result: Image.Image, unresolved: Image.Image) -> tuple[Image.Image, Image.Image, str]:
    if candidate_path is None or not candidate_path.exists():
        return result, Image.new("L", result.size, 0), "not_configured"
    candidate = Image.open(candidate_path).convert("RGB").resize(result.size, Image.Resampling.LANCZOS)
    output = result.copy().convert("RGB")
    output.paste(candidate, mask=unresolved)
    return output, unresolved.copy(), "external_generative_candidate"


def reconstruct_occluded_dial(
    reference_path: Path,
    assets_dir: Path,
    output_dir: Path,
    center: tuple[float, float],
    hands: dict[str, dict[str, Any]],
    generative_fallback_path: Path | None = None,
    margin: int = 4,
) -> dict[str, Any]:
    """Complete static artwork without treating synthesized pixels as observed truth."""

    reference = Image.open(reference_path).convert("RGB")
    dial_path = assets_dir / "dial_clean.png"
    if not dial_path.exists():
        raise FileNotFoundError(f"dial-before source not found: {dial_path}")
    before = Image.open(dial_path).convert("RGB")
    masks = _hand_masks(reference, center, hands, margin=margin)
    union = _union_masks(masks)
    observed = ImageChops.invert(union)
    reconstructed = Image.new("L", reference.size, 0)
    completed = before.copy()
    regions: list[dict[str, Any]] = []
    for role, mask in masks.items():
        region_class, class_confidence, reason = _classify_region(reference, mask, center)
        region_before = completed.copy()
        region_reconstructed = Image.new("L", reference.size, 0)
        method = "deterministic_background"
        confidence = class_confidence
        requires_review = False
        completed, background_reconstructed = _fill_simple_background(reference, completed, mask, center)
        region_reconstructed = ImageChops.lighter(region_reconstructed, background_reconstructed)
        if region_class == "line_geometry":
            completed, stroke_reconstructed = _bridge_strokes(reference, completed, mask, center, role=role)
            region_reconstructed = ImageChops.lighter(region_reconstructed, stroke_reconstructed)
            method = "two_sided_contour_continuation"
            confidence = min(class_confidence, 0.62 if region_reconstructed.getbbox() else 0.18)
            requires_review = region_reconstructed.getbbox() is None
        elif region_class == "numeral_text_logo":
            completed, stroke_reconstructed = _bridge_strokes(reference, completed, mask, center, max_offset=24, role=role)
            region_reconstructed = ImageChops.lighter(region_reconstructed, stroke_reconstructed)
            method = "visible_glyph_stroke_continuation"
            confidence = min(class_confidence, 0.55 if region_reconstructed.getbbox() else 0.12)
            requires_review = True
        elif region_class == "complex_artwork":
            method = "generative_inpainting_fallback"
            requires_review = True
            confidence = 0.0
        else:
            completed = completed.copy()
        reconstructed = ImageChops.lighter(reconstructed, region_reconstructed)
        regions.append(
            {
                "id": role.lower(),
                "sourceHandRole": role,
                "class": region_class,
                "classReason": reason,
                "bbox": list(_region_bbox(mask) or (0, 0, 0, 0)),
                "method": method,
                "confidence": round(confidence, 4),
                "reconstructedPixelCount": _count_mask_pixels(region_reconstructed),
                "requiresHumanReview": requires_review,
                "fallback": "pending" if method == "generative_inpainting_fallback" and generative_fallback_path is None else None,
                "beforeChangedByRegion": ImageChops.difference(region_before, completed).getbbox() is not None,
            }
        )
    unresolved = ImageChops.subtract(union, reconstructed)
    completed, fallback_mask, fallback_status = _apply_fallback(generative_fallback_path, completed, unresolved)
    reconstructed = ImageChops.lighter(reconstructed, fallback_mask)
    unresolved = ImageChops.subtract(union, reconstructed)
    assets_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    observed.save(assets_dir / "observed-mask.png")
    union.save(assets_dir / "hand-occlusion-mask.png")
    before.save(assets_dir / "dial-before-reconstruction.png")
    reconstructed.save(assets_dir / "reconstructed-mask.png")
    completed.save(assets_dir / "dial-completed.png")
    completed.save(assets_dir / "dial_clean.png")
    unresolved.save(assets_dir / "unresolved-mask.png")
    unresolved_count = _count_mask_pixels(unresolved)
    report = {
        "engine": "Occlusion Reconstruction Engine",
        "version": "A1d.1",
        "reference": str(reference_path),
        "marginPixels": margin,
        "sourceHandRoles": list(hands),
        "regions": regions,
        "fallback": {"status": fallback_status, "candidate": str(generative_fallback_path) if generative_fallback_path else None},
        "pixelCounts": {
            "observed": _count_mask_pixels(observed),
            "occluded": _count_mask_pixels(union),
            "reconstructed": _count_mask_pixels(reconstructed),
            "unresolved": unresolved_count,
        },
        "requiresHumanReview": bool(unresolved_count or any(region["requiresHumanReview"] for region in regions)),
        "generatedPixelsAreObservedTruth": False,
        "status": "completed_with_review" if unresolved_count else "completed",
    }
    (output_dir / "occlusion-metadata.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return report
