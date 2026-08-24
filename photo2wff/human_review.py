from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from .display_geometry import RoundedRect, inverse_raster_map, map_structured_element
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


def _render_atlas(xml_path: Path, review_dir: Path, times: tuple[str, ...]) -> dict[str, Any]:
    render_dir = review_dir / "atlas-renders"
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
    atlas_path = review_dir / "atlas.png"
    atlas.save(atlas_path)
    return {"path": str(atlas_path), "times": list(times), "renderDirectory": str(render_dir)}


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
    source_clock = scene.get("clock", {})
    source_clock_center = (float(source_clock.get("centerX", source.center_x)), float(source_clock.get("centerY", source.center_y)))
    target_clock_center = (float(scene["canvas"]["centerX"]), float(scene["canvas"]["centerY"]))
    structured = [
        map_structured_element(element, source, target, source_clock_center, target_clock_center)
        for element in scene.get("elements", [])
        if element.get("type") not in {"STATIC_IMAGE", "IMAGE", "ICON"}
    ]
    manifest = {
        "milestone": "A1c Display Geometry + Human Review",
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
        "artifacts": {
            "geometryOverlay": str(overlay_path),
            "assetSheet": str(asset_sheet_path),
            "mappingComparison": str(mapping_path),
            "dialCleanAdaptive": str(dial_adaptive_path),
        },
        "deviceOrEmulatorVerification": "deferred",
    }
    manifest_path = review_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return manifest
