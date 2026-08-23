from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from PIL import Image


def _time_parts(value: str) -> tuple[int, int, int]:
    parts = [int(part) for part in str(value).split(":")[:3]]
    while len(parts) < 3:
        parts.append(0)
    return parts[0] % 24, parts[1] % 60, parts[2] % 60


def _angle(role: str, value: str) -> float:
    hour, minute, second = _time_parts(value)
    if role == "HOUR":
        return ((hour % 12) + minute / 60 + second / 3600) * 30
    if role == "MINUTE":
        return (minute + second / 60) * 6
    return second * 6


def _is_light(pixel: tuple[int, int, int]) -> bool:
    return max(pixel) >= 135 and max(pixel) - min(pixel) <= 110


def _is_red(pixel: tuple[int, int, int]) -> bool:
    red, green, blue = pixel
    return red >= 85 and red >= green * 1.35 and red >= blue * 1.15


def _point(center: tuple[float, float], angle: float, radius: float, perpendicular: float) -> tuple[int, int]:
    radians = math.radians(angle)
    return (
        round(center[0] + math.sin(radians) * radius + math.cos(radians) * perpendicular),
        round(center[1] - math.cos(radians) * radius + math.sin(radians) * perpendicular),
    )


def _line_mask(center: tuple[float, float], angle: float, length: float, width: float) -> set[tuple[int, int]]:
    points: set[tuple[int, int]] = set()
    for radius in range(0, round(length) + 1):
        for perpendicular in range(-round(width), round(width) + 1):
            points.add(_point(center, angle, radius, perpendicular))
    return points


def _run_score(image: Image.Image, center: tuple[float, float], angle: float, length: float, role: str) -> float:
    pixels = image.convert("RGB").load()
    predicate = _is_red if role == "SECOND" else _is_light
    last = 0
    gap = 0
    started = False
    for radius in range(18, round(length) + 1):
        present = False
        for perpendicular in range(-6, 7):
            x, y = _point(center, angle, radius, perpendicular)
            if 0 <= x < image.width and 0 <= y < image.height and predicate(pixels[x, y]):
                present = True
                break
        if present:
            started = True
            last = radius
            gap = 0
        elif started:
            gap += 1
            if gap >= 3:
                break
    return max(0.0, (last - 18) / max(1.0, length - 18))


def _roi_changed_fraction(first: Image.Image, second: Image.Image, roi: set[tuple[int, int]]) -> float:
    first_pixels = first.convert("RGB").load()
    second_pixels = second.convert("RGB").load()
    valid = 0
    changed = 0
    for x, y in roi:
        if not (0 <= x < first.width and 0 <= y < first.height):
            continue
        valid += 1
        if sum(abs(a - b) for a, b in zip(first_pixels[x, y], second_pixels[x, y])) >= 30:
            changed += 1
    return round(changed / valid, 6) if valid else 0.0


def validate_dynamic_renders(scene: dict[str, Any], renders: dict[str, Path]) -> dict[str, Any]:
    """Check moving-hand ROIs and persistent source-angle ghosts across fixed renders."""
    clock = scene.get("clock", {})
    center = (float(clock.get("centerX", 219)), float(clock.get("centerY", 219)))
    hands = [element for element in scene["elements"] if element["type"] == "ANALOG_HAND"]
    images = {time: Image.open(path).convert("RGB") for time, path in renders.items()}
    roi_by_time: dict[str, set[tuple[int, int]]] = {}
    for time in renders:
        roi: set[tuple[int, int]] = set()
        for hand in hands:
            roi |= _line_mask(center, _angle(hand["role"], time), hand["length"], max(7, hand["thickness"] + 4))
        roi_by_time[time] = roi
    pairwise: dict[str, float] = {}
    times = list(renders)
    for index, first_time in enumerate(times):
        for second_time in times[index + 1 :]:
            key = f"{first_time} vs {second_time}"
            pairwise[key] = _roi_changed_fraction(images[first_time], images[second_time], roi_by_time[first_time] | roi_by_time[second_time])
    dynamic_passed = all(value >= 0.005 for value in pairwise.values()) if pairwise else False

    ghost_scores: dict[str, dict[str, float]] = {}
    for hand in hands:
        role = hand["role"]
        source_angle = float(hand["observedAngleDeg"])
        role_scores: dict[str, float] = {}
        for time in times:
            if abs((_angle(role, time) - source_angle + 180) % 360 - 180) <= 20:
                continue
            role_scores[time] = round(_run_score(images[time], center, source_angle, float(hand["length"]), role), 6)
        ghost_scores[role] = role_scores
    off_source_max = max((score for role_scores in ghost_scores.values() for score in role_scores.values()), default=0.0)
    ghost_passed = off_source_max < 0.25
    return {
        "renders": {time: str(path) for time, path in renders.items()},
        "dynamicHandROI": {"pairwiseChangedPixelFraction": pairwise, "threshold": 0.005, "passed": dynamic_passed},
        "sourceAngleGhost": {"scores": ghost_scores, "threshold": 0.25, "maxOffSourceScore": round(off_source_max, 6), "passed": ghost_passed},
        "passed": dynamic_passed and ghost_passed,
        "deviceOrEmulatorCapture": "deferred: no Wear OS runtime was available",
    }
