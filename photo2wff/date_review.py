from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from .wff_render import render_wff_xml


DATE_REVIEW_DAYS = (1, 8, 11, 20, 31)


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        Path("C:/Windows/Fonts/arialbd.ttf") if bold else Path("C:/Windows/Fonts/arial.ttf"),
        Path("C:/Windows/Fonts/segoeuib.ttf") if bold else Path("C:/Windows/Fonts/segoeui.ttf"),
    )
    for candidate in candidates:
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size)
    return ImageFont.load_default()


def _bbox_from_ink(image: Image.Image, inner: dict[str, int]) -> dict[str, int] | None:
    pixels = image.convert("RGB").load()
    points = []
    left, top = inner["x"], inner["y"]
    right, bottom = left + inner["width"], top + inner["height"]
    for y in range(max(0, top), min(image.height, bottom)):
        for x in range(max(0, left), min(image.width, right)):
            red, green, blue = pixels[x, y]
            if max(red, green, blue) >= 55 and not (red > green * 1.35 and red > blue * 1.15):
                points.append((x, y))
    if not points:
        return None
    return {
        "x": min(x for x, _ in points),
        "y": min(y for _, y in points),
        "width": max(x for x, _ in points) - min(x for x, _ in points) + 1,
        "height": max(y for _, y in points) - min(y for _, y in points) + 1,
    }


def _metrics(image: Image.Image, metadata: dict[str, Any], day: int) -> dict[str, Any]:
    inner = metadata["innerBbox"]
    ink = _bbox_from_ink(image, inner)
    inner_center = (inner["x"] + inner["width"] / 2, inner["y"] + inner["height"] / 2)
    if ink is None:
        return {
            "day": day,
            "inkBbox": None,
            "horizontalOffset": None,
            "verticalOffset": None,
            "leftPadding": None,
            "rightPadding": None,
            "topPadding": None,
            "bottomPadding": None,
            "clipped": True,
            "baselineY": None,
        }
    ink_center = (ink["x"] + ink["width"] / 2, ink["y"] + ink["height"] / 2)
    return {
        "day": day,
        "inkBbox": ink,
        "horizontalOffset": round(ink_center[0] - inner_center[0], 3),
        "verticalOffset": round(ink_center[1] - inner_center[1], 3),
        "leftPadding": ink["x"] - inner["x"],
        "rightPadding": inner["x"] + inner["width"] - (ink["x"] + ink["width"]),
        "topPadding": ink["y"] - inner["y"],
        "bottomPadding": inner["y"] + inner["height"] - (ink["y"] + ink["height"]),
        "clipped": ink["x"] <= inner["x"] or ink["y"] <= inner["y"] or ink["x"] + ink["width"] >= inner["x"] + inner["width"] or ink["y"] + ink["height"] >= inner["y"] + inner["height"],
        "baselineY": round(inner_center[1] + 8, 3),
    }


def _crop_panel(image: Image.Image, metadata: dict[str, Any], metrics: dict[str, Any], size: tuple[int, int]) -> Image.Image:
    frame = metadata["frameBbox"]
    pad = 18
    box = (
        max(0, frame["x"] - pad),
        max(0, frame["y"] - pad),
        min(image.width, frame["x"] + frame["width"] + pad),
        min(image.height, frame["y"] + frame["height"] + pad),
    )
    crop = image.crop(box).convert("RGB").resize(size, Image.Resampling.NEAREST)
    draw = ImageDraw.Draw(crop)
    draw.rectangle((0, 0, size[0] - 1, size[1] - 1), outline="#FFD34E", width=3)
    if metrics.get("inkBbox"):
        ink = metrics["inkBbox"]
        scale_x = size[0] / max(1, box[2] - box[0])
        scale_y = size[1] / max(1, box[3] - box[1])
        draw.rectangle(
            (
                (ink["x"] - box[0]) * scale_x,
                (ink["y"] - box[1]) * scale_y,
                (ink["x"] + ink["width"] - box[0]) * scale_x,
                (ink["y"] + ink["height"] - box[1]) * scale_y,
            ),
            outline="#39F5B0",
            width=2,
        )
    draw.text((8, 8), f"DAY {metrics['day']}", fill="#FFFFFF", stroke_width=2, stroke_fill="#000000", font=_font(20, True))
    return crop


def _make_geometry_review(empty_dial: Image.Image, metadata: dict[str, Any], metrics: list[dict[str, Any]], output: Path) -> None:
    frame = metadata["frameBbox"]
    pad = 34
    crop_box = (max(0, frame["x"] - pad), max(0, frame["y"] - pad), min(empty_dial.width, frame["x"] + frame["width"] + pad), min(empty_dial.height, frame["y"] + frame["height"] + pad))
    crop = empty_dial.crop(crop_box).convert("RGB").resize(((crop_box[2] - crop_box[0]) * 5, (crop_box[3] - crop_box[1]) * 5), Image.Resampling.NEAREST)
    draw = ImageDraw.Draw(crop)
    scale = 5
    frame_left = (frame["x"] - crop_box[0]) * scale
    frame_top = (frame["y"] - crop_box[1]) * scale
    frame_right = (frame["x"] + frame["width"] - crop_box[0]) * scale
    frame_bottom = (frame["y"] + frame["height"] - crop_box[1]) * scale
    inner = metadata["innerBbox"]
    inner_left = (inner["x"] - crop_box[0]) * scale
    inner_top = (inner["y"] - crop_box[1]) * scale
    inner_right = (inner["x"] + inner["width"] - crop_box[0]) * scale
    inner_bottom = (inner["y"] + inner["height"] - crop_box[1]) * scale
    draw.rectangle((frame_left, frame_top, frame_right, frame_bottom), outline="#FF4545", width=4)
    draw.rectangle((inner_left, inner_top, inner_right, inner_bottom), outline="#3BD6FF", width=4)
    center_x = (inner_left + inner_right) / 2
    center_y = (inner_top + inner_bottom) / 2
    draw.line((center_x, inner_top, center_x, inner_bottom), fill="#42F58D", width=2)
    draw.line((inner_left, center_y, inner_right, center_y), fill="#42F58D", width=2)
    baseline_y = (metadata["innerBbox"]["y"] + metadata["innerBbox"]["height"] / 2 + 8 - crop_box[1]) * scale
    draw.line((inner_left, baseline_y, inner_right, baseline_y), fill="#FFD34E", width=2)
    legend = "RED frame | CYAN padded text | GREEN center\nYELLOW baseline proxy"
    draw.multiline_text((12, 12), legend, fill="#FFFFFF", stroke_width=3, stroke_fill="#000000", font=_font(15, True), spacing=4)
    output.parent.mkdir(parents=True, exist_ok=True)
    crop.save(output)


def generate_date_window_review_artifacts(scene: dict[str, Any], output_root: Path, xml_path: Path, metadata: dict[str, Any]) -> dict[str, Any]:
    review_dir = output_root / "human-review" / "date-window"
    render_dir = review_dir / "renders"
    review_dir.mkdir(parents=True, exist_ok=True)
    render_dir.mkdir(parents=True, exist_ok=True)
    fixed_time = str(scene.get("preview", {}).get("time", "10:08:30"))
    renders: dict[int, Path] = {}
    metrics: list[dict[str, Any]] = []
    for day in DATE_REVIEW_DAYS:
        path = render_dir / f"day-{day:02d}.png"
        render_wff_xml(xml_path, path, fixed_time=fixed_time, fixed_date=f"2024-08-{day:02d}")
        renders[day] = path
        metrics.append(_metrics(Image.open(path), metadata, day))

    tile_size = (300, 250)
    atlas = Image.new("RGB", (tile_size[0] * 3, tile_size[1] * 2), "#151515")
    draw = ImageDraw.Draw(atlas)
    for index, day in enumerate(DATE_REVIEW_DAYS):
        tile = _crop_panel(Image.open(renders[day]), metadata, metrics[index], (230, 180))
        left = (index % 3) * tile_size[0] + 35
        top = (index // 3) * tile_size[1] + 20
        atlas.paste(tile, (left, top))
        draw.text(((index % 3) * tile_size[0] + 35, top + 188), f"day={day}  dx={metrics[index].get('horizontalOffset')}  dy={metrics[index].get('verticalOffset')}", fill="#FFFFFF", font=_font(14, True))
    atlas_path = review_dir / "date-window-review-atlas.png"
    atlas.save(atlas_path)
    empty_dial = Image.open(output_root / "assets" / "dial_empty_date.png")
    geometry_path = review_dir / "date-window-geometry-review.png"
    _make_geometry_review(empty_dial, metadata, metrics, geometry_path)
    report = {
        "semanticType": metadata["semanticType"],
        "days": list(DATE_REVIEW_DAYS),
        "fixedTime": fixed_time,
        "renders": {str(day): str(path) for day, path in renders.items()},
        "metrics": metrics,
        "artifacts": {
            "atlas": str(atlas_path),
            "geometryReview": str(geometry_path),
        },
        "checks": {
            "horizontalVerticalCentering": all(abs(float(item["horizontalOffset"])) <= 2 and abs(float(item["verticalOffset"])) <= 2 for item in metrics if item["horizontalOffset"] is not None),
            "clipping": all(not item["clipped"] for item in metrics),
            "baselineAndPaddingVisible": True,
        },
        "deviceOrEmulatorVerification": "deferred",
    }
    report_path = review_dir / "manifest.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return report
