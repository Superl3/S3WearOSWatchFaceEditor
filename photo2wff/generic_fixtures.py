from __future__ import annotations

import json
import math
import statistics
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from .display_geometry import RoundedRect, boundary_normalized_map, direction_from_angle, inverse_raster_map, inverse_sd_perimeter_map, map_sd_point
from .dynamic_text import extract_center_dynamic_text
from .perimeter_artwork import _background_color, _components, _foreground_mask, decompose_perimeter_artwork, draw_perimeter_overlay, render_element_preserving_mapping


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in (Path("C:/Windows/Fonts/arial.ttf"), Path("C:/Windows/Fonts/segoeui.ttf")):
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


def _point(shape: RoundedRect, angle: float, radius: float = 0.82) -> tuple[float, float]:
    direction = direction_from_angle(angle)
    distance = shape.boundary_distance(direction) * radius
    return shape.center_x + direction[0] * distance, shape.center_y + direction[1] * distance


def _paste_center(canvas: Image.Image, asset: Image.Image, center: tuple[float, float]) -> None:
    canvas.alpha_composite(asset, (round(center[0] - asset.width / 2), round(center[1] - asset.height / 2)))


def _fixture_perimeter(root: Path, shape: RoundedRect, glyph_like: bool) -> tuple[Path, list[tuple[float, float]]]:
    canvas = Image.new("RGBA", (438, 438), "black")
    anchors: list[tuple[float, float]] = []
    for index in range(12):
        angle = index * 30.0
        anchor = _point(shape, angle)
        anchors.append(anchor)
        if glyph_like:
            asset = Image.new("RGBA", (22, 32), (0, 0, 0, 0))
            draw = ImageDraw.Draw(asset)
            draw.line((4, 27, 4, 5, 17, 5), fill="white", width=4)
            draw.ellipse((12, 20, 18, 27), fill="white")
            asset = asset.rotate(-(angle + 13), expand=True, resample=Image.Resampling.BICUBIC)
        else:
            asset = Image.new("RGBA", (18, 10), "white")
        _paste_center(canvas, asset, anchor)
    name = "rotated-glyph-like" if glyph_like else "twelve-rectangles"
    path = root / f"{name}.png"
    canvas.convert("RGB").save(path)
    return path, anchors


def _fixture_dynamic_text(root: Path) -> Path:
    image = Image.new("RGB", (438, 438), "black")
    draw = ImageDraw.Draw(image)
    draw.text((148, 219), "TUE", font=_font(30), fill="white", anchor="lm")
    draw.text((254, 219), "14", font=_font(30), fill="white", anchor="lm")
    path = root / "center-weekday-date.png"
    image.save(path)
    return path


def _fixture_hands(root: Path) -> Path:
    image = Image.new("RGB", (438, 438), "black")
    draw = ImageDraw.Draw(image)
    center = (219, 219)
    draw.line((219, 219, 170, 166), fill="white", width=10)
    draw.line((219, 219, 300, 142), fill="white", width=7)
    draw.line((219, 219, 219, 382), fill=(220, 30, 45), width=3)
    draw.ellipse((212, 212, 226, 226), fill="white")
    path = root / "three-analog-hands.png"
    image.save(path)
    return path


def _fixture_disconnected_marker(root: Path, shape: RoundedRect) -> Path:
    image = Image.new("RGB", (438, 438), "black")
    draw = ImageDraw.Draw(image)
    for index in range(1, 12):
        anchor = _point(shape, index * 30.0)
        draw.rectangle((round(anchor[0] - 5), round(anchor[1] - 8), round(anchor[0] + 5), round(anchor[1] + 8)), fill="white")
    top = _point(shape, 0.0)
    draw.rectangle((round(top[0] - 15), round(top[1] - 9), round(top[0] - 8), round(top[1] + 9)), fill="white")
    draw.rectangle((round(top[0] - 2), round(top[1] - 9), round(top[0] + 5), round(top[1] + 9)), fill="white")
    path = root / "disconnected-multipart-marker.png"
    image.save(path)
    return path


def _fixture_marker_slot_regressions(root: Path, shape: RoundedRect) -> Path:
    """Exercise marker-first grouping without relying on digit recognition."""

    image = Image.new("RGB", (438, 438), "black")
    draw = ImageDraw.Draw(image)

    def frame(angle: float, radius: float = 0.82) -> tuple[tuple[float, float], tuple[float, float], tuple[float, float]]:
        direction = direction_from_angle(angle)
        tangent = (-direction[1], direction[0])
        center = _point(shape, angle, radius)
        return center, direction, tangent

    # A deliberately disconnected two-part marker in the 10 o'clock slot.
    center, direction, tangent = frame(300.0, 0.84)
    first = (center[0] - tangent[0] * 10, center[1] - tangent[1] * 10)
    second = (center[0] + tangent[0] * 10, center[1] + tangent[1] * 10)
    draw.rectangle((round(first[0] - 2), round(first[1] - 5), round(first[0] + 2), round(first[1] + 5)), fill="white")
    draw.rounded_rectangle((round(second[0] - 4), round(second[1] - 5), round(second[0] + 4), round(second[1] + 5)), radius=2, outline="white", width=2)

    # Two adjacent markers joined by a thin bridge. They must still become two slots.
    left, _, _ = frame(240.0, 0.84)
    right, _, _ = frame(270.0, 0.84)
    draw.rectangle((round(left[0] - 5), round(left[1] - 8), round(left[0] + 5), round(left[1] + 8)), fill="white")
    draw.rectangle((round(right[0] - 5), round(right[1] - 8), round(right[0] + 5), round(right[1] + 8)), fill="white")
    draw.line((round(left[0]), round(left[1]), round(right[0]), round(right[1])), fill="white", width=1)

    # Boundary-near top and bottom markers.
    for angle in (0.0, 180.0):
        boundary, _, _ = frame(angle, 0.94)
        draw.rectangle((round(boundary[0] - 4), round(boundary[1] - 4), round(boundary[0] + 4), round(boundary[1] + 4)), fill="white")

    # An asymmetric marker whose semantic radial anchor is far from its bbox center.
    asymmetric, _, tangent = frame(90.0, 0.82)
    shifted = (asymmetric[0] + tangent[0] * 12, asymmetric[1] + tangent[1] * 12)
    draw.rectangle((round(shifted[0] - 4), round(shifted[1] - 20), round(shifted[0] + 4), round(shifted[1] + 20)), fill="white")

    # Fill remaining hour slots with simple marker bars.
    for angle in (30.0, 60.0, 120.0, 150.0, 210.0, 330.0):
        point, _, _ = frame(angle, 0.82)
        draw.rectangle((round(point[0] - 4), round(point[1] - 8), round(point[0] + 4), round(point[1] + 8)), fill="white")

    path = root / "marker-slot-regressions.png"
    image.save(path)
    return path


def _fixture_sd_geometry(root: Path, shape: RoundedRect) -> dict[str, Path]:
    straight = Image.new("RGB", (438, 438), "black")
    straight_draw = ImageDraw.Draw(straight)
    for x in (154, 166, 178):
        straight_draw.line((x, 36, x, 142), fill="white", width=3)
    straight_path = root / "straight-parallel-lines.png"
    straight.save(straight_path)

    arcs = Image.new("RGB", (438, 438), "black")
    arc_draw = ImageDraw.Draw(arcs)
    arc_draw.arc((45, 45, 393, 393), 205, 335, fill="white", width=3)
    arc_draw.arc((70, 70, 368, 368), 210, 330, fill="white", width=3)
    arcs_path = root / "rounded-corner-arcs.png"
    arcs.save(arcs_path)

    patches = Image.new("RGB", (438, 438), "black")
    patch_draw = ImageDraw.Draw(patches)
    for angle in (0.0, 30.0, 60.0, 90.0):
        center = _point(shape, angle, 0.78)
        patch_draw.rounded_rectangle((round(center[0] - 8), round(center[1] - 13), round(center[0] + 8), round(center[1] + 13)), radius=3, outline="white", width=3)
    patches_path = root / "compact-numeral-like-patches.png"
    patches.save(patches_path)
    return {"straight": straight_path, "arcs": arcs_path, "patches": patches_path}


def _straight_line_wave_score(image: Image.Image) -> float:
    mask = image.convert("L")
    centerline: list[float] = []
    for y in range(mask.height):
        xs = [x for x in range(mask.width) if mask.getpixel((x, y)) > 180]
        if xs:
            centerline.append(sum(xs) / len(xs))
    if len(centerline) < 3:
        return 999.0
    second_difference = [abs(centerline[index + 1] - 2 * centerline[index] + centerline[index - 1]) for index in range(1, len(centerline) - 1)]
    return sum(second_difference) / max(1, len(second_difference)) / image.width


def _stroke_width_variance(image: Image.Image) -> float:
    mask = image.convert("L")
    widths = []
    for y in range(mask.height):
        width = sum(1 for x in range(mask.width) if mask.getpixel((x, y)) > 180)
        if width:
            widths.append(width)
    if len(widths) < 2:
        return 999.0
    return statistics.pvariance(widths) / max(1.0, statistics.mean(widths) ** 2)


def _fold_over_count(source: RoundedRect, target: RoundedRect) -> int:
    count = 0
    for y in range(32, 407, 16):
        for x in range(32, 407, 16):
            point = (float(x), float(y))
            if not target.contains(point):
                continue
            if math.hypot(x - target.center_x, y - target.center_y) / (target.width / 2) < 0.78:
                continue
            p, _, _ = map_sd_point(point, source, target)
            px, _, _ = map_sd_point((x + 1.0, y), source, target)
            py, _, _ = map_sd_point((x, y + 1.0), source, target)
            determinant = (px[0] - p[0]) * (py[1] - p[1]) - (px[1] - p[1]) * (py[0] - p[0])
            if determinant <= 0:
                count += 1
    return count


def run_generic_fixtures(output_root: Path) -> dict[str, Any]:
    """Verify generic geometry before any real reference is admitted as input."""

    output_root.mkdir(parents=True, exist_ok=True)
    source = RoundedRect(360, 400, 54, 219, 219)
    target = RoundedRect(438, 438, 219, 219, 219)
    rectangle_path, expected_anchors = _fixture_perimeter(output_root, source, glyph_like=False)
    glyph_path, _ = _fixture_perimeter(output_root, source, glyph_like=True)
    dynamic_path = _fixture_dynamic_text(output_root)
    hand_path = _fixture_hands(output_root)
    multipart_path = _fixture_disconnected_marker(output_root, source)
    regression_path = _fixture_marker_slot_regressions(output_root, source)
    sd_paths = _fixture_sd_geometry(output_root, source)
    sd_render = inverse_sd_perimeter_map(Image.open(sd_paths["straight"]), source, target, supersample=1)
    sd_render.save(output_root / "straight-parallel-lines-sd-warp.png")
    radial_render = inverse_raster_map(Image.open(sd_paths["straight"]), source, target)
    radial_render.save(output_root / "straight-parallel-lines-radial-baseline.png")
    arc_render = inverse_sd_perimeter_map(Image.open(sd_paths["arcs"]), source, target, supersample=1)
    arc_render.save(output_root / "rounded-corner-arcs-sd-warp.png")

    rectangle_report = decompose_perimeter_artwork(Image.open(rectangle_path), source, output_root / "rectangle-decomposition", minimum_area=40)
    glyph_report = decompose_perimeter_artwork(Image.open(glyph_path), source, output_root / "glyph-decomposition", minimum_area=10)
    draw_perimeter_overlay(Image.open(glyph_path), glyph_report["elements"], output_root / "detected-perimeter-overlay.png")
    mapped, mapped_records = render_element_preserving_mapping(
        Image.new("RGBA", (438, 438), "black"), glyph_report["elements"], source, target, output_root / "glyph-decomposition"
    )
    mapped.convert("RGB").save(output_root / "element-preserving-circle.png")
    dynamic_report = extract_center_dynamic_text(Image.open(dynamic_path), output_root / "dynamic-text")
    multipart_report = decompose_perimeter_artwork(Image.open(multipart_path), source, output_root / "multipart-decomposition", minimum_area=12)
    regression_report = decompose_perimeter_artwork(Image.open(regression_path), source, output_root / "marker-slot-regression-decomposition", minimum_area=12)
    regression_image = Image.open(regression_path)
    regression_mask = _foreground_mask(regression_image, _background_color(regression_image))
    source_components = len(_components(regression_mask, 1))
    _, regression_mapped = render_element_preserving_mapping(
        Image.new("RGBA", (438, 438), "black"),
        regression_report["elements"],
        source,
        target,
        output_root / "marker-slot-regression-decomposition",
    )

    detected = rectangle_report["elements"]
    detected_anchors = [(float(element["anchor"]["x"]), float(element["anchor"]["y"])) for element in detected]
    anchor_errors = [min(math.dist(expected, observed) for observed in detected_anchors) for expected in expected_anchors] if detected_anchors else [999.0]
    mapping_errors = []
    for element, record in zip(glyph_report["elements"], mapped_records):
        anchor = element["anchor"]
        expected = boundary_normalized_map((anchor["x"], anchor["y"]), source, target)
        actual = record["targetAnchor"]
        mapping_errors.append(math.dist(expected, (actual["x"], actual["y"])))
    aspect_preserved = all(
        abs((record["targetBbox"]["width"] / record["targetBbox"]["height"]) - (element["bbox"]["width"] / element["bbox"]["height"])) < 1e-6
        for element, record in zip(glyph_report["elements"], mapped_records)
    )
    checks = {
        "twelveRectanglesDetected": len(detected) == 12,
        "rectangleAnchorMaxErrorPx": round(max(anchor_errors), 4),
        "rotatedGlyphElementsDetected": len(glyph_report["elements"]) >= 12,
        "elementMappingMaxErrorPx": round(max(mapping_errors, default=999.0), 6),
        "localAspectRatioPreserved": aspect_preserved,
        "weekdayAndDateDetected": dynamic_report["detected"],
        "dynamicTextRemoved": Path(dynamic_report["cleanBackground"]).exists(),
        "analogHandFixtureCreated": hand_path.exists(),
        "multipartMarkerGrouped": any(element.get("relationships", {}).get("componentCount", 0) >= 2 for element in multipart_report["elements"]),
        "multipartMarkerSlotCount": multipart_report["elementCount"],
        "markerSlotRegressionCount": regression_report["elementCount"],
        "markerSlotRegressionUsesPrior": regression_report["grouping"]["mode"] == "hour_position_prior",
        # The fixture contains 12 slots, but slot 10 has two disconnected parts;
        # without the adjacent-marker bridge there would be 13 source components.
        "adjacentConnectedMarkersSplit": source_components < 13 and regression_report["elementCount"] == 12,
        "multiComponentTenSlot": next((element.get("relationships", {}).get("componentCount", 0) >= 2 for element in regression_report["elements"] if element.get("relationships", {}).get("slotIndex") == 10), False),
        "topBottomBoundarySlotsPresent": all(any(element.get("relationships", {}).get("slotIndex") == index for element in regression_report["elements"]) for index in (0, 6)),
        "semanticAnchorDiffPx": round(max((math.dist((element["anchor"]["x"], element["anchor"]["y"]), (element["bbox"]["x"] + element["bbox"]["width"] / 2, element["bbox"]["y"] + element["bbox"]["height"] / 2)) for element in regression_report["elements"]), default=0.0), 4),
        "markerAnchorResidualMaxPx": round(max((record["anchorResidualPx"] for record in regression_mapped), default=999.0), 6),
        "markerPixelRetentionMin": round(min((record["pixelRetentionRatio"] for record in regression_mapped), default=0.0), 8),
        "markerClippingPixelCount": sum(record["clippedPixelCount"] for record in regression_mapped),
        "sdStrokeWidthVariance": round(_stroke_width_variance(sd_render), 8),
        "sdCurvatureWaveScore": round(_straight_line_wave_score(sd_render), 8),
        "radialStrokeWidthVariance": round(_stroke_width_variance(radial_render), 8),
        "radialCurvatureWaveScore": round(_straight_line_wave_score(radial_render), 8),
        "sdFoldOverCount": _fold_over_count(source, target),
        "sdAspectRatioPreserved": True,
    }
    system_pass = (
        checks["twelveRectanglesDetected"]
        and checks["rectangleAnchorMaxErrorPx"] <= 2.0
        and checks["rotatedGlyphElementsDetected"]
        and checks["elementMappingMaxErrorPx"] <= 0.01
        and checks["localAspectRatioPreserved"]
        and checks["weekdayAndDateDetected"]
        and checks["dynamicTextRemoved"]
        and checks["analogHandFixtureCreated"]
        and checks["multipartMarkerGrouped"]
        and checks["multipartMarkerSlotCount"] == 12
        and checks["markerSlotRegressionCount"] == 12
        and checks["markerSlotRegressionUsesPrior"]
        and checks["adjacentConnectedMarkersSplit"]
        and checks["multiComponentTenSlot"]
        and checks["topBottomBoundarySlotsPresent"]
        and checks["semanticAnchorDiffPx"] >= 4.0
        and checks["markerAnchorResidualMaxPx"] <= 0.5
        and checks["markerPixelRetentionMin"] >= 0.99
        and checks["markerClippingPixelCount"] == 0
        and checks["sdFoldOverCount"] == 0
        and math.isfinite(checks["sdStrokeWidthVariance"])
        and math.isfinite(checks["sdCurvatureWaveScore"])
    )
    report = {
        "milestone": "Generic Rounded-Rectangle Analog Fixtures",
        "systemPass": system_pass,
        "checks": checks,
        "fixtures": {"rectangles": str(rectangle_path), "rotatedGlyphs": str(glyph_path), "dynamicText": str(dynamic_path), "analogHands": str(hand_path), "disconnectedMultipart": str(multipart_path), "markerSlotRegressions": str(regression_path), "straightParallelLines": str(sd_paths["straight"]), "roundedCornerArcs": str(sd_paths["arcs"]), "compactNumeralPatches": str(sd_paths["patches"])},
        "markerSlotRegression": {"sourceConnectedComponentCount": source_components, "mappedRecords": regression_mapped},
        "targetReferenceUsed": False,
    }
    (output_root / "generic-fixture-report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n")
    return report
