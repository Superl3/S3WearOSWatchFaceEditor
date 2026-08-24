from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw

from .occlusion import _fill_simple_background
from .perimeter_artwork import _background_color, _components, _foreground_mask


def _bbox(points: list[tuple[int, int]]) -> tuple[int, int, int, int]:
    return min(x for x, _ in points), min(y for _, y in points), max(x for x, _ in points) + 1, max(y for _, y in points) + 1


def extract_center_dynamic_text(
    image: Image.Image,
    output_root: Path,
    *,
    exclusion_mask: Image.Image | None = None,
    reconstruction_image: Image.Image | None = None,
) -> dict[str, Any]:
    """Find a compact center text row and split it into weekday and date candidates.

    This is deliberately structural: it does not OCR the artwork or depend on a
    reference-specific coordinate. Semantic confidence comes from a left alpha
    token followed by a shorter right numeric token in the central dial band.
    """

    output_root.mkdir(parents=True, exist_ok=True)
    mask = _foreground_mask(image, _background_color(image), threshold=55)
    if exclusion_mask is not None:
        mask = ImageChops.subtract(mask, exclusion_mask.convert("L"))
    width, height = image.size
    center_box = (round(width * 0.18), round(height * 0.28), round(width * 0.82), round(height * 0.72))
    candidates: list[tuple[int, int, int, int]] = []
    for points in _components(mask, minimum_area=max(5, width * height // 30000)):
        box = _bbox(points)
        if box[0] < center_box[0] or box[1] < center_box[1] or box[2] > center_box[2] or box[3] > center_box[3]:
            continue
        component_width, component_height = box[2] - box[0], box[3] - box[1]
        if 2 <= component_width <= width * 0.16 and height * 0.018 <= component_height <= height * 0.16:
            candidates.append(box)
    candidates.sort(key=lambda box: (box[1] + box[3], box[0]))

    best: list[tuple[int, int, int, int]] = []
    for seed in candidates:
        row = [box for box in candidates if abs((box[1] + box[3]) / 2 - (seed[1] + seed[3]) / 2) <= max(seed[3] - seed[1], box[3] - box[1]) * 0.65]
        row = sorted(set(row), key=lambda box: box[0])
        if len(row) >= 4 and (not best or len(row) > len(best)):
            best = row

    if not best:
        pixels = mask.load()
        row_counts = [sum(1 for x in range(center_box[0], center_box[2]) if pixels[x, y]) for y in range(center_box[1], center_box[3])]
        peak = max(range(len(row_counts)), key=row_counts.__getitem__) + center_box[1]
        threshold = max(2, round(row_counts[peak - center_box[1]] * 0.08))
        top = peak
        bottom = peak + 1
        while top > center_box[1] and row_counts[top - center_box[1] - 1] >= threshold:
            top -= 1
        while bottom < center_box[3] and row_counts[bottom - center_box[1]] >= threshold:
            bottom += 1
        active_columns = [x for x in range(center_box[0], center_box[2]) if any(pixels[x, y] for y in range(top, bottom))]
        runs: list[tuple[int, int, int, int]] = []
        for x in active_columns:
            if not runs or x > runs[-1][2] + 1:
                runs.append((x, top, x, bottom))
            else:
                runs[-1] = (runs[-1][0], top, x, bottom)
        if len(runs) >= 4:
            best = [(left, top, right + 1, bottom) for left, top, right, bottom in runs]

    elements: list[dict[str, Any]] = []
    removal = Image.new("L", image.size, 0)
    if best:
        gaps = [(best[index + 1][0] - best[index][2], index) for index in range(len(best) - 1)]
        _, split_index = max(gaps)
        groups = (("weekday", "WEEKDAY", best[: split_index + 1]), ("day_of_month", "DATE_DAY_OF_MONTH", best[split_index + 1 :]))
        for element_id, semantic_type, group in groups:
            if not group:
                continue
            left = min(box[0] for box in group)
            top = min(box[1] for box in group)
            right = max(box[2] for box in group)
            bottom = max(box[3] for box in group)
            padding = max(2, round((bottom - top) * 0.16))
            box = {
                "x": max(0, left - padding),
                "y": max(0, top - padding),
                "width": min(width, right + padding) - max(0, left - padding),
                "height": min(height, bottom + padding) - max(0, top - padding),
            }
            ImageDraw.Draw(removal).rectangle((box["x"], box["y"], box["x"] + box["width"], box["y"] + box["height"]), fill=255)
            elements.append(
                {
                    "id": element_id,
                    "type": "WEEKDAY" if semantic_type == "WEEKDAY" else "DYNAMIC_SLOT",
                    "slotType": semantic_type if semantic_type != "WEEKDAY" else None,
                    "dynamic": True,
                    "bbox": box,
                    "style": {"fontFamily": "Pretendard", "fontWeight": 400, "fontSize": max(10, round((bottom - top) * 1.18)), "alignment": "center", "color": "#FFFFFF"},
                    "confidence": round(min(0.9, 0.58 + 0.04 * len(group)), 3),
                    "zIndex": 6,
                    "relationships": {"detection": "central aligned component row", "semanticHeuristic": semantic_type},
                }
            )
            if elements[-1].get("slotType") is None:
                elements[-1].pop("slotType", None)

    before = (reconstruction_image or image).convert("RGB")
    completed, reconstructed = _fill_simple_background(before, before, removal, (width / 2, height / 2))
    unresolved = ImageChops.subtract(removal, reconstructed)
    if unresolved.getbbox():
        completed.paste(_background_color(image), mask=unresolved)
        reconstructed = ImageChops.lighter(reconstructed, unresolved)

    removal.save(output_root / "dynamic-text-mask.png")
    completed.save(output_root / "dynamic-text-removed.png")
    report = {
        "elements": elements,
        "candidateComponentCount": len(candidates),
        "detected": len(elements) == 2,
        "removalMask": str(output_root / "dynamic-text-mask.png"),
        "cleanBackground": str(output_root / "dynamic-text-removed.png"),
        "reconstruction": "existing deterministic simple-background reconstruction",
        "requiresHumanReview": len(elements) != 2,
    }
    (output_root / "dynamic-text.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n")
    return report
