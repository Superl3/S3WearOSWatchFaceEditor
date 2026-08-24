from __future__ import annotations

import copy
import json
import math
import shutil
import subprocess
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

from .compiler import compile_project
from .human_review import REVIEW_TIMES
from .manual_glyphs import import_manual_glyphs
from .model import load_scene, save_scene
from .occlusion import _hand_masks
from .render import render_scene
from .runtime_validation import capture_runtime_cases, detect_runtime
from .wff_render import render_wff_xml
from .wff_validate import validate_wff_xml

PRODUCTION_ASSETS = (
    "dial_complete.png",
    "hour_hand.png",
    "minute_hand.png",
    "second_hand.png",
    "center_cap.png",
)
DATE_DAYS = (1, 8, 11, 20, 31)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        Path("C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf"),
        Path("C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf"),
    )
    for candidate in candidates:
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size)
    return ImageFont.load_default()


def _copy_gradle_wrapper(project: Path) -> None:
    template = Path(__file__).resolve().parent.parent / "templates" / "gradle"
    for source in (template / "gradlew", template / "gradlew.bat"):
        if source.exists():
            shutil.copy2(source, project / source.name)
    if (template / "wrapper").exists():
        shutil.copytree(template / "wrapper", project / "gradle" / "wrapper", dirs_exist_ok=True)


def _build_project(project: Path) -> dict[str, Any]:
    project = project.resolve()
    result = subprocess.run(
        [str(project / "gradlew.bat"), "assembleDebug"],
        cwd=project,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    apk = project / "watchface" / "build" / "outputs" / "apk" / "debug" / "watchface-debug.apk"
    return {
        "success": result.returncode == 0 and apk.exists(),
        "returnCode": result.returncode,
        "apk": str(apk) if apk.exists() else None,
        "output": (result.stdout + result.stderr)[-6000:],
    }


def _hand_records(scene: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(element["role"]): {
            "bbox": element["bbox"],
            "observedAngleDeg": element["observedAngleDeg"],
            "length": element["length"],
            "thickness": element["thickness"],
        }
        for element in scene["elements"]
        if element["type"] == "ANALOG_HAND"
    }


def _extend_split_roi(
    result: Image.Image,
    source: Image.Image,
    mask: Image.Image,
    roi: tuple[int, int, int, int],
    split_x: float,
) -> Image.Image:
    """Continue a horizontally observed two-tone artwork into its center split."""

    source_pixels = source.load()
    result_pixels = result.load()
    changed = Image.new("L", result.size, 0)
    changed_pixels = changed.load()
    left, top, right, bottom = roi
    for y in range(max(0, top), min(result.height, bottom)):
        for x in range(max(0, left), min(result.width, right)):
            if mask.getpixel((x, y)) == 0:
                continue
            left_sample = next(
                (source_pixels[candidate, y] for candidate in range(x - 1, max(left - 1, x - 32), -1) if mask.getpixel((candidate, y)) == 0),
                None,
            )
            right_sample = next(
                (source_pixels[candidate, y] for candidate in range(x + 1, min(right, x + 32)) if mask.getpixel((candidate, y)) == 0),
                None,
            )
            sample = left_sample if x < split_x else right_sample
            if sample is None:
                sample = right_sample if x < split_x else left_sample
            if sample is not None:
                result_pixels[x, y] = sample
                changed_pixels[x, y] = 255
    return changed


def _repair_two_numeral(result: Image.Image, minute_mask: Image.Image) -> Image.Image:
    """Bridge only the hidden diagonal strokes of the observed 2 o'clock numeral."""

    scale = 4
    overlay = Image.new("RGBA", (result.width * scale, result.height * scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    color = (239, 237, 233, 255)
    draw.line((366 * scale, 118 * scale, 346 * scale, 136 * scale), fill=color, width=2 * scale)
    draw.line((359 * scale, 110 * scale, 346 * scale, 130 * scale), fill=color, width=2 * scale)
    overlay = overlay.rotate(
        -60,
        center=(345 * scale, 135 * scale),
        resample=Image.Resampling.BICUBIC,
    )
    overlay = overlay.resize(result.size, Image.Resampling.LANCZOS)
    bridge_region = Image.new("L", result.size, 0)
    ImageDraw.Draw(bridge_region).rectangle((300, 100, 390, 170), fill=255)
    alpha = ImageChops.multiply(overlay.getchannel("A"), ImageChops.multiply(minute_mask, bridge_region))
    overlay.putalpha(alpha)
    result.paste(overlay.convert("RGB"), (0, 0), overlay)
    return alpha


def _remove_date_glyph(image: Image.Image, source_assets: Path) -> tuple[Image.Image, Image.Image]:
    glyph_mask_path = source_assets / "date-window-glyph-mask.png"
    if not glyph_mask_path.exists():
        raise FileNotFoundError(f"date glyph mask not found: {glyph_mask_path}")
    glyph_mask = Image.open(glyph_mask_path).convert("L")
    result = image.copy()
    result.paste((0, 0, 0), mask=glyph_mask)
    return result, glyph_mask


def _make_production_dial(scene: dict[str, Any], source_root: Path, destination: Path) -> dict[str, Any]:
    source_assets = source_root / "assets"
    completed_path = source_assets / "dial-completed.png"
    reference_path = source_assets / "display_reference.png"
    completed = Image.open(completed_path).convert("RGB")
    reference = Image.open(reference_path).convert("RGB")
    center = (float(scene["clock"]["centerX"]), float(scene["clock"]["centerY"]))
    hand_records = _hand_records(scene)
    masks = {
        role: mask.filter(ImageFilter.MaxFilter(5))
        for role, mask in _hand_masks(reference, center, hand_records, margin=4).items()
    }
    cleanup_masks = {
        role: mask.filter(ImageFilter.MaxFilter(5))
        for role, mask in _hand_masks(reference, center, hand_records, margin=12).items()
    }
    union = Image.new("L", completed.size, 0)
    for mask in cleanup_masks.values():
        union = ImageChops.lighter(union, mask)

    production = completed.copy()
    production_pixels = production.load()
    minute_roi = (305, 100, 390, 160)
    second_roi = (160, 238, 278, 438)
    for y in range(production.height):
        for x in range(production.width):
            roles = [role for role, mask in cleanup_masks.items() if mask.getpixel((x, y))]
            if not roles:
                continue
            pixel = production_pixels[x, y]
            in_minute_static = "MINUTE" in roles and minute_roi[0] <= x < minute_roi[2] and minute_roi[1] <= y < minute_roi[3]
            in_second_static = "SECOND" in roles and second_roi[0] <= x < second_roi[2] and second_roi[1] <= y < second_roi[3]
            is_red_hand = pixel[0] >= 70 and pixel[0] > pixel[1] * 1.3 and pixel[0] > pixel[2] * 1.15
            if in_second_static:
                if is_red_hand:
                    production_pixels[x, y] = (0, 0, 0)
            elif in_minute_static:
                if is_red_hand or max(pixel) < 110:
                    production_pixels[x, y] = (0, 0, 0)
            else:
                production_pixels[x, y] = (0, 0, 0)

    minute_corridor = Image.new("L", production.size, 0)
    minute_angle = math.radians(float(hand_records["MINUTE"]["observedAngleDeg"]))
    minute_end = (
        round(center[0] + math.sin(minute_angle) * 190),
        round(center[1] - math.cos(minute_angle) * 190),
    )
    ImageDraw.Draw(minute_corridor).line((center[0], center[1], minute_end[0], minute_end[1]), fill=255, width=28)
    for y in range(production.height):
        for x in range(production.width):
            if minute_corridor.getpixel((x, y)) and not (
                minute_roi[0] <= x < minute_roi[2] and minute_roi[1] <= y < minute_roi[3]
            ):
                production_pixels[x, y] = (0, 0, 0)
    reconstructed = Image.new("L", production.size, 0)
    minute_repair = _repair_two_numeral(production, cleanup_masks["MINUTE"])
    reconstructed = ImageChops.lighter(reconstructed, minute_repair)
    second_repair = _extend_split_roi(
        production,
        completed,
        masks["SECOND"],
        second_roi,
        center[0],
    )
    reconstructed = ImageChops.lighter(reconstructed, second_repair)
    production, date_mask = _remove_date_glyph(production, source_assets)
    production.save(destination)

    changed = ImageChops.difference(completed, production).convert("L").point(lambda value: 255 if value else 0)
    unresolved = ImageChops.subtract(union, reconstructed)
    return {
        "source": str(completed_path),
        "reference": str(reference_path),
        "policy": "preserve observed pixels; black-fill simple background; bridge only known static intersections",
        "sourceHandCorridorPixels": sum(1 for value in union.tobytes() if value),
        "reconstructedStaticPixels": sum(1 for value in reconstructed.tobytes() if value),
        "unresolvedCorridorPixels": sum(1 for value in unresolved.tobytes() if value),
        "dateGlyphRemovedPixels": sum(1 for value in date_mask.tobytes() if value),
        "changedPixels": sum(1 for value in changed.tobytes() if value),
        "generatedPixelsAreObservedTruth": False,
        "requiresHumanReview": True,
        "reviewRegions": ["minute-hand intersection with 2 o'clock numeral", "second-hand intersection with lower artwork and 6 o'clock numeral"],
        "_masks": {"union": union, "reconstructed": reconstructed, "changed": changed, "date": date_mask},
    }


def _production_scene(source_scene: dict[str, Any]) -> dict[str, Any]:
    scene = copy.deepcopy(source_scene)
    for element in scene["elements"]:
        if element["id"] == "dial_clean":
            element["id"] = "dial_complete"
            element["asset"] = "assets/dial_complete.png"
        elif element["type"] == "DYNAMIC_SLOT":
            element.pop("manualGlyphs", None)
            element["style"]["fontFamily"] = "Pretendard"
    scene["analysis"].update(
        {
            "method": "production analog port from verified A0-A2.5 assets",
            "requiresHumanReview": True,
        }
    )
    return scene


def _panel_atlas(
    entries: list[tuple[str, Path]],
    destination: Path,
    columns: int = 3,
    tile_size: int = 280,
) -> None:
    rows = math.ceil(len(entries) / columns)
    cell_width, cell_height = tile_size + 40, tile_size + 64
    canvas = Image.new("RGB", (cell_width * columns, cell_height * rows), "#141414")
    draw = ImageDraw.Draw(canvas)
    for index, (label, path) in enumerate(entries):
        image = Image.open(path).convert("RGB")
        fitted = image.resize((tile_size, tile_size), Image.Resampling.LANCZOS)
        left = (index % columns) * cell_width + 20
        top = (index // columns) * cell_height + 10
        canvas.paste(fitted, (left, top))
        draw.text((left, top + tile_size + 12), label, fill="#FFFFFF", font=_font(20, bold=True))
    destination.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(destination)


def _side_by_side_atlas(
    deterministic: list[tuple[str, Path]],
    runtime: dict[str, Path],
    destination: Path,
) -> None:
    tile = 250
    row_height = tile + 48
    canvas = Image.new("RGB", (tile * 2 + 32, row_height * len(deterministic) + 46), "#111111")
    draw = ImageDraw.Draw(canvas)
    draw.text((12, 12), "DETERMINISTIC", fill="#B7E0FF", font=_font(20, bold=True))
    draw.text((tile + 24, 12), "WEAR OS RUNTIME", fill="#B9F4CF", font=_font(20, bold=True))
    for row, (label, path) in enumerate(deterministic):
        top = 46 + row * row_height
        expected = Image.open(path).convert("RGB").resize((tile, tile), Image.Resampling.LANCZOS)
        canvas.paste(expected, (8, top))
        runtime_path = runtime.get(label)
        if runtime_path and runtime_path.exists():
            actual = Image.open(runtime_path).convert("RGB").resize((tile, tile), Image.Resampling.LANCZOS)
            canvas.paste(actual, (tile + 24, top))
        draw.text((12, top + tile + 10), label, fill="#FFFFFF", font=_font(18, bold=True))
    destination.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(destination)


def _static_review(source_root: Path, output_root: Path, repair: dict[str, Any]) -> dict[str, str]:
    review = output_root / "human-review"
    source = Image.open(source_root / "assets" / "display_reference.png").convert("RGB")
    dial = Image.open(output_root / "assets" / "dial_complete.png").convert("RGB")
    source_path = review / "source-normalized.png"
    hands_off_path = review / "dial-complete-hands-off.png"
    source.save(source_path)
    dial.save(hands_off_path)

    cap = Image.open(output_root / "assets" / "center_cap.png").convert("RGBA")
    hands_off_cap = dial.convert("RGBA")
    hands_off_cap.alpha_composite(cap, (207, 207))
    hands_off_cap.convert("RGB").save(review / "dial-complete-with-center-cap.png")

    highlight = dial.convert("RGBA")
    overlay = Image.new("RGBA", dial.size, (255, 0, 190, 0))
    overlay.putalpha(repair["_masks"]["changed"].point(lambda value: round(value * 0.72)))
    highlight.alpha_composite(overlay)
    highlight.convert("RGB").save(review / "reconstructed-pixel-highlight.png")

    panels = (
        ("SOURCE NORMALIZED", source),
        ("DIAL COMPLETE", dial),
        ("RECONSTRUCTED HIGHLIGHT", highlight.convert("RGB")),
    )
    canvas = Image.new("RGB", (438 * 3, 492), "#111111")
    draw = ImageDraw.Draw(canvas)
    for index, (label, image) in enumerate(panels):
        canvas.paste(image, (index * 438, 54))
        draw.text((index * 438 + 12, 15), label, fill="#FFFFFF", font=_font(20, bold=True))
    static_path = review / "static-detail-side-by-side.png"
    canvas.save(static_path)

    zoom_regions = (
        ("NUMERALS", (30, 15, 408, 205)),
        ("SOURCE-HAND / 2", (280, 90, 400, 180)),
        ("DATE WINDOW", (325, 185, 410, 255)),
        ("CENTER + CAP", (180, 180, 260, 260)),
        ("LOWER ARTWORK", (150, 235, 290, 390)),
        ("6 O'CLOCK", (185, 365, 250, 438)),
    )
    cap_composite = hands_off_cap.convert("RGB")
    zoom_sheet = Image.new("RGB", (780, len(zoom_regions) * 220 + 48), "#111111")
    zoom_draw = ImageDraw.Draw(zoom_sheet)
    for column, label in enumerate(("SOURCE", "DIAL COMPLETE", "WITH CENTER CAP")):
        zoom_draw.text((column * 260 + 12, 12), label, fill="#FFFFFF", font=_font(18, bold=True))
    for row, (label, box) in enumerate(zoom_regions):
        top = 48 + row * 220
        zoom_draw.text((12, top + 4), label, fill="#FFD27D", font=_font(16, bold=True))
        for column, image in enumerate((source, dial, cap_composite)):
            crop = image.crop(box)
            crop.thumbnail((236, 174), Image.Resampling.LANCZOS)
            left = column * 260 + 12 + (236 - crop.width) // 2
            zoom_sheet.paste(crop, (left, top + 34 + (174 - crop.height) // 2))
    zoom_path = review / "static-detail-zoom-sheet.png"
    zoom_sheet.save(zoom_path)
    return {
        "sourceNormalized": str(source_path),
        "handsOff": str(hands_off_path),
        "staticDetail": str(static_path),
        "staticDetailZoom": str(zoom_path),
        "reconstructedHighlight": str(review / "reconstructed-pixel-highlight.png"),
    }


def _deterministic_review(xml_path: Path, output_root: Path) -> dict[str, Any]:
    review = output_root / "human-review"
    time_entries: list[tuple[str, Path]] = []
    for time_value in REVIEW_TIMES:
        path = review / "deterministic" / "times" / f"{time_value.replace(':', '-')}.png"
        render_wff_xml(xml_path, path, fixed_time=time_value, fixed_date="2024-08-08")
        time_entries.append((time_value, path))
    time_atlas = review / "nine-time-render-atlas.png"
    _panel_atlas(time_entries, time_atlas)

    date_entries: list[tuple[str, Path]] = []
    for day in DATE_DAYS:
        path = review / "deterministic" / "dates" / f"day-{day:02d}.png"
        render_wff_xml(xml_path, path, fixed_time="10:08:30", fixed_date=f"2024-08-{day:02d}")
        date_entries.append((str(day), path))
    date_atlas = review / "date-values-render-atlas.png"
    _panel_atlas(date_entries, date_atlas, columns=5, tile_size=210)
    return {
        "timeEntries": time_entries,
        "dateEntries": date_entries,
        "timeAtlas": str(time_atlas),
        "dateAtlas": str(date_atlas),
    }


def _runtime_review(
    output_root: Path,
    deterministic: dict[str, Any],
    apk: Path,
    adb: Path | None,
    serial: str | None,
) -> dict[str, Any]:
    runtime = detect_runtime(adb)
    if runtime["status"] != "runtime_available":
        return runtime
    selected = serial or str(runtime["selectedDevice"])
    executable = str(adb) if adb is not None else str(runtime["adb"])
    review = output_root / "human-review"
    cases: list[tuple[str, int, Path]] = []
    for time_value in REVIEW_TIMES:
        cases.append((time_value, 8, review / "runtime" / "times" / f"{time_value.replace(':', '-')}.png"))
    for day in DATE_DAYS:
        cases.append(("10:08:30", day, review / "runtime" / "dates" / f"day-{day:02d}.png"))
    capture = capture_runtime_cases(executable, selected, apk, cases)

    runtime_times = {
        record["time"]: Path(record["path"])
        for record in capture["captures"][: len(REVIEW_TIMES)]
        if record["captureOk"]
    }
    runtime_dates = {
        str(record["date"]): Path(record["path"])
        for record in capture["captures"][len(REVIEW_TIMES) :]
        if record["captureOk"]
    }
    runtime_time_entries = [(time_value, runtime_times[time_value]) for time_value in REVIEW_TIMES if time_value in runtime_times]
    runtime_date_entries = [(str(day), runtime_dates[str(day)]) for day in DATE_DAYS if str(day) in runtime_dates]
    if runtime_time_entries:
        _panel_atlas(runtime_time_entries, review / "nine-time-runtime-atlas.png")
    if runtime_date_entries:
        _panel_atlas(runtime_date_entries, review / "date-values-runtime-atlas.png", columns=5, tile_size=210)
    _side_by_side_atlas(deterministic["timeEntries"], runtime_times, review / "nine-time-runtime-render-side-by-side.png")

    final_runtime = runtime_times.get("10:08:30")
    if final_runtime:
        preview = Image.new("RGB", (560, 620), "#101010")
        draw = ImageDraw.Draw(preview)
        image = Image.open(final_runtime).convert("RGB").resize((520, 520), Image.Resampling.LANCZOS)
        preview.paste(image, (20, 20))
        draw.text((20, 558), "Photo2WFF production port - Wear OS runtime", fill="#FFFFFF", font=_font(20, bold=True))
        draw.text((20, 586), "10:08:30 / day 8 / active watch face", fill="#B0B0B0", font=_font(16))
        preview.save(review / "final-full-watch-preview.png")
    return {
        "status": "runtime_verified" if all(record["captureOk"] and record["activeWatchFace"] for record in capture["captures"]) else "runtime_capture_failed",
        "device": selected,
        "capture": capture,
        "timeAtlas": str(review / "nine-time-runtime-atlas.png"),
        "dateAtlas": str(review / "date-values-runtime-atlas.png"),
        "sideBySide": str(review / "nine-time-runtime-render-side-by-side.png"),
        "finalPreview": str(review / "final-full-watch-preview.png"),
    }


def create_production_port(
    source_root: Path,
    output_root: Path,
    *,
    build: bool = True,
    capture: bool = True,
    adb: Path | None = None,
    serial: str | None = None,
    manual_glyph_dir: Path | None = None,
) -> dict[str, Any]:
    if output_root.exists() and any(output_root.iterdir()):
        raise ValueError(f"output directory is not empty: {output_root}")
    output_root.mkdir(parents=True, exist_ok=True)
    assets = output_root / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    review = output_root / "human-review"
    review.mkdir(parents=True, exist_ok=True)

    source_scene = load_scene(source_root / "scene.json")
    scene = _production_scene(source_scene)
    manual_glyphs = import_manual_glyphs(manual_glyph_dir, output_root)
    if manual_glyphs:
        date_slot = next(element for element in scene["elements"] if element["type"] == "DYNAMIC_SLOT")
        date_slot["manualGlyphs"] = manual_glyphs
    repair = _make_production_dial(scene, source_root, assets / "dial_complete.png")
    for name in PRODUCTION_ASSETS[1:]:
        shutil.copy2(source_root / "assets" / name, assets / name)
    font_source = source_root / "assets" / "fonts" / "pretendard.ttf"
    if font_source.exists():
        (assets / "fonts").mkdir(parents=True, exist_ok=True)
        shutil.copy2(font_source, assets / "fonts" / "pretendard.ttf")
    shutil.copy2(source_root / "assets" / "display_reference.png", assets / "source_normalized.png")

    save_scene(scene, output_root / "scene.json")
    render_scene(scene, output_root / "preview.png", output_root)
    project = output_root / "project"
    compile_project(scene, project, output_root)
    _copy_gradle_wrapper(project)
    xml_path = project / "watchface" / "src" / "main" / "res" / "raw" / "watchface.xml"
    official_validation = validate_wff_xml(xml_path)
    build_result = _build_project(project) if build else {"success": False, "status": "not_requested", "apk": None}

    static_review = _static_review(source_root, output_root, repair)
    deterministic = _deterministic_review(xml_path, output_root)
    runtime_result: dict[str, Any] = {"status": "not_requested"}
    if capture and build_result.get("success") and build_result.get("apk"):
        runtime_result = _runtime_review(output_root, deterministic, Path(str(build_result["apk"])), adb, serial)

    public_repair = {key: value for key, value in repair.items() if key != "_masks"}
    report = {
        "milestone": "Production Analog Port",
        "sourceBaseline": str(source_root),
        "structure": list(PRODUCTION_ASSETS) + ["dynamic date"],
        "staticDial": public_repair,
        "officialValidation": official_validation,
        "build": build_result,
        "runtime": runtime_result,
        "humanReview": {
            **static_review,
            "nineTimeRenderAtlas": deterministic["timeAtlas"],
            "dateRenderAtlas": deterministic["dateAtlas"],
        },
        "scope": {
            "automaticFontGeneration": False,
            "missingGlyphSynthesis": False,
            "numeralVectorization": False,
            "genericComplications": False,
            "newDisplayMapping": False,
            "opticalOffset": None,
            "manualGlyphOverride": manual_glyphs,
        },
    }
    _write_json(output_root / "production-report.json", report)
    return report
