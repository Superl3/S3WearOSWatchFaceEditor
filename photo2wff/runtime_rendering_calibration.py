from __future__ import annotations

import copy
import json
import math
import shutil
from pathlib import Path
from statistics import median
from typing import Any, Callable

from PIL import Image, ImageDraw

from .measurement_correctness import (
    CANVAS_SIZE,
    DIAGNOSTIC_TIME,
    _build_project,
    _compile_diagnostic,
    _diagnostic_scene,
    _fit_geometry,
    _foreground_bbox,
    _geometry_source,
    _inverse_map_point,
    _map_point,
)
from .runtime_validation import capture_runtime_image, detect_runtime
from .wff_render import render_wff_xml


LINE_WIDTHS = (1, 2, 3, 4, 8, 13)
PROFILE_SCHEMA_VERSION = "1.0"


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def _manual_glyph(scene: dict[str, Any], source_root: Path, character: str = "8") -> tuple[Path, dict[str, int]]:
    slot = next(element for element in scene["elements"] if element.get("type") == "DYNAMIC_SLOT")
    manual = slot.get("manualGlyphs") or {}
    relative_path = manual.get("resources", {}).get(character)
    if not relative_path:
        raise ValueError(f"manual scene does not provide glyph {character}.png")
    source = source_root / relative_path
    if not source.is_file():
        raise FileNotFoundError(source)
    with Image.open(source) as image:
        native_width, native_height = image.size
    metrics = manual.get("metrics", {}).get(character, {})
    return source, {
        "width": int(metrics.get("width", native_width)),
        "height": int(metrics.get("height", native_height)),
        "nativeWidth": native_width,
        "nativeHeight": native_height,
    }


def _bitmap_diagnostic(
    source_scene: dict[str, Any],
    manual_scene: dict[str, Any],
    manual_root: Path,
    output_root: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    source = output_root / "sources" / "bitmap-font"
    asset_root = source / "assets"
    asset_root.mkdir(parents=True, exist_ok=True)
    glyph_source, native_metrics = _manual_glyph(manual_scene, manual_root)
    glyph_asset = asset_root / "manual_8.png"
    shutil.copy2(glyph_source, glyph_asset)
    Image.new("RGB", (CANVAS_SIZE, CANVAS_SIZE), "black").save(source / "preview.png")

    visual_height = 24
    visual_width = round(native_metrics["nativeWidth"] * visual_height / native_metrics["nativeHeight"])
    cells = (
        ("part_image", "PART_IMAGE", 10, 100, 96, 48, native_metrics, "center"),
        ("native_h29", "BITMAP_FONT", 116, 100, 96, 29, native_metrics, "center"),
        ("native_h48", "BITMAP_FONT", 222, 100, 96, 48, native_metrics, "center"),
        ("native_h72", "BITMAP_FONT", 328, 100, 96, 72, native_metrics, "center"),
        ("half_metrics", "BITMAP_FONT", 10, 260, 96, 48, {"width": 83, "height": 101}, "center"),
        ("square_metrics", "BITMAP_FONT", 116, 260, 96, 48, {"width": 166, "height": 166}, "center"),
        ("native_start", "BITMAP_FONT", 222, 260, 96, 48, native_metrics, "left"),
        ("native_end", "BITMAP_FONT", 328, 260, 96, 48, native_metrics, "right"),
    )
    elements: list[dict[str, Any]] = []
    metadata: dict[str, Any] = {}
    for name, kind, x, y, width, height, metrics, alignment in cells:
        container = {"x": x, "y": y, "width": width, "height": height}
        if kind == "PART_IMAGE":
            image_bbox = {
                "x": round(x + (width - visual_width) / 2),
                "y": round(y + (height - visual_height) / 2),
                "width": visual_width,
                "height": visual_height,
            }
            elements.append({
                "id": name,
                "type": "STATIC_IMAGE",
                "dynamic": False,
                "bbox": image_bbox,
                "asset": "assets/manual_8.png",
                "confidence": 1.0,
            })
        else:
            family = f"Photo2WFFDiagnostic{name.replace('_', '').title()}"
            elements.append({
                "id": name,
                "type": "DYNAMIC_SLOT",
                "slotType": "DATE_DAY_OF_MONTH",
                "dynamic": True,
                "bbox": container,
                "format": "d",
                "style": {
                    "fontFamily": "Pretendard",
                    "fontWeight": 400,
                    "fontSize": 24,
                    "alignment": alignment,
                    "color": "#FFFFFF",
                },
                "manualGlyphs": {
                    "type": "MANUAL_GLYPH_OVERRIDE",
                    "family": family,
                    "resources": {"8": "assets/manual_8.png"},
                    "metrics": {"8": {"width": int(metrics["width"]), "height": int(metrics["height"])}},
                    "providedDigits": ["8"],
                    "automaticSynthesis": False,
                },
                "confidence": 1.0,
            })
        metadata[name] = {
            "kind": kind,
            "container": container,
            "glyphMetrics": {"width": int(metrics["width"]), "height": int(metrics["height"])},
            "alignment": alignment,
        }
    scene = _diagnostic_scene(source_scene, elements)
    return _compile_diagnostic("bitmap-font", scene, source, output_root), metadata


def _resampling_diagnostic(source_scene: dict[str, Any], output_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    source = output_root / "sources" / "resampling"
    assets = source / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (CANVAS_SIZE, CANVAS_SIZE), "black").save(source / "preview.png")
    elements: list[dict[str, Any]] = []
    metadata: dict[str, Any] = {}
    for index, width in enumerate(LINE_WIDTHS):
        vertical = Image.new("RGBA", (24, 100), (0, 0, 0, 0))
        left = (vertical.width - width) // 2
        ImageDraw.Draw(vertical).rectangle((left, 0, left + width - 1, vertical.height - 1), fill=(255, 255, 255, 255))
        vertical_asset = assets / f"vertical_{width}.png"
        vertical.save(vertical_asset)
        vertical_bbox = {"x": 50 + index * 62, "y": 80, "width": 24, "height": 100}
        vertical_id = f"vertical_{width}"
        elements.append({"id": vertical_id, "type": "STATIC_IMAGE", "dynamic": False, "bbox": vertical_bbox, "asset": f"assets/{vertical_asset.name}", "confidence": 1.0})
        metadata[vertical_id] = {"orientation": "VERTICAL", "knownWidthPx": width, "bbox": vertical_bbox}

        horizontal = Image.new("RGBA", (140, 24), (0, 0, 0, 0))
        top = (horizontal.height - width) // 2
        ImageDraw.Draw(horizontal).rectangle((0, top, horizontal.width - 1, top + width - 1), fill=(255, 255, 255, 255))
        horizontal_asset = assets / f"horizontal_{width}.png"
        horizontal.save(horizontal_asset)
        horizontal_bbox = {"x": 149, "y": 210 + index * 34, "width": 140, "height": 24}
        horizontal_id = f"horizontal_{width}"
        elements.append({"id": horizontal_id, "type": "STATIC_IMAGE", "dynamic": False, "bbox": horizontal_bbox, "asset": f"assets/{horizontal_asset.name}", "confidence": 1.0})
        metadata[horizontal_id] = {"orientation": "HORIZONTAL", "knownWidthPx": width, "bbox": horizontal_bbox}
    scene = _diagnostic_scene(source_scene, elements)
    return _compile_diagnostic("resampling", scene, source, output_root), metadata


def _pivot_diagnostic(source_scene: dict[str, Any], output_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    source = output_root / "sources" / "pivot"
    assets = source / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (CANVAS_SIZE, CANVAS_SIZE), "black").save(source / "preview.png")
    fiducials = Image.new("RGBA", (CANVAS_SIZE, CANVAS_SIZE), (0, 0, 0, 0))
    fiducial_points = ((207, 207), (231, 207), (207, 231), (231, 231))
    draw = ImageDraw.Draw(fiducials)
    for x, y in fiducial_points:
        draw.ellipse((x - 4, y - 4, x + 4, y + 4), fill=(0, 255, 255, 255))
    fiducials.save(assets / "pivot_fiducials.png")

    hand = Image.new("RGBA", (20, 180), (0, 0, 0, 0))
    hand_draw = ImageDraw.Draw(hand)
    hand_draw.line((10, 8, 10, 171), fill=(255, 255, 255, 255), width=3)
    hand_draw.ellipse((6, 131, 14, 139), fill=(255, 0, 0, 255))
    hand.save(assets / "pivot_hand.png")
    elements = [
        {
            "id": "pivot_fiducials",
            "type": "STATIC_IMAGE",
            "dynamic": False,
            "bbox": {"x": 0, "y": 0, "width": CANVAS_SIZE, "height": CANVAS_SIZE},
            "asset": "assets/pivot_fiducials.png",
            "confidence": 1.0,
        },
        {
            "id": "pivot_hand",
            "type": "ANALOG_HAND",
            "role": "HOUR",
            "dynamic": True,
            "bbox": {"x": 209, "y": 84, "width": 20, "height": 180},
            "asset": "assets/pivot_hand.png",
            "observedAngleDeg": 0,
            "length": 127,
            "thickness": 3,
            "pivotX": 0.5,
            "pivotY": 0.75,
            "zIndex": 10,
            "confidence": 1.0,
        },
    ]
    scene = _diagnostic_scene(source_scene, elements)
    scene["clock"] = {"type": "ANALOG", "centerX": 219, "centerY": 219, "confidence": 1.0}
    metadata = {"targetLogicalCenter": [219, 219], "fiducials": [list(point) for point in fiducial_points], "assetPivot": [10, 135]}
    return _compile_diagnostic("pivot", scene, source, output_root), metadata


def _logical_bbox(runtime_bbox: tuple[int, int, int, int], transform: dict[str, Any]) -> tuple[float, float, float, float]:
    top_left = _inverse_map_point(transform, runtime_bbox[0], runtime_bbox[1])
    bottom_right = _inverse_map_point(transform, runtime_bbox[2], runtime_bbox[3])
    return top_left[0], top_left[1], bottom_right[0], bottom_right[1]


def _center(box: tuple[float, float, float, float]) -> tuple[float, float]:
    return (box[0] + box[2]) / 2, (box[1] + box[3]) / 2


def _measure_bitmap(
    deterministic_path: Path,
    runtime_path: Path,
    transform: dict[str, Any],
    metadata: dict[str, Any],
) -> dict[str, Any]:
    deterministic_image = Image.open(deterministic_path).convert("RGB")
    runtime_image = Image.open(runtime_path).convert("RGB")
    variants: dict[str, Any] = {}
    for name, item in metadata.items():
        container = item["container"]
        logical_roi = (container["x"], container["y"], container["x"] + container["width"], container["y"] + container["height"])
        mapped = (_map_point(transform, logical_roi[0], logical_roi[1]), _map_point(transform, logical_roi[2], logical_roi[3]))
        runtime_roi = (
            math.floor(min(point[0] for point in mapped)) - 3,
            math.floor(min(point[1] for point in mapped)) - 3,
            math.ceil(max(point[0] for point in mapped)) + 3,
            math.ceil(max(point[1] for point in mapped)) + 3,
        )
        deterministic_bbox = _foreground_bbox(deterministic_image, logical_roi)
        runtime_bbox = _foreground_bbox(runtime_image, runtime_roi)
        runtime_logical = _logical_bbox(runtime_bbox, transform) if runtime_bbox else None
        deterministic_center = _center(deterministic_bbox) if deterministic_bbox else None
        runtime_center = _center(runtime_logical) if runtime_logical else None
        variants[name] = {
            **item,
            "deterministicForegroundBboxLogical": list(deterministic_bbox) if deterministic_bbox else None,
            "runtimeForegroundBbox": list(runtime_bbox) if runtime_bbox else None,
            "runtimeForegroundBboxLogical": [round(value, 4) for value in runtime_logical] if runtime_logical else None,
            "runtimeVsDeterministicCenterDifferencePx": {
                "x": round(runtime_center[0] - deterministic_center[0], 4),
                "y": round(runtime_center[1] - deterministic_center[1], 4),
            } if runtime_center and deterministic_center else None,
            "runtimeVsDeterministicTopDifferencePx": round(runtime_logical[1] - deterministic_bbox[1], 4) if runtime_logical and deterministic_bbox else None,
            "runtimeVsDeterministicBottomDifferencePx": round(runtime_logical[3] - deterministic_bbox[3], 4) if runtime_logical and deterministic_bbox else None,
        }
    deterministic_image.close()
    runtime_image.close()
    part_delta = variants["part_image"]["runtimeVsDeterministicCenterDifferencePx"]
    bitmap_delta = variants["native_h48"]["runtimeVsDeterministicCenterDifferencePx"]
    container_names = ("native_h29", "native_h48", "native_h72")
    container_deltas = [variants[name]["runtimeVsDeterministicCenterDifferencePx"]["y"] for name in container_names]
    runtime_top_offsets = [
        variants[name]["runtimeForegroundBboxLogical"][1] - variants[name]["container"]["y"]
        for name in container_names
    ]
    container_heights = [variants[name]["container"]["height"] for name in container_names]
    mean_height = sum(container_heights) / len(container_heights)
    mean_offset = sum(runtime_top_offsets) / len(runtime_top_offsets)
    vertical_position_slope = sum(
        (height - mean_height) * (offset - mean_offset)
        for height, offset in zip(container_heights, runtime_top_offsets)
    ) / sum((height - mean_height) ** 2 for height in container_heights)
    start_center = _center(tuple(variants["native_start"]["runtimeForegroundBboxLogical"]))[0]
    center_center = _center(tuple(variants["native_h48"]["runtimeForegroundBboxLogical"]))[0]
    end_center = _center(tuple(variants["native_end"]["runtimeForegroundBboxLogical"]))[0]
    native_size = variants["native_h48"]["runtimeForegroundBboxLogical"]
    half_size = variants["half_metrics"]["runtimeForegroundBboxLogical"]
    square_size = variants["square_metrics"]["runtimeForegroundBboxLogical"]
    native_width = native_size[2] - native_size[0]
    half_width = half_size[2] - half_size[0]
    square_width = square_size[2] - square_size[0]
    return {
        "status": "measured",
        "variants": variants,
        "findings": {
            "partImageVerticalDifferencePx": part_delta["y"],
            "bitmapFontVerticalDifferencePx": bitmap_delta["y"],
            "bitmapMinusPartImageVerticalDifferencePx": round(bitmap_delta["y"] - part_delta["y"], 4),
            "containerHeightVerticalDifferenceRangePx": round(max(container_deltas) - min(container_deltas), 4),
            "runtimeGlyphTopOffsetMedianPx": round(median(runtime_top_offsets), 4),
            "runtimeGlyphTopOffsetRangePx": round(max(runtime_top_offsets) - min(runtime_top_offsets), 4),
            "runtimeTopOffsetVsContainerHeightSlope": round(vertical_position_slope, 6),
            "runtimeVerticalAnchor": "CENTER" if abs(vertical_position_slope - 0.5) <= 0.1 else "UNRESOLVED",
            "containerHeightAffectsVerticalPosition": max(runtime_top_offsets) - min(runtime_top_offsets) > 1.0,
            "sameAspectMetricScaleInvariant": abs(native_width - half_width) <= 1.0,
            "squareMetricsIncreaseApparentWidth": square_width > native_width + 2.0,
            "horizontalAlignmentHonored": start_center < center_center < end_center,
            "rootCause": "deterministic renderer centered the native bitmap before applying BitmapFont size and Character metrics",
            "deterministicRendererCalibrationApplied": True,
            "productionYOffsetApplied": False,
        },
    }


def _profile(image: Image.Image, bbox: tuple[int, int, int, int], orientation: str) -> list[float]:
    gray = image.convert("L")
    left, top, right, bottom = bbox
    if orientation == "VERTICAL":
        margin = max(1, (bottom - top) // 8)
        top += margin
        bottom -= margin
        return [sum(gray.getpixel((x, y)) for y in range(top, bottom)) / max(1, bottom - top) for x in range(left, right)]
    margin = max(1, (right - left) // 8)
    left += margin
    right -= margin
    return [sum(gray.getpixel((x, y)) for x in range(left, right)) / max(1, right - left) for y in range(top, bottom)]


def _profile_metrics(values: list[float], scale: float = 1.0) -> dict[str, Any]:
    peak = max(values, default=0.0)
    thresholds = (16, 32, 64, 128, 192)
    return {
        "peak": round(peak, 4),
        "energyEquivalentWidthPx": round(sum(values) / peak / scale, 4) if peak else None,
        "fwhmPx": round(sum(value >= peak / 2 for value in values) / scale, 4) if peak else None,
        "supportWidthByThresholdPx": {
            str(threshold): round(sum(value >= threshold for value in values) / scale, 4)
            for threshold in thresholds
        },
    }


def _measure_resampling(
    deterministic_path: Path,
    runtime_path: Path,
    transform: dict[str, Any],
    metadata: dict[str, Any],
) -> dict[str, Any]:
    deterministic_image = Image.open(deterministic_path).convert("RGB")
    runtime_image = Image.open(runtime_path).convert("RGB")
    records = []
    for name, item in metadata.items():
        bbox = item["bbox"]
        logical_roi = (bbox["x"], bbox["y"], bbox["x"] + bbox["width"], bbox["y"] + bbox["height"])
        corners = (_map_point(transform, logical_roi[0], logical_roi[1]), _map_point(transform, logical_roi[2], logical_roi[3]))
        runtime_roi = (
            max(0, math.floor(min(point[0] for point in corners))),
            max(0, math.floor(min(point[1] for point in corners))),
            min(runtime_image.width, math.ceil(max(point[0] for point in corners))),
            min(runtime_image.height, math.ceil(max(point[1] for point in corners))),
        )
        axis_scale = transform["scaleX"] if item["orientation"] == "VERTICAL" else transform["scaleY"]
        deterministic_metrics = _profile_metrics(_profile(deterministic_image, logical_roi, item["orientation"]))
        runtime_metrics = _profile_metrics(_profile(runtime_image, runtime_roi, item["orientation"]), axis_scale)
        records.append({
            "id": name,
            **item,
            "deterministic": deterministic_metrics,
            "runtimeNormalizedLogical": runtime_metrics,
            "runtimeEnergyWidthErrorPx": round(runtime_metrics["energyEquivalentWidthPx"] - item["knownWidthPx"], 4),
            "runtimeFwhmErrorPx": round(runtime_metrics["fwhmPx"] - item["knownWidthPx"], 4),
        })
    deterministic_image.close()
    runtime_image.close()
    energy_errors = [abs(record["runtimeEnergyWidthErrorPx"]) for record in records]
    fwhm_errors = [record["runtimeFwhmErrorPx"] for record in records]
    energy_preserved = median(energy_errors) <= 0.75
    width_scale_by_orientation = {}
    for orientation in ("VERTICAL", "HORIZONTAL"):
        orientation_records = [record for record in records if record["orientation"] == orientation]
        numerator = sum(record["knownWidthPx"] * record["runtimeNormalizedLogical"]["energyEquivalentWidthPx"] for record in orientation_records)
        denominator = sum(record["knownWidthPx"] ** 2 for record in orientation_records)
        width_scale_by_orientation[orientation.lower()] = round(numerator / denominator, 6)
    return {
        "status": "measured",
        "records": records,
        "findings": {
            "medianAbsoluteEnergyWidthErrorPx": round(median(energy_errors), 4),
            "medianFwhmErrorPx": round(median(fwhm_errors), 4),
            "energyPreservedWithin0_75Px": energy_preserved,
            "classification": "threshold_or_edge_filtering_artifact" if energy_preserved else "runtime_resampling_changes_apparent_width",
            "hourMinuteWidthLossExplainedBySolidLineResampling": not energy_preserved,
            "energyWidthScaleByOrientation": width_scale_by_orientation,
            "runtimeWidthCorrectionRequired": not energy_preserved,
            "filterKernel": "unmeasured",
        },
    }


def _colored_centroid(image: Image.Image, predicate: Callable[[tuple[int, int, int]], bool]) -> tuple[tuple[float, float] | None, int]:
    points = []
    rgb = image.convert("RGB")
    for y in range(rgb.height):
        for x in range(rgb.width):
            if predicate(rgb.getpixel((x, y))):
                points.append((x, y))
    if not points:
        return None, 0
    return (sum(x for x, _ in points) / len(points), sum(y for _, y in points) / len(points)), len(points)


def _measure_pivot(runtime_path: Path, transform: dict[str, Any]) -> dict[str, Any]:
    image = Image.open(runtime_path).convert("RGB")
    pivot, pivot_pixels = _colored_centroid(image, lambda color: color[0] > 150 and color[1] < 100 and color[2] < 100)
    fiducial_points = []
    for logical_x, logical_y in ((207, 207), (231, 207), (207, 231), (231, 231)):
        runtime_x, runtime_y = _map_point(transform, logical_x, logical_y)
        radius = 8
        crop = image.crop((round(runtime_x) - radius, round(runtime_y) - radius, round(runtime_x) + radius + 1, round(runtime_y) + radius + 1))
        local, count = _colored_centroid(crop, lambda color: color[0] < 100 and color[1] > 140 and color[2] > 140)
        if local and count >= 3:
            fiducial_points.append((local[0] + round(runtime_x) - radius, local[1] + round(runtime_y) - radius))
    image.close()
    if pivot is None or len(fiducial_points) != 4:
        return {"status": "unmeasured", "reason": "pivot marker or all four center fiducials were not independently detected"}
    target = (sum(x for x, _ in fiducial_points) / 4, sum(y for _, y in fiducial_points) / 4)
    delta_runtime = (pivot[0] - target[0], pivot[1] - target[1])
    delta_logical = (delta_runtime[0] / transform["scaleX"], delta_runtime[1] / transform["scaleY"])
    return {
        "status": "measured",
        "method": "red hand-asset pivot marker versus centroid of four cyan static center fiducials",
        "runtimePivot": [round(value, 4) for value in pivot],
        "runtimeTargetCenter": [round(value, 4) for value in target],
        "pivotErrorRuntimePx": {"x": round(delta_runtime[0], 4), "y": round(delta_runtime[1], 4), "distance": round(math.hypot(*delta_runtime), 4)},
        "pivotErrorLogicalPx": {"x": round(delta_logical[0], 4), "y": round(delta_logical[1], 4), "distance": round(math.hypot(*delta_logical), 4)},
        "pivotMarkerPixelCount": pivot_pixels,
        "fiducialCount": len(fiducial_points),
        "verified": True,
    }


def _apply_affine_preview(source_path: Path, destination: Path, transform: dict[str, Any], runtime_size: tuple[int, int]) -> None:
    matrix = transform["matrix"]
    determinant = matrix["a"] * matrix["d"] - matrix["b"] * matrix["c"]
    inverse = (
        matrix["d"] / determinant,
        -matrix["b"] / determinant,
        (matrix["b"] * matrix["ty"] - matrix["d"] * matrix["tx"]) / determinant,
        -matrix["c"] / determinant,
        matrix["a"] / determinant,
        (matrix["c"] * matrix["tx"] - matrix["a"] * matrix["ty"]) / determinant,
    )
    with Image.open(source_path) as image:
        predicted = image.convert("RGBA").transform(runtime_size, Image.Transform.AFFINE, inverse, resample=Image.Resampling.BICUBIC)
    destination.parent.mkdir(parents=True, exist_ok=True)
    predicted.save(destination)


def _write_review_atlas(output_root: Path, captures: dict[str, Any]) -> Path | None:
    names = ("bitmap_font", "resampling", "pivot")
    if not all(captures.get(name, {}).get("captureOk") for name in names):
        return None
    cell_size = 454
    label_height = 24
    atlas = Image.new("RGB", (cell_size * 2, (cell_size + label_height) * len(names)), (16, 16, 16))
    draw = ImageDraw.Draw(atlas)
    for row, name in enumerate(names):
        y = row * (cell_size + label_height)
        deterministic_path = output_root / "deterministic" / f"{name.replace('_', '-')}.png"
        with Image.open(deterministic_path) as deterministic_source:
            deterministic = deterministic_source.convert("RGB")
        with Image.open(captures[name]["path"]) as runtime_source:
            runtime = runtime_source.convert("RGB")
        atlas.paste(deterministic, ((cell_size - deterministic.width) // 2, y))
        atlas.paste(runtime, (cell_size + (cell_size - runtime.width) // 2, y))
        draw.text((8, y + cell_size + 4), f"{name}: deterministic", fill="white")
        draw.text((cell_size + 8, y + cell_size + 4), f"{name}: runtime", fill="white")
    destination = output_root / "runtime-rendering-calibration-atlas.png"
    atlas.save(destination)
    return destination


def run_runtime_rendering_calibration(
    scene_path: Path,
    manual_scene_path: Path,
    output_root: Path,
    build: bool = False,
    capture: bool = False,
    adb: Path | None = None,
    serial: str | None = None,
) -> dict[str, Any]:
    source_scene = json.loads(scene_path.read_text(encoding="utf-8"))
    manual_scene = json.loads(manual_scene_path.read_text(encoding="utf-8"))
    output_root.mkdir(parents=True, exist_ok=True)

    geometry_source, geometry_metadata = _geometry_source(output_root)
    geometry_element = {"id": "geometry_fiducials", "type": "STATIC_IMAGE", "dynamic": False, "bbox": {"x": 0, "y": 0, "width": CANVAS_SIZE, "height": CANVAS_SIZE}, "asset": "assets/geometry_fiducials.png", "confidence": 1.0}
    projects: dict[str, Any] = {
        "geometry": _compile_diagnostic("geometry", _diagnostic_scene(source_scene, [geometry_element]), geometry_source, output_root),
    }
    projects["bitmap_font"], bitmap_metadata = _bitmap_diagnostic(source_scene, manual_scene, manual_scene_path.parent, output_root)
    projects["resampling"], resampling_metadata = _resampling_diagnostic(source_scene, output_root)
    projects["pivot"], pivot_metadata = _pivot_diagnostic(source_scene, output_root)
    projects["production_off"] = _compile_diagnostic("production-off", copy.deepcopy(source_scene), scene_path.parent, output_root)
    projects["production_manual"] = _compile_diagnostic("production-manual", copy.deepcopy(manual_scene), manual_scene_path.parent, output_root)

    deterministic_root = output_root / "deterministic"
    deterministic_root.mkdir(parents=True, exist_ok=True)
    for name in ("bitmap_font", "resampling", "pivot", "production_off"):
        fixed_time = "00:00:00" if name == "pivot" else DIAGNOSTIC_TIME
        render_wff_xml(Path(projects[name]["xml"]), deterministic_root / f"{name.replace('_', '-')}.png", fixed_time=fixed_time, fixed_date="2024-08-08")

    build_results: dict[str, Any] = {}
    if build or capture:
        for name, project in projects.items():
            build_results[name] = _build_project(Path(project["project"]))

    runtime = detect_runtime(adb)
    failed_builds = [name for name, result in build_results.items() if not result.get("success")]
    captures: dict[str, Any] = {}
    geometry: dict[str, Any] = {"status": "not_captured", "fiducials": geometry_metadata}
    bitmap: dict[str, Any] = {"status": "not_captured"}
    resampling: dict[str, Any] = {"status": "not_captured"}
    pivot: dict[str, Any] = {"status": "unmeasured", "reason": "diagnostic not captured"}
    if capture and runtime["status"] == "runtime_available" and not failed_builds:
        selected_serial = serial or runtime["selectedDevice"]
        adb_executable = runtime["adb"]
        for name in ("geometry", "bitmap_font", "resampling", "pivot"):
            destination = output_root / "runtime" / f"{name.replace('_', '-')}.png"
            captures[name] = capture_runtime_image(
                adb_executable,
                selected_serial,
                Path(build_results[name]["apk"]),
                destination,
                time_value="00:00:00" if name == "pivot" else DIAGNOSTIC_TIME,
                day=8,
            )
        if captures["geometry"]["captureOk"]:
            geometry = _fit_geometry(Path(captures["geometry"]["path"]))
            geometry["fiducials"] = geometry_metadata
        if geometry.get("status") == "measured":
            if captures["bitmap_font"]["captureOk"]:
                bitmap = _measure_bitmap(deterministic_root / "bitmap-font.png", Path(captures["bitmap_font"]["path"]), geometry, bitmap_metadata)
            if captures["resampling"]["captureOk"]:
                resampling = _measure_resampling(deterministic_root / "resampling.png", Path(captures["resampling"]["path"]), geometry, resampling_metadata)
            if captures["pivot"]["captureOk"]:
                pivot = _measure_pivot(Path(captures["pivot"]["path"]), geometry)

    official_validation = {name: {"passed": "PASSED" in project["officialValidation"], "output": project["officialValidation"]} for name, project in projects.items()}
    production_builds_pass = bool(build_results) and all(build_results[name].get("success") for name in ("production_off", "production_manual"))
    verified_values: dict[str, Any] = {}
    if geometry.get("status") == "measured":
        verified_values["logicalToRuntime"] = {
            "scaleX": geometry["scaleX"],
            "scaleY": geometry["scaleY"],
            "centerOffset": geometry["centerOffset"],
            "rotationDeg": geometry["rotationDeg"],
            "fitResidualRmsPx": geometry["fitResidualRmsPx"],
            "matrix": geometry["matrix"],
        }
    if bitmap.get("status") == "measured":
        bitmap_findings = bitmap["findings"]
        verified_values["bitmapFont"] = {
            "runtimeVerticalAnchor": bitmap_findings["runtimeVerticalAnchor"],
            "runtimeTopOffsetVsContainerHeightSlope": bitmap_findings["runtimeTopOffsetVsContainerHeightSlope"],
            "sameAspectMetricScaleInvariant": bitmap_findings["sameAspectMetricScaleInvariant"],
            "squareMetricsIncreaseApparentWidth": bitmap_findings["squareMetricsIncreaseApparentWidth"],
            "horizontalAlignmentHonored": bitmap_findings["horizontalAlignmentHonored"],
            "postCalibrationVerticalResidualPx": bitmap_findings["bitmapFontVerticalDifferencePx"],
            "bitmapMinusPartImageVerticalResidualPx": bitmap_findings["bitmapMinusPartImageVerticalDifferencePx"],
            "productionYOffsetApplied": False,
        }
    if resampling.get("status") == "measured":
        resampling_findings = resampling["findings"]
        verified_values["imageResampling"] = {
            "medianAbsoluteEnergyWidthErrorPx": resampling_findings["medianAbsoluteEnergyWidthErrorPx"],
            "medianFwhmErrorPx": resampling_findings["medianFwhmErrorPx"],
            "energyWidthScaleByOrientation": resampling_findings["energyWidthScaleByOrientation"],
            "runtimeWidthCorrectionRequired": resampling_findings["runtimeWidthCorrectionRequired"],
            "hourMinuteWidthLossExplainedBySolidLineResampling": resampling_findings["hourMinuteWidthLossExplainedBySolidLineResampling"],
        }
    if pivot.get("status") == "measured" and pivot.get("verified"):
        verified_values["analogPivot"] = pivot["pivotErrorLogicalPx"]

    profile = {
        "schemaVersion": PROFILE_SCHEMA_VERSION,
        "milestone": "A2.5c.2 Runtime Rendering Calibration",
        "baseline": "1ee56a8",
        "baselineTag": "baseline/a25c1-measurement-correctness",
        "runtime": {"serial": serial or runtime.get("selectedDevice"), "status": runtime.get("status")},
        "verifiedCalibrationValues": verified_values,
        "unverifiedValuesOmitted": True,
        "productionSceneGeometryModified": False,
    }
    profile_path = output_root / "device-runtime-calibration.json"
    _write_json(profile_path, profile)

    predicted_preview = None
    if "logicalToRuntime" in verified_values and captures.get("geometry", {}).get("captureOk"):
        with Image.open(captures["geometry"]["path"]) as runtime_geometry:
            runtime_size = runtime_geometry.size
        predicted_preview = output_root / "predicted-runtime-preview-geometry-only.png"
        _apply_affine_preview(deterministic_root / "production-off.png", predicted_preview, geometry, runtime_size)
    review_atlas = _write_review_atlas(output_root, captures)

    if failed_builds:
        status = "blocked_by_build"
    elif capture and runtime["status"] != "runtime_available":
        status = "blocked_by_runtime_environment"
    elif capture:
        status = "runtime_measured"
    else:
        status = "implemented_not_captured"
    calibration_complete = all(result.get("status") == "measured" for result in (bitmap, resampling, pivot))
    report = {
        "milestone": "A2.5c.2 Runtime Rendering Calibration",
        "baseline": "1ee56a8",
        "baselineTag": "baseline/a25c1-measurement-correctness",
        "status": status,
        "officialValidation": official_validation,
        "bitmapFontFindings": bitmap,
        "resamplingFindings": resampling,
        "pivotStatus": pivot,
        "verifiedCalibrationValues": verified_values,
        "deviceRuntimeCalibration": str(profile_path),
        "predictedRuntimePreview": str(predicted_preview) if predicted_preview else None,
        "predictedRuntimePreviewLimitations": ["global affine transform only", "runtime filter kernel remains unmeasured"],
        "reviewAtlas": str(review_atlas) if review_atlas else None,
        "productionFixesRequired": [] if calibration_complete else ["undetermined until all runtime diagnostics are measured"],
        "previewRendererFixesApplied": [
            "BitmapFont glyphs scale from BitmapFont size and Character metrics before vertical centering",
            "START/END alignment is honored by compiler and deterministic renderer",
        ],
        "productionSceneGeometryModified": False,
        "regressions": [] if production_builds_pass else (["production regression builds were not run"] if not build_results else ["production regression build failed"]),
        "projects": projects,
        "builds": build_results,
        "captures": captures,
        "runtimeEnvironment": runtime,
        "diagnosticMetadata": {"bitmapFont": bitmap_metadata, "resampling": resampling_metadata, "pivot": pivot_metadata},
    }
    report_path = output_root / "runtime-rendering-calibration-report.json"
    _write_json(report_path, report)
    report["report"] = str(report_path)
    return report
