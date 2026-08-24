from __future__ import annotations

import copy
import json
import math
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from .compiler import compile_project
from .runtime_validation import capture_runtime_image, detect_runtime
from .wff_render import render_wff_xml
from .wff_validate import validate_wff_xml

CANVAS_SIZE = 438
DIAGNOSTIC_TIME = "10:08:30"
DIAGNOSTIC_DAY = 8
TEXT_DAYS = (1, 8, 11, 20, 31)
HAND_TIMES = ("00:00:00", "03:15:45", "06:30:00", "10:08:30")
FIDUCIALS = (
    ("center", (219, 219), (255, 255, 255)),
    ("north", (219, 50), (255, 40, 40)),
    ("east", (388, 219), (40, 255, 40)),
    ("south", (219, 388), (40, 80, 255)),
    ("west", (50, 219), (255, 220, 40)),
    ("north_west", (100, 100), (255, 40, 220)),
    ("north_east", (338, 100), (40, 255, 220)),
    ("south_east", (338, 338), (255, 140, 40)),
    ("south_west", (100, 338), (150, 80, 255)),
)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def _diagnostic_scene(source: dict[str, Any], elements: list[dict[str, Any]]) -> dict[str, Any]:
    scene = copy.deepcopy(source)
    scene["background"] = {"type": "SOLID", "color": "#000000"}
    scene["elements"] = elements
    scene["analysis"] = {
        "watchFaceCategory": "MINIMAL_ANALOG",
        "overallConfidence": 1.0,
        "requiresStaticAssetExtraction": False,
        "requiresHumanReview": False,
    }
    scene["preview"] = {"time": DIAGNOSTIC_TIME, "date": "08.08", "weekday": "THU"}
    return scene


def _geometry_source(output_root: Path) -> tuple[Path, dict[str, Any]]:
    source = output_root / "sources" / "geometry"
    assets = source / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGBA", (CANVAS_SIZE, CANVAS_SIZE), (0, 0, 0, 255))
    draw = ImageDraw.Draw(image)
    for _, (x, y), color in FIDUCIALS:
        draw.ellipse((x - 6, y - 6, x + 6, y + 6), fill=(*color, 255))
        draw.line((x - 10, y, x + 10, y), fill=(*color, 255), width=2)
        draw.line((x, y - 10, x, y + 10), fill=(*color, 255), width=2)
    image.save(assets / "geometry_fiducials.png")
    image.save(source / "preview.png")
    metadata = {
        name: {"logical": {"x": point[0], "y": point[1]}, "color": list(color)}
        for name, point, color in FIDUCIALS
    }
    return source, metadata


def _static_hand_elements(scene: dict[str, Any], source_root: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    elements: list[dict[str, Any]] = []
    metadata: dict[str, Any] = {}
    x_positions = (80, 205, 340)
    hands = [element for element in scene.get("elements", []) if element.get("type") == "ANALOG_HAND"]
    for x, hand in zip(x_positions, hands):
        bbox = hand["bbox"]
        width = int(bbox["width"])
        height = int(bbox["height"])
        y = round((CANVAS_SIZE - height) / 2)
        element = {
            "id": f"diagnostic_{str(hand['role']).lower()}",
            "type": "STATIC_IMAGE",
            "dynamic": False,
            "bbox": {"x": x - width // 2, "y": y, "width": width, "height": height},
            "asset": hand["asset"],
            "confidence": 1.0,
        }
        with Image.open(source_root / hand["asset"]) as source_asset:
            asset = source_asset.convert("RGBA")
        alpha_bbox = asset.getchannel("A").getbbox() or (0, 0, asset.width, asset.height)
        metadata[str(hand["role"])] = {
            "elementBbox": element["bbox"],
            "assetAlphaBbox": list(alpha_bbox),
            "expectedForegroundSize": {
                "width": alpha_bbox[2] - alpha_bbox[0],
                "height": alpha_bbox[3] - alpha_bbox[1],
            },
        }
        elements.append(element)
    return elements, metadata


def _compile_diagnostic(name: str, scene: dict[str, Any], source_root: Path, output_root: Path) -> dict[str, Any]:
    root = output_root / name
    scene_path = root / "scene.json"
    _write_json(scene_path, scene)
    project = root / "project"
    compile_project(scene, project, source_root)
    wrapper_root = Path(__file__).resolve().parent.parent / "templates" / "gradle"
    for wrapper in (wrapper_root / "gradlew", wrapper_root / "gradlew.bat"):
        if wrapper.exists():
            shutil.copy2(wrapper, project / wrapper.name)
    if (wrapper_root / "wrapper").exists():
        shutil.copytree(wrapper_root / "wrapper", project / "gradle" / "wrapper", dirs_exist_ok=True)
    xml_path = project / "watchface" / "src" / "main" / "res" / "raw" / "watchface.xml"
    validation = validate_wff_xml(xml_path)
    return {"name": name, "root": str(root), "scene": str(scene_path), "project": str(project), "xml": str(xml_path), "officialValidation": validation}


def _build_project(project: Path) -> dict[str, Any]:
    project = project.resolve()
    wrapper = project / "gradlew.bat"
    result = subprocess.run(
        [str(wrapper), "assembleDebug"],
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


def _solve_three_by_three(matrix: list[list[float]], values: list[float]) -> list[float]:
    augmented = [row[:] + [value] for row, value in zip(matrix, values)]
    for column in range(3):
        pivot = max(range(column, 3), key=lambda row: abs(augmented[row][column]))
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        if abs(divisor) < 1e-9:
            raise ValueError("fiducial fit is singular")
        augmented[column] = [value / divisor for value in augmented[column]]
        for row in range(3):
            if row == column:
                continue
            factor = augmented[row][column]
            augmented[row] = [left - factor * right for left, right in zip(augmented[row], augmented[column])]
    return [augmented[index][3] for index in range(3)]


def _least_squares(features: list[tuple[float, float, float]], values: list[float]) -> list[float]:
    matrix = [[sum(row[i] * row[j] for row in features) for j in range(3)] for i in range(3)]
    vector = [sum(row[i] * value for row, value in zip(features, values)) for i in range(3)]
    return _solve_three_by_three(matrix, vector)


def _detect_fiducials(runtime_path: Path) -> dict[str, tuple[float, float]]:
    image = Image.open(runtime_path).convert("RGB")
    pixels = image.load()
    detected: dict[str, tuple[float, float]] = {}
    for name, _, color in FIDUCIALS:
        points = []
        for y in range(image.height):
            for x in range(image.width):
                pixel = pixels[x, y]
                distance = math.sqrt(sum((pixel[index] - color[index]) ** 2 for index in range(3)))
                if distance <= 30:
                    points.append((x, y))
        if points:
            detected[name] = (
                sum(point[0] for point in points) / len(points),
                sum(point[1] for point in points) / len(points),
            )
    return detected


def _fit_geometry(runtime_path: Path) -> dict[str, Any]:
    detected = _detect_fiducials(runtime_path)
    logical_by_name = {name: point for name, point, _ in FIDUCIALS}
    names = [name for name in logical_by_name if name in detected]
    if len(names) < 4:
        return {"status": "insufficient_fiducials", "detected": {name: list(point) for name, point in detected.items()}}
    features = [(logical_by_name[name][0], logical_by_name[name][1], 1.0) for name in names]
    x_coefficients = _least_squares(features, [detected[name][0] for name in names])
    y_coefficients = _least_squares(features, [detected[name][1] for name in names])
    a, b, tx = x_coefficients
    c, d, ty = y_coefficients
    residuals = []
    observations = {}
    for name in names:
        x, y = logical_by_name[name]
        predicted = (a * x + b * y + tx, c * x + d * y + ty)
        observed = detected[name]
        residual = math.hypot(predicted[0] - observed[0], predicted[1] - observed[1])
        residuals.append(residual)
        observations[name] = {"logical": [x, y], "runtime": list(observed), "predicted": list(predicted), "residualPx": round(residual, 4)}
    with Image.open(runtime_path) as runtime_image:
        image_width, image_height = runtime_image.size
    mapped_center = (a * CANVAS_SIZE / 2 + b * CANVAS_SIZE / 2 + tx, c * CANVAS_SIZE / 2 + d * CANVAS_SIZE / 2 + ty)
    framebuffer_center = (image_width / 2, image_height / 2)
    return {
        "status": "measured",
        "matrix": {"a": a, "b": b, "c": c, "d": d, "tx": tx, "ty": ty},
        "scaleX": round(math.hypot(a, c), 8),
        "scaleY": round(math.hypot(b, d), 8),
        "affineTranslation": {"x": round(tx, 4), "y": round(ty, 4)},
        "mappedLogicalCenter": {"x": round(mapped_center[0], 4), "y": round(mapped_center[1], 4)},
        "framebufferCenter": {"x": framebuffer_center[0], "y": framebuffer_center[1]},
        "centerOffset": {
            "x": round(mapped_center[0] - framebuffer_center[0], 4),
            "y": round(mapped_center[1] - framebuffer_center[1], 4),
        },
        "rotationDeg": round(math.degrees(math.atan2(c, a)), 6),
        "fitResidualRmsPx": round(math.sqrt(sum(value * value for value in residuals) / len(residuals)), 4),
        "fiducialCount": len(names),
        "observations": observations,
        "framebufferSizeUsedAsMeasurement": False,
    }


def _map_point(transform: dict[str, Any], x: float, y: float) -> tuple[float, float]:
    matrix = transform["matrix"]
    return (
        matrix["a"] * x + matrix["b"] * y + matrix["tx"],
        matrix["c"] * x + matrix["d"] * y + matrix["ty"],
    )


def _inverse_map_point(transform: dict[str, Any], x: float, y: float) -> tuple[float, float]:
    matrix = transform["matrix"]
    determinant = matrix["a"] * matrix["d"] - matrix["b"] * matrix["c"]
    if abs(determinant) < 1e-12:
        raise ValueError("geometry transform is singular")
    translated_x = x - matrix["tx"]
    translated_y = y - matrix["ty"]
    return (
        (matrix["d"] * translated_x - matrix["b"] * translated_y) / determinant,
        (-matrix["c"] * translated_x + matrix["a"] * translated_y) / determinant,
    )


def _foreground_bbox(image: Image.Image, box: tuple[int, int, int, int]) -> tuple[int, int, int, int] | None:
    rgb = image.convert("RGB")
    points = []
    for y in range(max(0, box[1]), min(rgb.height, box[3])):
        for x in range(max(0, box[0]), min(rgb.width, box[2])):
            if max(rgb.getpixel((x, y))) >= 28:
                points.append((x, y))
    if not points:
        return None
    return min(x for x, _ in points), min(y for _, y in points), max(x for x, _ in points) + 1, max(y for _, y in points) + 1


def _measure_static_hands(runtime_path: Path, transform: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    image = Image.open(runtime_path).convert("RGB")
    scale_x = transform["scaleX"]
    scale_y = transform["scaleY"]
    measured = {}
    for role, item in metadata.items():
        bbox = item["elementBbox"]
        corners = [
            _map_point(transform, bbox["x"], bbox["y"]),
            _map_point(transform, bbox["x"] + bbox["width"], bbox["y"] + bbox["height"]),
        ]
        margin = 6
        search = (
            math.floor(min(point[0] for point in corners)) - margin,
            math.floor(min(point[1] for point in corners)) - margin,
            math.ceil(max(point[0] for point in corners)) + margin,
            math.ceil(max(point[1] for point in corners)) + margin,
        )
        observed = _foreground_bbox(image, search)
        expected = item["expectedForegroundSize"]
        if observed is None:
            measured[role] = {"status": "not_detected", "pivotErrorPx": None}
            continue
        observed_width = (observed[2] - observed[0]) / scale_x
        observed_height = (observed[3] - observed[1]) / scale_y
        measured[role] = {
            "status": "measured",
            "expectedForegroundSize": expected,
            "observedForegroundSizeLogicalPx": {"width": round(observed_width, 4), "height": round(observed_height, 4)},
            "widthDifferencePx": round(observed_width - expected["width"], 4),
            "lengthDifferencePx": round(observed_height - expected["height"], 4),
            "runtimeForegroundBbox": list(observed),
            "pivotErrorPx": None,
            "pivotStatus": "unmeasured",
        }
    return measured


def _hand_angle_from_black_scene(runtime_path: Path, transform: dict[str, Any]) -> float | None:
    image = Image.open(runtime_path).convert("RGB")
    center = _map_point(transform, CANVAS_SIZE / 2, CANVAS_SIZE / 2)
    weighted_x = 0.0
    weighted_y = 0.0
    total_weight = 0.0
    for y in range(image.height):
        for x in range(image.width):
            if max(image.getpixel((x, y))) < 28:
                continue
            dx = x - center[0]
            dy = y - center[1]
            radius = math.hypot(dx, dy)
            if radius < 14:
                continue
            weighted_x += dx * radius
            weighted_y += dy * radius
            total_weight += radius
    if total_weight == 0:
        return None
    return round(math.degrees(math.atan2(weighted_x, -weighted_y)) % 360, 4)


def _base_hand_angle(role: str, time_value: str) -> float:
    hour, minute, second = (int(part) for part in time_value.split(":"))
    if role == "HOUR":
        return ((hour % 12) + minute / 60 + second / 3600) * 30
    if role == "MINUTE":
        return (minute + second / 60) * 6
    return second * 6


def _angle_interval(role: str, time_value: str, bracket: list[int] | None) -> dict[str, Any]:
    base = _base_hand_angle(role, time_value)
    rate = {"HOUR": 30 / 3_600_000, "MINUTE": 6 / 60_000, "SECOND": 6 / 1_000}[role]
    if not bracket:
        return {"startDeg": round(base, 6), "endDeg": round(base, 6), "uncertaintyDeg": None}
    start = base + bracket[0] * rate
    end = base + bracket[1] * rate
    return {"startDeg": round(start % 360, 6), "endDeg": round(end % 360, 6), "uncertaintyDeg": round((bracket[1] - bracket[0]) * rate, 6)}


def _circular_distance(left: float, right: float) -> float:
    return abs((left - right + 180) % 360 - 180)


def _angle_error_to_interval(observed: float | None, interval: dict[str, Any]) -> float | None:
    if observed is None:
        return None
    start = interval["startDeg"]
    end = interval["endDeg"]
    span = (end - start) % 360
    position = (observed - start) % 360
    if position <= span:
        return 0.0
    return min(_circular_distance(observed, start), _circular_distance(observed, end))


def _measure_text(runtime_path: Path, deterministic_path: Path, transform: dict[str, Any], bbox: dict[str, Any]) -> dict[str, Any]:
    corners = [
        _map_point(transform, bbox["x"], bbox["y"]),
        _map_point(transform, bbox["x"] + bbox["width"], bbox["y"] + bbox["height"]),
    ]
    search = (
        math.floor(min(point[0] for point in corners)),
        math.floor(min(point[1] for point in corners)),
        math.ceil(max(point[0] for point in corners)),
        math.ceil(max(point[1] for point in corners)),
    )
    observed = _foreground_bbox(Image.open(runtime_path).convert("RGB"), search)
    logical_search = (bbox["x"], bbox["y"], bbox["x"] + bbox["width"], bbox["y"] + bbox["height"])
    deterministic = _foreground_bbox(Image.open(deterministic_path).convert("RGB"), logical_search)
    observed_logical = None
    if observed:
        top_left = _inverse_map_point(transform, observed[0], observed[1])
        bottom_right = _inverse_map_point(transform, observed[2], observed[3])
        observed_logical = (top_left[0], top_left[1], bottom_right[0], bottom_right[1])
    slot_center = (bbox["x"] + bbox["width"] / 2, bbox["y"] + bbox["height"] / 2)

    def center(box: tuple[float, float, float, float]) -> tuple[float, float]:
        return ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)

    runtime_center = center(observed_logical) if observed_logical else None
    deterministic_center = center(deterministic) if deterministic else None
    return {
        "visible": observed is not None,
        "runtimeForegroundBbox": list(observed) if observed else None,
        "runtimeForegroundBboxLogical": [round(value, 4) for value in observed_logical] if observed_logical else None,
        "deterministicForegroundBboxLogical": list(deterministic) if deterministic else None,
        "expectedRuntimeRoi": list(search),
        "clipped": bool(observed and (observed[0] <= search[0] or observed[1] <= search[1] or observed[2] >= search[2] or observed[3] >= search[3])),
        "runtimeCenteringErrorLogicalPx": {
            "x": round(runtime_center[0] - slot_center[0], 4),
            "y": round(runtime_center[1] - slot_center[1], 4),
        } if runtime_center else None,
        "runtimeVsDeterministicCenterDifferencePx": {
            "x": round(runtime_center[0] - deterministic_center[0], 4),
            "y": round(runtime_center[1] - deterministic_center[1], 4),
        } if runtime_center and deterministic_center else None,
        "runtimeVsDeterministicBaselineDifferencePx": round(observed_logical[3] - deterministic[3], 4) if observed_logical and deterministic else None,
    }


def _write_text_atlas(captures: list[dict[str, Any]], destination: Path) -> None:
    valid = [capture for capture in captures if capture.get("capture", {}).get("captureOk")]
    if not valid:
        return
    cell_width = 240
    cell_height = 120
    atlas = Image.new("RGB", (cell_width * 5, cell_height * 2), (16, 16, 16))
    draw = ImageDraw.Draw(atlas)
    for index, item in enumerate(valid):
        image = Image.open(item["capture"]["path"]).convert("RGB")
        bbox = item["measurement"].get("expectedRuntimeRoi")
        crop = image.crop(tuple(bbox)).resize((180, 80), Image.Resampling.NEAREST) if bbox else image.resize((180, 80))
        x = (index % 5) * cell_width
        y = (index // 5) * cell_height
        atlas.paste(crop, (x + 30, y + 5))
        draw.text((x + 8, y + 90), f"{item['mode']} day {item['day']}", fill="white")
    destination.parent.mkdir(parents=True, exist_ok=True)
    atlas.save(destination)


def run_measurement_correctness(
    scene_path: Path,
    output_root: Path,
    manual_scene_path: Path | None = None,
    build: bool = False,
    capture: bool = False,
    adb: Path | None = None,
    serial: str | None = None,
) -> dict[str, Any]:
    source_scene = json.loads(scene_path.read_text(encoding="utf-8"))
    manual_scene = json.loads(manual_scene_path.read_text(encoding="utf-8")) if manual_scene_path else None
    output_root.mkdir(parents=True, exist_ok=True)
    geometry_source, fiducial_metadata = _geometry_source(output_root)
    geometry_element = {
        "id": "geometry_fiducials",
        "type": "STATIC_IMAGE",
        "dynamic": False,
        "bbox": {"x": 0, "y": 0, "width": CANVAS_SIZE, "height": CANVAS_SIZE},
        "asset": "assets/geometry_fiducials.png",
        "confidence": 1.0,
    }
    projects: dict[str, dict[str, Any]] = {}
    projects["geometry"] = _compile_diagnostic("geometry", _diagnostic_scene(source_scene, [geometry_element]), geometry_source, output_root)

    static_elements, static_hand_metadata = _static_hand_elements(source_scene, scene_path.parent)
    projects["hand_static"] = _compile_diagnostic("hand-static", _diagnostic_scene(source_scene, static_elements), scene_path.parent, output_root)
    for hand in [element for element in source_scene.get("elements", []) if element.get("type") == "ANALOG_HAND"]:
        role = str(hand["role"]).lower()
        projects[f"hand_dynamic_{role}"] = _compile_diagnostic(
            f"hand-dynamic-{role}",
            _diagnostic_scene(source_scene, [copy.deepcopy(hand)]),
            scene_path.parent,
            output_root,
        )

    text_element = next(element for element in source_scene.get("elements", []) if element.get("type") == "DYNAMIC_SLOT")
    projects["text_off"] = _compile_diagnostic("text-off", _diagnostic_scene(source_scene, [copy.deepcopy(text_element)]), scene_path.parent, output_root)
    projects["production_off"] = _compile_diagnostic("production-off", copy.deepcopy(source_scene), scene_path.parent, output_root)
    if manual_scene is not None and manual_scene_path is not None:
        manual_text = next(element for element in manual_scene.get("elements", []) if element.get("type") == "DYNAMIC_SLOT")
        projects["text_manual"] = _compile_diagnostic("text-manual", _diagnostic_scene(manual_scene, [copy.deepcopy(manual_text)]), manual_scene_path.parent, output_root)
        projects["production_manual"] = _compile_diagnostic("production-manual", copy.deepcopy(manual_scene), manual_scene_path.parent, output_root)

    build_results = {}
    if build or capture:
        for name, project in projects.items():
            build_results[name] = _build_project(Path(project["project"]))

    runtime = detect_runtime(adb)
    failed_builds = [name for name, result in build_results.items() if not result.get("success")]
    captures: dict[str, Any] = {}
    geometry_measurement: dict[str, Any] = {"status": "not_captured", "fiducials": fiducial_metadata}
    hand_calibration: dict[str, Any] = {"assetGeometry": {}, "dynamicAngles": {}, "pivot": "unmeasured"}
    text_calibration: list[dict[str, Any]] = []
    if capture and runtime["status"] == "runtime_available" and not failed_builds:
        selected_serial = serial or runtime["selectedDevice"]
        adb_executable = runtime["adb"]
        geometry_capture = capture_runtime_image(adb_executable, selected_serial, Path(build_results["geometry"]["apk"]), output_root / "runtime" / "geometry.png")
        captures["geometry"] = geometry_capture
        if geometry_capture["captureOk"]:
            geometry_measurement = _fit_geometry(Path(geometry_capture["path"]))
            geometry_measurement["fiducials"] = fiducial_metadata

        static_capture = capture_runtime_image(adb_executable, selected_serial, Path(build_results["hand_static"]["apk"]), output_root / "runtime" / "hand-static.png")
        captures["hand_static"] = static_capture
        if static_capture["captureOk"] and geometry_measurement.get("status") == "measured":
            hand_calibration["assetGeometry"] = _measure_static_hands(Path(static_capture["path"]), geometry_measurement, static_hand_metadata)

        for role in ("hour", "minute", "second"):
            project_key = f"hand_dynamic_{role}"
            role_records = []
            for time_value in HAND_TIMES:
                destination = output_root / "runtime" / f"hand-{role}-{time_value.replace(':', '-')}.png"
                capture_record = capture_runtime_image(adb_executable, selected_serial, Path(build_results[project_key]["apk"]), destination, time_value=time_value)
                observed = _hand_angle_from_black_scene(destination, geometry_measurement) if capture_record["captureOk"] and geometry_measurement.get("status") == "measured" else None
                interval = _angle_interval(role.upper(), time_value, capture_record.get("captureTimestampDeltaRangeMs"))
                angle_error = _angle_error_to_interval(observed, interval)
                detector_tolerance = 2.0
                role_records.append({
                    "time": time_value,
                    "capture": capture_record,
                    "observedAngleDeg": observed,
                    "expectedAngleInterval": interval,
                    "angleErrorToCaptureIntervalDeg": round(angle_error, 4) if angle_error is not None else None,
                    "withinCaptureInterval": angle_error == 0 if angle_error is not None else None,
                    "detectorToleranceDeg": detector_tolerance,
                    "withinCaptureIntervalWithDetectorTolerance": angle_error <= detector_tolerance if angle_error is not None else None,
                })
            hand_calibration["dynamicAngles"][role.upper()] = role_records

        text_modes = [("off", "text_off", source_scene)]
        if manual_scene is not None and manual_scene_path is not None:
            text_modes.append(("manual", "text_manual", manual_scene))
        for mode, project_key, text_scene in text_modes:
            bbox = next(element for element in text_scene["elements"] if element.get("type") == "DYNAMIC_SLOT")["bbox"]
            for day in TEXT_DAYS:
                destination = output_root / "runtime" / f"text-{mode}-day-{day:02d}.png"
                capture_record = capture_runtime_image(adb_executable, selected_serial, Path(build_results[project_key]["apk"]), destination, day=day)
                deterministic = output_root / "deterministic" / f"text-{mode}-day-{day:02d}.png"
                render_wff_xml(Path(projects[project_key]["xml"]), deterministic, fixed_time=DIAGNOSTIC_TIME, fixed_date=f"2024-08-{day:02d}")
                measurement = _measure_text(destination, deterministic, geometry_measurement, bbox) if capture_record["captureOk"] and geometry_measurement.get("status") == "measured" else {"visible": False}
                text_calibration.append({"mode": mode, "day": day, "capture": capture_record, "measurement": measurement, "deterministic": str(deterministic)})
        _write_text_atlas(text_calibration, output_root / "text-runtime-atlas.png")

    official_validation = {
        name: {"passed": "PASSED" in project["officialValidation"], "output": project["officialValidation"]}
        for name, project in projects.items()
    }
    manual_days = []
    if "text_manual" in projects:
        root = ET.parse(projects["text_manual"]["xml"]).getroot()
        expression = root.find(".//Condition/Expressions/Expression")
        if expression is not None:
            manual_days = [int(value) for value in re.findall(r"==\s*(\d+)", str(expression.text or ""))]
    visible = [item for item in text_calibration if item["measurement"].get("visible")]
    angle_records = [record for records in hand_calibration["dynamicAngles"].values() for record in records]
    if failed_builds:
        status = "blocked_by_build"
    elif capture and runtime["status"] != "runtime_available":
        status = "blocked_by_runtime_environment"
    elif capture:
        status = "runtime_measured"
    else:
        status = "implemented_not_captured"
    report = {
        "milestone": "A2.5c.1 Measurement Correctness",
        "baseline": "d5d6c98",
        "baselineStatus": "partial_with_invalid_metrics",
        "experimentalBaselineTag": "experimental/a25c-invalid-metrics",
        "status": status,
        "officialValidation": official_validation,
        "manualGlyphBehavior": {
            "strategy": "whole-date BitmapFont only when every digit is available; otherwise whole-date Pretendard",
            "manualEligibleDays": manual_days,
            "characterLevelFallback": False,
            "fallbackFamilyAttributePresent": False,
        },
        "geometryCalibration": geometry_measurement,
        "handCalibration": hand_calibration,
        "textCalibration": text_calibration,
        "validMetrics": {
            "officialValidatorPass": all(item["passed"] for item in official_validation.values()),
            "geometryFitResidualMeasured": geometry_measurement.get("status") == "measured",
            "handAssetGeometryMeasured": bool(hand_calibration["assetGeometry"]),
            "captureTimeUncertaintyMeasured": bool(angle_records) and all(record["capture"].get("captureTimeUncertaintyMs") is not None for record in angle_records),
            "dynamicHandAnglesMeasured": bool(angle_records) and all(record.get("observedAngleDeg") is not None for record in angle_records),
            "dynamicHandAnglesWithinCaptureBracket": bool(angle_records) and all(record.get("withinCaptureInterval") is True for record in angle_records),
            "dynamicHandAnglesWithinCaptureBracketWithDetectorTolerance": bool(angle_records) and all(record.get("withinCaptureIntervalWithDetectorTolerance") is True for record in angle_records),
            "dateRuntimeVisible": bool(text_calibration) and len(visible) == len(text_calibration),
        },
        "invalidatedOldMetrics": [
            "A2.5c global/static/hand/date MAE computed from production screenshots",
            "expected-angle-constrained production hand detector",
            "hardcoded 0px pivot error",
            "framebuffer-derived viewport and transform",
            "inverse-resampling winner treated as the platform filter",
        ],
        "remainingIssues": ["pivot remains unmeasured unless independently detectable from a dedicated target"] + ([f"diagnostic builds failed: {', '.join(failed_builds)}"] if failed_builds else []),
        "regressions": [] if all(result.get("success") for name, result in build_results.items() if name.startswith("production_")) else ["production diagnostic build failed"],
        "runtimeEnvironment": runtime,
        "projects": projects,
        "builds": build_results,
        "captures": captures,
        "acceptanceUsesGlobalMae": False,
    }
    report_path = output_root / "measurement-correctness-report.json"
    _write_json(report_path, report)
    report["report"] = str(report_path)
    return report
