from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from .analyzer import analyze_product_photo
from .compiler import compile_project
from .display_geometry import RoundedRect, boundary_normalized_map, inverse_raster_map, map_analog_hand, map_element_preserving
from .dynamic_text import extract_center_dynamic_text
from .generic_fixtures import run_generic_fixtures
from .human_review import REVIEW_TIMES
from .model import save_scene, validate_scene
from .occlusion import _hand_masks, _source_foreground_mask
from .perimeter_artwork import decompose_perimeter_artwork, draw_perimeter_overlay, remove_perimeter_artwork
from .production_port import _build_project, _copy_gradle_wrapper, _panel_atlas
from .render import render_scene
from .runtime_validation import capture_runtime_cases, detect_runtime
from .wff_render import render_wff_xml
from .wff_validate import validate_wff_xml

DATE_DAYS = (1, 8, 11, 20, 31)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def _copy_font(output_root: Path) -> None:
    packaged = Path(__file__).resolve().parent.parent / "assets" / "fonts" / "pretendard.ttf"
    if packaged.exists():
        destination = output_root / "assets" / "fonts" / "pretendard.ttf"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(packaged, destination)


def _artwork_sheet(asset_root: Path, destination: Path) -> None:
    paths = sorted(asset_root.glob("*.png"))
    tile = 112
    sheet = Image.new("RGB", (tile * 4, tile * max(1, (len(paths) + 3) // 4)), "#151515")
    draw = ImageDraw.Draw(sheet)
    for index, path in enumerate(paths):
        image = Image.open(path).convert("RGBA")
        image.thumbnail((80, 76), Image.Resampling.LANCZOS)
        left = (index % 4) * tile
        top = (index // 4) * tile
        panel = Image.new("RGBA", (tile, tile), (21, 21, 21, 255))
        panel.alpha_composite(image, ((tile - image.width) // 2, 8))
        sheet.paste(panel.convert("RGB"), (left, top))
        draw.text((left + 6, top + 88), path.stem, fill="white", font=ImageFont.load_default())
    destination.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(destination)


def _mapping_comparison(raster: Image.Image, preserved: Image.Image, destination: Path) -> None:
    sheet = Image.new("RGB", (900, 480), "#151515")
    sheet.paste(raster.convert("RGB"), (4, 36))
    sheet.paste(preserved.convert("RGB"), (458, 36))
    draw = ImageDraw.Draw(sheet)
    draw.text((8, 10), "A. RASTER_WARP", fill="white", font=ImageFont.load_default())
    draw.text((462, 10), "B. ELEMENT_PRESERVING", fill="white", font=ImageFont.load_default())
    sheet.save(destination)


def _materialize_artwork(
    elements: list[dict[str, Any]], source_root: Path, output_root: Path, source: RoundedRect, target: RoundedRect
) -> tuple[list[dict[str, Any]], Image.Image]:
    mapped_elements: list[dict[str, Any]] = []
    preview = Image.new("RGBA", (438, 438), (0, 0, 0, 0))
    asset_root = output_root / "assets" / "perimeter"
    asset_root.mkdir(parents=True, exist_ok=True)
    for index, element in enumerate(elements):
        mapping = map_element_preserving(element, source, target)
        original = Image.open(source_root / element["asset"]).convert("RGBA")
        scale = float(mapping["uniformScale"])
        scaled = original.resize((max(1, round(original.width * scale)), max(1, round(original.height * scale))), Image.Resampling.LANCZOS)
        transformed = scaled.rotate(-float(mapping["rotation"]), expand=True, resample=Image.Resampling.BICUBIC)
        anchor = mapping["targetAnchor"]
        left = round(anchor["x"] - transformed.width / 2)
        top = round(anchor["y"] - transformed.height / 2)
        visible_left = max(0, left)
        visible_top = max(0, top)
        visible_right = min(438, left + transformed.width)
        visible_bottom = min(438, top + transformed.height)
        if visible_left >= visible_right or visible_top >= visible_bottom:
            continue
        if (visible_left, visible_top, visible_right, visible_bottom) != (left, top, left + transformed.width, top + transformed.height):
            transformed = transformed.crop((visible_left - left, visible_top - top, visible_right - left, visible_bottom - top))
            left, top = visible_left, visible_top
        asset = f"assets/perimeter/artwork_{index:02d}.png"
        transformed.save(output_root / asset)
        preview.alpha_composite(transformed, (left, top))
        mapped_elements.append(
            {
                **copy.deepcopy(element),
                "bbox": {"x": left, "y": top, "width": transformed.width, "height": transformed.height},
                "anchor": {"x": round(anchor["x"], 4), "y": round(anchor["y"], 4)},
                "asset": asset,
                "mappingMode": "ELEMENT_PRESERVING",
                "relationships": {**element.get("relationships", {}), "sourceAsset": element["asset"], "mappedUniformScale": round(scale, 6)},
                "zIndex": 2,
            }
        )
    return mapped_elements, preview


def _mapped_dynamic_elements(elements: list[dict[str, Any]], source: RoundedRect, target: RoundedRect) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for element in elements:
        mapped = map_element_preserving(element, source, target)
        clone = copy.deepcopy(element)
        clone["bbox"] = {key: round(value) for key, value in mapped["targetBbox"].items()}
        clone["mappingMode"] = "ELEMENT_PRESERVING"
        result.append(clone)
    return result


def _mapped_hands(source_scene: dict[str, Any], source_root: Path, output_root: Path, source: RoundedRect, target: RoundedRect) -> tuple[list[dict[str, Any]], tuple[float, float]]:
    source_center = (float(source_scene["clock"]["centerX"]), float(source_scene["clock"]["centerY"]))
    target_center = boundary_normalized_map(source_center, source, target)
    mapped: list[dict[str, Any]] = []
    for element in source_scene["elements"]:
        if element["type"] != "ANALOG_HAND":
            continue
        geometry = map_analog_hand(element, source, target, source_center, target_center)
        ratio = geometry["targetLength"] / max(1.0, float(element["length"]))
        original = Image.open(source_root / element["asset"]).convert("RGBA")
        resized = original.resize((max(1, round(original.width * ratio)), max(1, round(original.height * ratio))), Image.Resampling.LANCZOS)
        asset = f"assets/{element['role'].lower()}_hand.png"
        resized.save(output_root / asset)
        clone = copy.deepcopy(element)
        clone["asset"] = asset
        clone["length"] = round(float(element["length"]) * ratio, 4)
        clone["thickness"] = round(float(element["thickness"]) * ratio, 4)
        clone["bbox"] = {
            "x": round(target_center[0] - float(element["pivotX"]) * resized.width),
            "y": round(target_center[1] - float(element["pivotY"]) * resized.height),
            "width": resized.width,
            "height": resized.height,
        }
        clone["relationships"] = {"mapping": geometry, "pipeline": "existing A1 analog hand extraction"}
        mapped.append(clone)
    return mapped, target_center


def _mapped_center_cap(source_scene: dict[str, Any], source_root: Path, output_root: Path, target_center: tuple[float, float]) -> dict[str, Any]:
    cap = next(element for element in source_scene["elements"] if element["id"] == "center_cap")
    source_asset = Image.open(source_root / cap["asset"]).convert("RGBA")
    asset = "assets/center_cap.png"
    source_asset.save(output_root / asset)
    clone = copy.deepcopy(cap)
    clone["asset"] = asset
    clone["bbox"] = {"x": round(target_center[0] - source_asset.width / 2), "y": round(target_center[1] - source_asset.height / 2), "width": source_asset.width, "height": source_asset.height}
    return clone


def _hand_cleanup_checks(source_scene: dict[str, Any], raw_root: Path) -> dict[str, Any]:
    reference = Image.open(raw_root / "reference.png").convert("RGB")
    completed = Image.open(raw_root / "assets" / "dial-completed.png").convert("RGB")
    hands = {element["role"]: element for element in source_scene["elements"] if element["type"] == "ANALOG_HAND"}
    masks = _hand_masks(reference, (float(source_scene["clock"]["centerX"]), float(source_scene["clock"]["centerY"])), hands, assets_dir=raw_root / "assets", margin=1)
    source_foreground = _source_foreground_mask(reference)
    completed_foreground = _source_foreground_mask(completed)
    records: dict[str, Any] = {}
    for role, mask in masks.items():
        source_count = 0
        remaining_count = 0
        red_remaining = 0
        for y in range(reference.height):
            for x in range(reference.width):
                if not mask.getpixel((x, y)):
                    continue
                if source_foreground.getpixel((x, y)):
                    source_count += 1
                if completed_foreground.getpixel((x, y)):
                    remaining_count += 1
                red, green, blue = completed.getpixel((x, y))
                if red >= 65 and red >= green * 1.25 and red >= blue * 1.15:
                    red_remaining += 1
        records[role] = {"sourceForegroundPixels": source_count, "remainingForegroundPixels": remaining_count, "remainingForegroundRatio": round(remaining_count / max(1, source_count), 4), "remainingRedPixels": red_remaining, "maskBbox": list(mask.getbbox() or (0, 0, 0, 0))}
    return {"roles": records, "redResidualPass": all(record["remainingRedPixels"] == 0 for record in records.values()), "whiteForegroundRatioPass": all(record["remainingForegroundRatio"] <= 0.25 for role, record in records.items() if role != "SECOND")}


def _scene(source_scene: dict[str, Any], static_elements: list[dict[str, Any]], dynamic: list[dict[str, Any]], hands: list[dict[str, Any]], cap: dict[str, Any], target_center: tuple[float, float], mode: str) -> dict[str, Any]:
    scene = copy.deepcopy(source_scene)
    scene["clock"] = {**scene["clock"], "centerX": round(target_center[0], 4), "centerY": round(target_center[1], 4)}
    scene["elements"] = static_elements + dynamic + hands + [cap]
    scene["displayGeometry"]["mappingPolicy"] = mode
    scene["displayGeometry"]["availableMappings"] = ["RASTER_WARP", "ELEMENT_PRESERVING"]
    scene["analysis"]["method"] = "generic perimeter decomposition + existing analog pipeline"
    scene["analysis"]["requiresHumanReview"] = True
    validate_scene(scene)
    return scene


def _render_review(scene: dict[str, Any], root: Path, review: Path, prefix: str) -> dict[str, Any]:
    entries = []
    for value in REVIEW_TIMES:
        clone = copy.deepcopy(scene)
        clone["preview"]["time"] = value
        path = review / f"{prefix}-{value.replace(':', '-')}.png"
        render_scene(clone, path, root)
        entries.append((value, path))
    atlas = review / f"{prefix}-nine-time-atlas.png"
    _panel_atlas(entries, atlas)
    date_entries = []
    for day in DATE_DAYS:
        clone = copy.deepcopy(scene)
        clone["preview"]["date"] = f"2024-08-{day:02d}"
        clone["preview"]["weekday"] = "TUE"
        path = review / f"date-{day}.png"
        render_scene(clone, path, root)
        date_entries.append((str(day), path))
    date_atlas = review / "weekday-date-review-atlas.png"
    _panel_atlas(date_entries, date_atlas, columns=5, tile_size=210)
    return {"timeAtlas": str(atlas), "dateAtlas": str(date_atlas), "entries": entries}


def run_perimeter_benchmark(reference: Path, output_root: Path, *, build: bool = True, capture: bool = True, adb: Path | None = None, serial: str | None = None) -> dict[str, Any]:
    if output_root.exists() and any(output_root.iterdir()):
        raise ValueError(f"output directory is not empty: {output_root}")
    output_root.mkdir(parents=True)
    fixtures = run_generic_fixtures(output_root / "generic-fixtures")
    if not fixtures["systemPass"]:
        raise RuntimeError("generic fixture gate failed; target benchmark was not started")

    raw = output_root / "raw-analysis"
    source_scene = analyze_product_photo(reference, raw)
    save_scene(source_scene, raw / "scene.json")
    source = RoundedRect.from_dict(source_scene["displayGeometry"]["source"])
    target = RoundedRect.from_dict(source_scene["displayGeometry"]["target"])
    review = output_root / "human-review"
    review.mkdir()
    assets = output_root / "assets"
    assets.mkdir()
    _copy_font(output_root)

    normalized = Image.open(raw / "reference.png").convert("RGB")
    normalized.save(review / "source-normalized.png")
    completed_path = raw / "assets" / "dial-completed.png"
    completed = Image.open(completed_path if completed_path.exists() else raw / "assets" / "dial_clean.png").convert("RGB")
    hand_mask_path = raw / "assets" / "hand-occlusion-mask.png"
    hand_mask = Image.open(hand_mask_path).convert("L") if hand_mask_path.exists() else None
    dynamic_report = extract_center_dynamic_text(normalized, output_root / "dynamic-text", exclusion_mask=hand_mask, reconstruction_image=completed)
    dynamic_removed = Image.open(dynamic_report["cleanBackground"]).convert("RGB")
    dynamic_removed.save(review / "dynamic-text-removal-result.png")
    perimeter_report = decompose_perimeter_artwork(dynamic_removed, source, output_root / "perimeter-decomposition")
    hand_cleanup = _hand_cleanup_checks(source_scene, raw)
    draw_perimeter_overlay(normalized, perimeter_report["elements"], review / "detected-perimeter-elements-overlay.png")
    if (raw / "assets" / "hand-mask-overlay.png").exists():
        shutil.copy2(raw / "assets" / "hand-mask-overlay.png", review / "source-hand-mask-overlay.png")
    shutil.copy2(perimeter_report["unwrap"], review / "perimeter-sd-unwrap.png")

    raster_dial = inverse_raster_map(dynamic_removed, source, target).convert("RGBA")
    raster_dial.save(assets / "dial-raster-warp.png")
    perimeter_mask = Image.open(perimeter_report["mask"]).convert("L")
    base_without_perimeter = remove_perimeter_artwork(dynamic_removed, perimeter_mask)
    element_base = inverse_raster_map(base_without_perimeter, source, target).convert("RGBA")
    element_base.save(assets / "dial-element-base.png")
    mapped_artwork, artwork_preview = _materialize_artwork(perimeter_report["elements"], output_root / "perimeter-decomposition", output_root, source, target)
    combined = element_base.copy()
    combined.alpha_composite(artwork_preview)
    combined.convert("RGB").save(review / "element-preserving-result.png")
    raster_dial.convert("RGB").save(review / "raster-warp-result.png")
    combined.convert("RGB").save(review / "hands-off-static-dial.png")
    _artwork_sheet(output_root / "perimeter-decomposition" / "assets" / "perimeter", review / "extracted-artwork-sheet.png")
    _mapping_comparison(raster_dial, combined, review / "mapping-mode-side-by-side.png")

    dynamic = _mapped_dynamic_elements(dynamic_report["elements"], source, target)
    hands, target_center = _mapped_hands(source_scene, raw, output_root, source, target)
    cap = _mapped_center_cap(source_scene, raw, output_root, target_center)
    raster_static = [{"id": "dial_raster_warp", "type": "STATIC_IMAGE", "dynamic": False, "bbox": {"x": 0, "y": 0, "width": 438, "height": 438}, "asset": "assets/dial-raster-warp.png", "confidence": 1.0, "zIndex": 0, "mappingMode": "RASTER_WARP"}]
    element_static = [{"id": "dial_element_base", "type": "STATIC_IMAGE", "dynamic": False, "bbox": {"x": 0, "y": 0, "width": 438, "height": 438}, "asset": "assets/dial-element-base.png", "confidence": 1.0, "zIndex": 0, "mappingMode": "RASTER_WARP"}] + mapped_artwork
    raster_scene = _scene(source_scene, raster_static, dynamic, hands, cap, target_center, "RASTER_WARP")
    element_scene = _scene(source_scene, element_static, dynamic, hands, cap, target_center, "ELEMENT_PRESERVING")
    save_scene(raster_scene, output_root / "scene.raster-warp.json")
    save_scene(element_scene, output_root / "scene.element-preserving.json")

    projects: dict[str, Any] = {}
    for mode, scene in (("raster-warp", raster_scene), ("element-preserving", element_scene)):
        project = output_root / f"project-{mode}"
        render_scene(scene, output_root / "preview.png", output_root)
        compile_project(scene, project, output_root)
        _copy_gradle_wrapper(project)
        xml = project / "watchface" / "src" / "main" / "res" / "raw" / "watchface.xml"
        validation = validate_wff_xml(xml)
        build_result = _build_project(project) if build else {"success": False, "status": "not_requested", "apk": None}
        projects[mode] = {"xml": str(xml), "officialValidation": validation, "build": build_result}

    review_report = _render_review(element_scene, output_root, review, "element-preserving")
    full_preview = Image.new("RGB", (560, 610), "#101010")
    preview = Image.open(review_report["entries"][3][1]).convert("RGB").resize((520, 520), Image.Resampling.LANCZOS)
    full_preview.paste(preview, (20, 20))
    ImageDraw.Draw(full_preview).text((20, 557), "Generic perimeter analog benchmark", fill="white", font=ImageFont.load_default())
    full_preview.save(review / "final-full-watch-preview.png")

    runtime: dict[str, Any] = {"status": "not_requested"}
    built = projects["element-preserving"]["build"]
    if capture and built.get("success") and built.get("apk"):
        environment = detect_runtime(adb)
        selected = serial or environment.get("wearDevices", [None])[0]
        if environment.get("status") == "runtime_available" and selected:
            cases = [(value, 14, review / f"runtime-{value.replace(':', '-')}.png") for value in REVIEW_TIMES]
            runtime = capture_runtime_cases(environment["adb"], selected, Path(built["apk"]), cases)
            runtime["status"] = "runtime_verified" if all(item["captureOk"] and item["activeWatchFace"] for item in runtime["captures"]) else "runtime_capture_failed"
            runtime_entries = [(record["time"], Path(record["path"])) for record in runtime["captures"] if record["captureOk"]]
            if runtime_entries:
                runtime_atlas = review / "runtime-nine-time-atlas.png"
                _panel_atlas(runtime_entries, runtime_atlas)
                runtime["nineTimeAtlas"] = str(runtime_atlas)
        else:
            runtime = {"status": "blocked_by_runtime_environment", "environment": environment}

    official_pass = all("PASSED" in record["officialValidation"] for record in projects.values())
    build_pass = all(record["build"].get("success") for record in projects.values()) if build else True
    automated_pipeline_pass = len(perimeter_report["elements"]) > 0 and dynamic_report["detected"] and len(hands) == 3 and official_pass and build_pass and runtime.get("status") == "runtime_verified"
    benchmark_pass = automated_pipeline_pass and hand_cleanup["redResidualPass"] and hand_cleanup["whiteForegroundRatioPass"]
    report = {
        "milestone": "General Rounded-Rectangle Perimeter Analog Support",
        "system": {"pass": fixtures["systemPass"], "fixtures": fixtures, "officialValidatorPass": official_pass, "buildPass": build_pass},
        "benchmark": {"pass": benchmark_pass, "automatedPipelinePass": automated_pipeline_pass, "humanReviewStatus": "required", "reference": str(reference), "perimeterElements": perimeter_report["elementCount"], "dynamicText": dynamic_report, "handCleanup": hand_cleanup, "hands": [element["role"] for element in hands], "runtime": runtime},
        "projects": projects,
        "humanReview": {"sourceNormalized": str(review / "source-normalized.png"), "sourceHandMaskOverlay": str(review / "source-hand-mask-overlay.png"), "perimeterOverlay": str(review / "detected-perimeter-elements-overlay.png"), "perimeterSdUnwrap": str(review / "perimeter-sd-unwrap.png"), "extractedArtworkSheet": str(review / "extracted-artwork-sheet.png"), "rasterWarp": str(review / "raster-warp-result.png"), "elementPreserving": str(review / "element-preserving-result.png"), "mappingSideBySide": str(review / "mapping-mode-side-by-side.png"), "handsOff": str(review / "hands-off-static-dial.png"), "dynamicTextRemoval": str(review / "dynamic-text-removal-result.png"), "nineTimeAtlas": review_report["timeAtlas"], "weekdayDateAtlas": review_report["dateAtlas"], "runtimeNineTimeAtlas": runtime.get("nineTimeAtlas"), "finalPreview": str(review / "final-full-watch-preview.png")},
        "overfittingGuard": {"targetReferenceUsedForFixtureCalibration": False, "referenceSpecificCoordinatesInImplementation": False},
    }
    _write_json(output_root / "perimeter-benchmark-report.json", report)
    return report
