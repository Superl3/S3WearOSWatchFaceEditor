from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from .display_geometry import RoundedRect, boundary_normalized_map, direction_from_angle
from .dynamic_text import extract_center_dynamic_text
from .perimeter_artwork import decompose_perimeter_artwork, draw_perimeter_overlay, render_element_preserving_mapping


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


def run_generic_fixtures(output_root: Path) -> dict[str, Any]:
    """Verify generic geometry before any real reference is admitted as input."""

    output_root.mkdir(parents=True, exist_ok=True)
    source = RoundedRect(360, 400, 54, 219, 219)
    target = RoundedRect(438, 438, 219, 219, 219)
    rectangle_path, expected_anchors = _fixture_perimeter(output_root, source, glyph_like=False)
    glyph_path, _ = _fixture_perimeter(output_root, source, glyph_like=True)
    dynamic_path = _fixture_dynamic_text(output_root)
    hand_path = _fixture_hands(output_root)

    rectangle_report = decompose_perimeter_artwork(Image.open(rectangle_path), source, output_root / "rectangle-decomposition", minimum_area=40)
    glyph_report = decompose_perimeter_artwork(Image.open(glyph_path), source, output_root / "glyph-decomposition", minimum_area=10)
    draw_perimeter_overlay(Image.open(glyph_path), glyph_report["elements"], output_root / "detected-perimeter-overlay.png")
    mapped, mapped_records = render_element_preserving_mapping(
        Image.new("RGBA", (438, 438), "black"), glyph_report["elements"], source, target, output_root / "glyph-decomposition"
    )
    mapped.convert("RGB").save(output_root / "element-preserving-circle.png")
    dynamic_report = extract_center_dynamic_text(Image.open(dynamic_path), output_root / "dynamic-text")

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
    )
    report = {
        "milestone": "Generic Rounded-Rectangle Analog Fixtures",
        "systemPass": system_pass,
        "checks": checks,
        "fixtures": {"rectangles": str(rectangle_path), "rotatedGlyphs": str(glyph_path), "dynamicText": str(dynamic_path), "analogHands": str(hand_path)},
        "targetReferenceUsed": False,
    }
    (output_root / "generic-fixture-report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n")
    return report
