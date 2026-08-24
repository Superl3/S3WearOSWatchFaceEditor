from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFont

from .display_geometry import RoundedRect, inverse_raster_map, map_structured_element
from .date_review import generate_date_window_review_artifacts
from .glyph_review import generate_glyph_review_artifacts
from .wff_render import render_wff_xml


REVIEW_TIMES = (
    "00:00:00",
    "03:15:45",
    "06:30:00",
    "09:45:15",
    "10:08:30",
    "12:00:00",
    "15:22:10",
    "18:40:30",
    "21:55:50",
)


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf",
    ]
    for candidate in candidates:
        path = Path(candidate)
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


def _shape_from_scene(scene: dict[str, Any], key: str) -> RoundedRect:
    display_geometry = scene.get("displayGeometry", {})
    value = display_geometry.get(key)
    if isinstance(value, dict):
        return RoundedRect.from_dict(value)
    canvas = scene["canvas"]
    return RoundedRect(float(canvas["width"]), float(canvas["height"]), float(canvas["width"]) / 2, float(canvas["centerX"]), float(canvas["centerY"]))


def _paste_fit(base: Image.Image, image: Image.Image, box: tuple[int, int, int, int]) -> None:
    left, top, width, height = box
    fitted = image.convert("RGBA")
    fitted.thumbnail((width, height), Image.Resampling.LANCZOS)
    x = left + (width - fitted.width) // 2
    y = top + (height - fitted.height) // 2
    base.alpha_composite(fitted, (x, y))


def _draw_shape(draw: ImageDraw.ImageDraw, shape: RoundedRect, offset: tuple[int, int], outline: tuple[int, int, int], width: int = 3) -> None:
    left, top, right, bottom = shape.bounds()
    x, y = offset
    bounds = (round(left + x), round(top + y), round(right + x), round(bottom + y))
    if shape.is_circle:
        draw.ellipse(bounds, outline=outline, width=width)
    else:
        draw.rounded_rectangle(bounds, radius=round(shape.radius), outline=outline, width=width)


def _draw_cross(draw: ImageDraw.ImageDraw, point: tuple[float, float], offset: tuple[int, int], color: tuple[int, int, int]) -> None:
    x = point[0] + offset[0]
    y = point[1] + offset[1]
    draw.line((round(x - 8), round(y), round(x + 8), round(y)), fill=color, width=2)
    draw.line((round(x), round(y - 8), round(x), round(y + 8)), fill=color, width=2)


def _render_atlas(
    xml_path: Path,
    review_dir: Path,
    times: tuple[str, ...],
    atlas_name: str = "atlas.png",
    render_subdir: str = "atlas-renders",
) -> dict[str, Any]:
    render_dir = review_dir / render_subdir
    render_dir.mkdir(parents=True, exist_ok=True)
    tile_width = 320
    tile_height = 350
    atlas = Image.new("RGB", (tile_width * 3, tile_height * ((len(times) + 2) // 3)), "#171717")
    draw = ImageDraw.Draw(atlas)
    for index, fixed_time in enumerate(times):
        path = render_dir / f"{fixed_time.replace(':', '-')}.png"
        render_wff_xml(xml_path, path, fixed_time=fixed_time)
        image = Image.open(path).convert("RGB")
        tile_left = (index % 3) * tile_width
        tile_top = (index // 3) * tile_height
        tile = image.resize((280, 280), Image.Resampling.LANCZOS)
        atlas.paste(tile, (tile_left + 20, tile_top + 10))
        draw.text((tile_left + 20, tile_top + 302), fixed_time, fill="#FFFFFF", font=_font(22, bold=True))
        draw.text((tile_left + 20, tile_top + 328), "WFF XML deterministic render", fill="#A8A8A8", font=_font(14))
    atlas_path = review_dir / atlas_name
    atlas.save(atlas_path)
    return {"path": str(atlas_path), "times": list(times), "renderDirectory": str(render_dir)}


def _parse_time(value: str) -> tuple[int, int, int]:
    hours, minutes, seconds = (int(part) for part in value.split(":"))
    return hours, minutes, seconds


def _clock_angles(value: str) -> dict[str, float]:
    hours, minutes, seconds = _parse_time(value)
    return {
        "HOUR": ((hours % 12) + minutes / 60 + seconds / 3600) * 30,
        "MINUTE": (minutes + seconds / 60) * 6,
        "SECOND": seconds * 6,
    }


def _angular_distance(first: float, second: float) -> float:
    return abs((first - second + 180) % 360 - 180)


def _source_hand_angles(scene: dict[str, Any]) -> dict[str, float]:
    return {
        str(element.get("role")): float(element.get("observedAngleDeg", 0))
        for element in scene.get("elements", [])
        if element.get("type") == "ANALOG_HAND" and element.get("role")
    }


def _choose_occlusion_reveal_times(scene: dict[str, Any], count: int = 9) -> tuple[tuple[str, ...], list[dict[str, Any]]]:
    source_angles = _source_hand_angles(scene)
    required = ("03:15:45", "06:30:00", "09:45:15")
    candidates = set(required)
    for hour in range(24):
        for minute in range(0, 60, 5):
            candidates.add(f"{hour:02d}:{minute:02d}:00")
    scored = []
    for fixed_time in sorted(candidates):
        angles = _clock_angles(fixed_time)
        distances = {
            role: round(_angular_distance(angle, source_angles.get(role, angle)), 4)
            for role, angle in angles.items()
            if role in source_angles
        }
        score = sum(distances.values()) / max(1, len(distances))
        scored.append((score, fixed_time, distances))
    selected: list[tuple[float, str, dict[str, float]]] = []
    for item in sorted(scored, key=lambda value: (-value[0], value[1])):
        if item[1] in required or len(selected) < count:
            selected.append(item)
        if len(selected) >= count and all(required_time in {value[1] for value in selected} for required_time in required):
            break
    selected = sorted(selected, key=lambda value: value[1])
    return tuple(item[1] for item in selected), [
        {"time": item[1], "score": round(item[0], 4), "angularDistanceDeg": item[2]}
        for item in selected
    ]


def _rgb_panel(image: Image.Image, size: tuple[int, int], mask: Image.Image | None = None, tint: tuple[int, int, int] | None = None) -> Image.Image:
    fitted = image.convert("RGBA").resize(size, Image.Resampling.LANCZOS)
    if mask is not None:
        fitted_mask = mask.convert("L").resize(size, Image.Resampling.NEAREST)
        if tint is None:
            fitted = Image.merge("RGBA", (fitted_mask, fitted_mask, fitted_mask, Image.new("L", size, 255)))
        else:
            color = Image.new("RGBA", size, (*tint, 255))
            fitted = Image.composite(color, Image.new("RGBA", size, (0, 0, 0, 255)), fitted_mask)
    background = Image.new("RGB", size, "#000000")
    background.paste(fitted.convert("RGB"), mask=fitted.getchannel("A"))
    return background


def _make_hands_off(dial: Image.Image, center_cap_path: Path, output: Path) -> None:
    canvas = Image.new("RGBA", dial.size, (0, 0, 0, 255))
    canvas.alpha_composite(dial.convert("RGBA"))
    if center_cap_path.exists():
        cap = Image.open(center_cap_path).convert("RGBA")
        cap_left = round((dial.width - cap.width) / 2)
        cap_top = round((dial.height - cap.height) / 2)
        canvas.alpha_composite(cap, (cap_left, cap_top))
    canvas.convert("RGB").save(output)


def _make_four_way_comparison(
    source: Image.Image,
    before: Image.Image,
    occlusion_mask: Image.Image,
    reconstructed_mask: Image.Image,
    completed: Image.Image,
    output: Path,
) -> None:
    panel_size = (438, 438)
    header = 58
    labels = ("SOURCE", "MASK", "RECONSTRUCTED PIXELS", "FINAL")
    panels = (
        _rgb_panel(source, panel_size),
        _rgb_panel(before, panel_size, occlusion_mask, (255, 55, 55)),
        _rgb_panel(completed, panel_size, reconstructed_mask, (70, 245, 170)),
        _rgb_panel(completed, panel_size),
    )
    canvas = Image.new("RGB", (panel_size[0] * 4, panel_size[1] + header), "#101010")
    draw = ImageDraw.Draw(canvas)
    for index, (label, panel) in enumerate(zip(labels, panels)):
        left = index * panel_size[0]
        draw.text((left + 14, 16), label, fill="#FFFFFF", font=_font(20, bold=True))
        canvas.paste(panel, (left, header))
    output.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output)


def _make_reconstructed_highlight(completed: Image.Image, reconstructed_mask: Image.Image, output: Path) -> None:
    highlighted = completed.convert("RGBA")
    overlay = Image.new("RGBA", highlighted.size, (255, 0, 190, 0))
    overlay.putalpha(reconstructed_mask.convert("L").point(lambda value: round(value * 0.78)))
    highlighted.alpha_composite(overlay)
    draw = ImageDraw.Draw(highlighted)
    draw.text((12, 12), "GENERATED PIXELS / REVIEW", fill="#FFFFFF", stroke_width=3, stroke_fill="#000000", font=_font(18, bold=True))
    highlighted.convert("RGB").save(output)


def _make_occlusion_zoom_sheet(
    reference: Image.Image,
    before: Image.Image,
    completed: Image.Image,
    occlusion_mask: Image.Image,
    metadata: dict[str, Any],
    output: Path,
) -> None:
    roles = metadata.get("regions", [])
    tile = (220, 220)
    row_height = 278
    canvas = Image.new("RGB", (tile[0] * 4, 74 + row_height * max(1, len(roles))), "#111111")
    draw = ImageDraw.Draw(canvas)
    draw.text((18, 20), "HAND OCCLUSION ZOOM  source / mask / before / completed", fill="#FFFFFF", font=_font(22, bold=True))
    for row, region in enumerate(roles):
        bbox = region.get("bbox") or [0, 0, reference.width, reference.height]
        left, top, right, bottom = (int(value) for value in bbox)
        pad = 20
        crop_box = (max(0, left - pad), max(0, top - pad), min(reference.width, right + pad), min(reference.height, bottom + pad))
        panels = (
            _rgb_panel(reference.crop(crop_box), tile),
            _rgb_panel(before.crop(crop_box), tile, occlusion_mask.crop(crop_box), (255, 55, 55)),
            _rgb_panel(before.crop(crop_box), tile),
            _rgb_panel(completed.crop(crop_box), tile),
        )
        y = 74 + row * row_height
        draw.text((18, y), f"{region.get('sourceHandRole', region.get('id', 'region'))}  {region.get('class', '')}  confidence={region.get('confidence', 0)}", fill="#FFD27D", font=_font(15, bold=True))
        for index, panel in enumerate(panels):
            canvas.paste(panel, (index * tile[0], y + 28))
    output.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output)


def _make_geometry_overlay(source_image: Image.Image, adaptive_image: Image.Image, source: RoundedRect, target: RoundedRect, output: Path) -> None:
    panel_size = (438, 438)
    gap = 24
    header = 52
    canvas = Image.new("RGB", (panel_size[0] * 2 + gap, panel_size[1] + header), "#111111")
    canvas.paste(source_image.convert("RGB").resize(panel_size, Image.Resampling.LANCZOS), (0, header))
    adaptive_rgb = Image.new("RGB", panel_size, "#000000")
    adaptive_rgb.paste(adaptive_image.convert("RGBA"), mask=adaptive_image.convert("RGBA").getchannel("A"))
    canvas.paste(adaptive_rgb, (panel_size[0] + gap, header))
    draw = ImageDraw.Draw(canvas)
    draw.text((14, 14), "SOURCE  RoundedRect", fill="#FFB45B", font=_font(20, bold=True))
    draw.text((panel_size[0] + gap + 14, 14), "TARGET  Circle special case", fill="#79C7FF", font=_font(20, bold=True))
    _draw_shape(draw, source, (0, header), (255, 169, 76), 3)
    _draw_shape(draw, target, (panel_size[0] + gap, header), (93, 194, 255), 3)
    _draw_cross(draw, (source.center_x, source.center_y), (0, header), (255, 85, 85))
    _draw_cross(draw, (target.center_x, target.center_y), (panel_size[0] + gap, header), (93, 194, 255))
    for angle in range(0, 360, 45):
        radians = math.radians(angle)
        direction = (math.sin(radians), -math.cos(radians))
        source_distance = source.boundary_distance(direction)
        target_distance = target.boundary_distance(direction)
        source_end = (source.center_x + direction[0] * source_distance, source.center_y + direction[1] * source_distance)
        target_end = (target.center_x + direction[0] * target_distance, target.center_y + direction[1] * target_distance)
        draw.line((source.center_x, source.center_y + header, source_end[0], source_end[1] + header), fill=(255, 169, 76), width=1)
        draw.line((target.center_x + panel_size[0] + gap, target.center_y + header, target_end[0] + panel_size[0] + gap, target_end[1] + header), fill=(93, 194, 255), width=1)
    output.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output)


def _make_asset_sheet(assets_dir: Path, output: Path) -> None:
    names = (
        ("dial_clean", "dial_clean.png"),
        ("hour", "hour_hand.png"),
        ("minute", "minute_hand.png"),
        ("second", "second_hand.png"),
        ("center cap", "center_cap.png"),
    )
    canvas = Image.new("RGBA", (1080, 700), "#121212")
    draw = ImageDraw.Draw(canvas)
    draw.text((22, 16), "dial_clean + extracted hand asset sheet", fill="#FFFFFF", font=_font(24, bold=True))
    dial_path = assets_dir / names[0][1]
    if dial_path.exists():
        _paste_fit(canvas, Image.open(dial_path), (20, 64, 500, 610))
    draw.rectangle((20, 64, 500, 674), outline="#666666", width=2)
    draw.text((34, 640), "dial_clean", fill="#FFFFFF", font=_font(18, bold=True))
    positions = ((540, 80), (800, 80), (540, 380), (800, 380))
    for (label, filename), (left, top) in zip(names[1:], positions):
        path = assets_dir / filename
        draw.rectangle((left, top, left + 220, top + 220), fill="#050505", outline="#666666", width=2)
        if path.exists():
            _paste_fit(canvas, Image.open(path), (left + 10, top + 10, 200, 190))
        draw.text((left, top + 232), label, fill="#FFFFFF", font=_font(18, bold=True))
        draw.text((left, top + 256), filename, fill="#A8A8A8", font=_font(14))
    output.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(output)


def _make_mapping_comparison(source_image: Image.Image, source: RoundedRect, target: RoundedRect, output: Path) -> None:
    source_rgba = source_image.convert("RGBA")
    left, top, right, bottom = source.bounds()
    source_crop = source_rgba.crop((round(left), round(top), round(right), round(bottom)))
    naive = Image.new("RGBA", (round(target.width), round(target.height)), (0, 0, 0, 255))
    naive.alpha_composite(source_crop.resize(naive.size, Image.Resampling.LANCZOS))
    adaptive = inverse_raster_map(source_rgba, source, target, (round(target.width), round(target.height)))
    panels = (source_rgba, naive, adaptive)
    labels = ("source normalized", "naive XY stretch", "rounded-rect adaptive")
    panel_width = round(target.width)
    panel_height = round(target.height)
    canvas = Image.new("RGB", (panel_width * 3, panel_height + 62), "#111111")
    draw = ImageDraw.Draw(canvas)
    for index, (panel, label) in enumerate(zip(panels, labels)):
        rgb = Image.new("RGB", panel.size, "#000000")
        if panel.mode == "RGBA":
            rgb.paste(panel.convert("RGBA"), mask=panel.convert("RGBA").getchannel("A"))
        else:
            rgb.paste(panel.convert("RGB"))
        canvas.paste(rgb.resize((panel_width, panel_height), Image.Resampling.LANCZOS), (index * panel_width, 0))
        draw.text((index * panel_width + 14, panel_height + 18), label, fill="#FFFFFF", font=_font(20, bold=True))
    output.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output)


def generate_human_review_artifacts(scene: dict[str, Any], output_root: Path, xml_path: Path, times: tuple[str, ...] = REVIEW_TIMES) -> dict[str, Any]:
    review_dir = output_root / "human-review"
    review_dir.mkdir(parents=True, exist_ok=True)
    assets_dir = output_root / "assets"
    source_path = assets_dir / "display_reference.png"
    if not source_path.exists():
        source_path = output_root / "reference.png"
    source_image = Image.open(source_path).convert("RGBA")
    dial_path = assets_dir / "dial_clean.png"
    if not dial_path.exists():
        raise FileNotFoundError(f"dial_clean asset not found: {dial_path}")
    source = _shape_from_scene(scene, "source")
    target = _shape_from_scene(scene, "target")
    adaptive = inverse_raster_map(source_image, source, target, (round(target.width), round(target.height)))
    atlas = _render_atlas(xml_path, review_dir, times)
    overlay_path = review_dir / "geometry-overlay.png"
    _make_geometry_overlay(source_image, adaptive, source, target, overlay_path)
    asset_sheet_path = review_dir / "asset-sheet.png"
    _make_asset_sheet(assets_dir, asset_sheet_path)
    dial_clean = Image.open(dial_path).convert("RGBA")
    dial_adaptive = inverse_raster_map(dial_clean, source, target, (round(target.width), round(target.height)))
    dial_adaptive_path = review_dir / "dial-clean-adaptive.png"
    dial_adaptive_rgb = Image.new("RGB", dial_adaptive.size, "#000000")
    dial_adaptive_rgb.paste(dial_adaptive, mask=dial_adaptive.getchannel("A"))
    dial_adaptive_rgb.save(dial_adaptive_path)
    mapping_path = review_dir / "mapping-comparison.png"
    _make_mapping_comparison(source_image, source, target, mapping_path)
    completed_path = assets_dir / "dial-completed.png"
    before_path = assets_dir / "dial-before-reconstruction.png"
    occlusion_mask_path = assets_dir / "hand-occlusion-mask.png"
    reconstructed_mask_path = assets_dir / "reconstructed-mask.png"
    occlusion_metadata_path = output_root / "occlusion-metadata.json"
    completed = Image.open(completed_path if completed_path.exists() else dial_path).convert("RGBA")
    before = Image.open(before_path if before_path.exists() else dial_path).convert("RGBA")
    occlusion_mask = Image.open(occlusion_mask_path).convert("L") if occlusion_mask_path.exists() else Image.new("L", completed.size, 0)
    reconstructed_mask = Image.open(reconstructed_mask_path).convert("L") if reconstructed_mask_path.exists() else Image.new("L", completed.size, 0)
    occlusion_metadata = json.loads(occlusion_metadata_path.read_text(encoding="utf-8")) if occlusion_metadata_path.exists() else {
        "status": "not_available",
        "requiresHumanReview": True,
    }
    hands_off_path = review_dir / "hands-off.png"
    _make_hands_off(completed, assets_dir / "center_cap.png", hands_off_path)
    zoom_sheet_path = review_dir / "occlusion-zoom-sheet.png"
    _make_occlusion_zoom_sheet(source_image, before, completed, occlusion_mask, occlusion_metadata, zoom_sheet_path)
    four_way_path = review_dir / "before-mask-reconstructed-final.png"
    _make_four_way_comparison(source_image, before, occlusion_mask, reconstructed_mask, completed, four_way_path)
    highlight_path = review_dir / "reconstructed-highlight.png"
    _make_reconstructed_highlight(completed, reconstructed_mask, highlight_path)
    generative_candidate_path = assets_dir / "generative-inpaint-candidate.png"
    reveal_times, reveal_selection = _choose_occlusion_reveal_times(scene)
    reveal_atlas = _render_atlas(
        xml_path,
        review_dir,
        reveal_times,
        atlas_name="occlusion-reveal-atlas.png",
        render_subdir="occlusion-reveal-renders",
    )
    source_clock = scene.get("clock", {})
    source_clock_center = (float(source_clock.get("centerX", source.center_x)), float(source_clock.get("centerY", source.center_y)))
    target_clock_center = (float(scene["canvas"]["centerX"]), float(scene["canvas"]["centerY"]))
    structured = [
        map_structured_element(element, source, target, source_clock_center, target_clock_center)
        for element in scene.get("elements", [])
        if element.get("type") not in {"STATIC_IMAGE", "IMAGE", "ICON"}
    ]
    manifest = {
        "milestone": "A1d Occlusion Reconstruction / Dial Completion",
        "source": source.as_dict(),
        "target": target.as_dict(),
        "mappingPolicies": {
            "naive": "NAIVE_XY_STRETCH",
            "adaptive": "CENTER_PRESERVING_BOUNDARY_NORMALIZED",
            "staticRaster": "INVERSE_RASTER_MAPPING",
            "structuredElements": "ANCHOR_PIVOT_GEOMETRY_ONLY_LOCAL_APPEARANCE_PRESERVED",
        },
        "structuredElements": structured,
        "staticRasterLayers": [
            {"id": "dial_clean", "operation": "INVERSE_RASTER_MAPPING", "output": str(dial_adaptive_path)},
            {"id": "center_cap", "operation": "LOCAL_ASSET_PRESERVED"},
        ],
        "atlas": atlas,
        "occlusion": {
            "metadata": str(occlusion_metadata_path) if occlusion_metadata_path.exists() else None,
            "status": occlusion_metadata.get("status"),
            "requiresHumanReview": bool(occlusion_metadata.get("requiresHumanReview", True)),
            "reconstructedPixelsAreObservedTruth": False,
            "revealSelection": reveal_selection,
        },
        "artifacts": {
            "geometryOverlay": str(overlay_path),
            "assetSheet": str(asset_sheet_path),
            "mappingComparison": str(mapping_path),
            "dialCleanAdaptive": str(dial_adaptive_path),
            "handsOff": str(hands_off_path),
            "occlusionZoomSheet": str(zoom_sheet_path),
            "fourWayComparison": str(four_way_path),
            "reconstructedHighlight": str(highlight_path),
            "generativeInpaintCandidate": str(generative_candidate_path) if generative_candidate_path.exists() else None,
            "occlusionRevealAtlas": reveal_atlas,
        },
        "deviceOrEmulatorVerification": "deferred",
    }
    date_slot = next((element for element in scene.get("elements", []) if element.get("type") == "DYNAMIC_SLOT"), None)
    date_metadata_path = output_root / "date-window-metadata.json"
    if date_slot is not None and date_metadata_path.exists():
        manifest["milestone"] = "A2 Dynamic Date Window"
        date_metadata = json.loads(date_metadata_path.read_text(encoding="utf-8"))
        manifest["dateWindow"] = generate_date_window_review_artifacts(scene, output_root, xml_path, date_metadata)
    glyph_report_path = output_root / "glyph-report.json"
    fallback_xml_path = output_root / "project-fallback/watchface/src/main/res/raw/watchface.xml"
    if date_slot is not None and glyph_report_path.exists() and fallback_xml_path.exists():
        manifest["milestone"] = "A2b.2 Compositional Glyph Synthesis"
        glyph_report = json.loads(glyph_report_path.read_text(encoding="utf-8"))
        candidate_xmls = {}
        for candidate in glyph_report.get("candidates", {}).get("3", []):
            candidate_id = str(candidate.get("candidate", "")).zfill(2)
            candidate_xml = output_root / f"project-themed-review-candidate-{candidate_id}/watchface/src/main/res/raw/watchface.xml"
            if candidate_xml.exists():
                candidate_xmls[candidate_id] = candidate_xml
        manifest["themedGlyph"] = generate_glyph_review_artifacts(
            scene,
            output_root,
            xml_path,
            fallback_xml_path,
            glyph_report,
            candidate_xmls=candidate_xmls,
        )
    manifest_path = review_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return manifest
