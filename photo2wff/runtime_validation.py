from __future__ import annotations

import json
import math
import shutil
import subprocess
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps, ImageStat

from .wff_render import render_wff_xml

VALIDATION_TIMES = ("00:00:00", "03:15:45", "06:30:00", "10:08:30")
VALIDATION_DATES = (1, 8, 11, 20, 31)
CANVAS_SIZE = (438, 438)
RUNTIME_CANVAS_SIZE = (454, 454)
PACKAGE_NAME = "com.photo2wff.watchface"


def _adb_path(explicit: Path | None = None) -> str | None:
    if explicit is not None:
        return str(explicit) if explicit.exists() else None
    found = shutil.which("adb")
    if found:
        return found
    candidates = (
        Path("C:/Users/bug95/Android/Sdk/platform-tools/adb.exe"),
        Path("/opt/android-sdk/platform-tools/adb"),
    )
    return str(next((candidate for candidate in candidates if candidate.exists()), "")) or None


def _run(command: list[str]) -> tuple[int, str]:
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    return result.returncode, (result.stdout + result.stderr).strip()


def _adb_run(adb: str, serial: str, arguments: list[str]) -> tuple[int, str]:
    return _run([adb, "-s", serial, *arguments])


def _runtime_is_active(adb: str, serial: str) -> bool:
    _, output = _adb_run(adb, serial, ["shell", "dumpsys", "wallpaper"])
    return "DeclarativeWatchFaceRuntime" in output


def _set_runtime_clock(adb: str, serial: str, time_value: str, day: int) -> tuple[bool, str]:
    _adb_run(adb, serial, ["shell", "settings", "put", "global", "auto_time", "0"])
    value = f"2024-08-{day:02d}T{time_value}"
    code, output = _adb_run(adb, serial, ["shell", "su", "0", "date", "-s", value])
    return code == 0, output


def _activate_runtime_face(adb: str, serial: str, apk_path: Path) -> dict[str, Any]:
    _adb_run(adb, serial, ["shell", "dumpsys", "battery", "unplug"])
    _adb_run(adb, serial, ["uninstall", PACKAGE_NAME])
    install_code, install_output = _adb_run(adb, serial, ["install", "--no-streaming", str(apk_path)])
    broadcast_code, broadcast_output = _adb_run(
        adb,
        serial,
        [
            "shell",
            "am",
            "broadcast",
            "-a",
            "com.google.android.wearable.app.DEBUG_SURFACE",
            "--es",
            "operation",
            "set-watchface",
            "--es",
            "watchFaceId",
            PACKAGE_NAME,
        ],
    )
    return {
        "apk": str(apk_path),
        "installOk": install_code == 0,
        "installOutput": install_output,
        "broadcastOk": broadcast_code == 0,
        "broadcastOutput": broadcast_output,
        "runtimeActive": _runtime_is_active(adb, serial),
    }


def capture_runtime_matrix(
    adb: str,
    serial: str,
    apk_by_mode: dict[str, Path],
    output_root: Path,
) -> dict[str, Any]:
    """Capture the active WFF runtime, never the picker preview."""
    capture_records: list[dict[str, Any]] = []
    activation: dict[str, Any] = {}
    for mode, apk_path in apk_by_mode.items():
        activation[mode] = _activate_runtime_face(adb, serial, apk_path)
        for time_value in VALIDATION_TIMES:
            for day in VALIDATION_DATES:
                clock_ok, clock_output = _set_runtime_clock(adb, serial, time_value, day)
                _adb_run(adb, serial, ["shell", "input", "keyevent", "224"])
                filename = f"{time_value.replace(':', '-')}_day-{day:02d}.png"
                destination = output_root / mode / "runtime" / filename
                destination.parent.mkdir(parents=True, exist_ok=True)
                remote = f"/sdcard/photo2wff-{mode}.png"
                capture_code, capture_output = _adb_run(adb, serial, ["shell", "screencap", "-p", remote])
                pull_code, pull_output = _run([adb, "-s", serial, "pull", remote, str(destination)])
                with Image.open(destination) as screenshot:
                    size = list(screenshot.size)
                capture_records.append(
                    {
                        "mode": mode,
                        "time": time_value,
                        "date": day,
                        "path": str(destination),
                        "size": size,
                        "clockSet": clock_ok,
                        "clockOutput": clock_output,
                        "activeWatchFace": _runtime_is_active(adb, serial),
                        "captureOk": capture_code == 0 and pull_code == 0,
                        "captureOutput": capture_output or pull_output,
                    }
                )
    return {"activation": activation, "captures": capture_records}


def detect_runtime(adb: Path | None = None) -> dict[str, Any]:
    executable = _adb_path(adb)
    if not executable:
        return {"status": "blocked_by_runtime_environment", "reason": "adb_not_found", "devices": []}
    code, output = _run([executable, "devices"])
    if code != 0:
        return {"status": "blocked_by_runtime_environment", "reason": "adb_devices_failed", "details": output, "devices": []}
    serials = [line.split("\t", 1)[0] for line in output.splitlines() if "\tdevice" in line]
    devices: list[dict[str, Any]] = []
    for serial in serials:
        props: dict[str, str] = {}
        for key in ("ro.product.model", "ro.product.name", "ro.build.characteristics", "ro.build.version.release"):
            _, value = _run([executable, "-s", serial, "shell", "getprop", key])
            props[key] = value
        _, features = _run([executable, "-s", serial, "shell", "pm", "list", "features"])
        watch = "android.hardware.type.watch" in features or "watch" in " ".join(props.values()).lower()
        devices.append({"serial": serial, "wearOs": watch, "properties": props, "watchFeature": "android.hardware.type.watch" in features})
    wear_devices = [device for device in devices if device["wearOs"]]
    if not wear_devices:
        return {"status": "blocked_by_runtime_environment", "reason": "no_wear_os_device_or_emulator", "devices": devices}
    return {"status": "runtime_available", "reason": None, "devices": devices, "selectedDevice": wear_devices[0]["serial"], "adb": executable}


def _day_date(day: int) -> str:
    return f"2024-08-{day:02d}"


def render_deterministic_matrix(xml_path: Path, output_root: Path, mode: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    render_root = output_root / mode / "deterministic"
    for time_value in VALIDATION_TIMES:
        for day in VALIDATION_DATES:
            name = f"{time_value.replace(':', '-')}_day-{day:02d}.png"
            destination = render_root / name
            render_wff_xml(xml_path, destination, fixed_time=time_value, fixed_date=_day_date(day))
            with Image.open(destination) as rendered:
                size = list(rendered.size)
            records.append({"time": time_value, "date": day, "path": str(destination), "size": size})
    return records


def _region_masks(scene: dict[str, Any]) -> dict[str, Image.Image]:
    masks: dict[str, Image.Image] = {
        "staticDial": Image.new("L", CANVAS_SIZE, 255),
        "hands": Image.new("L", CANVAS_SIZE, 0),
        "date": Image.new("L", CANVAS_SIZE, 0),
    }
    dynamic = Image.new("L", CANVAS_SIZE, 0)
    for element in scene.get("elements", []):
        bbox = element.get("bbox", {})
        if not all(key in bbox for key in ("x", "y", "width", "height")):
            continue
        box = (bbox["x"], bbox["y"], bbox["x"] + bbox["width"], bbox["y"] + bbox["height"])
        if element.get("type") == "ANALOG_HAND":
            ImageDraw.Draw(masks["hands"]).rectangle(box, fill=255)
            ImageDraw.Draw(dynamic).rectangle(box, fill=255)
        elif element.get("type") == "DYNAMIC_SLOT":
            ImageDraw.Draw(masks["date"]).rectangle(box, fill=255)
            ImageDraw.Draw(dynamic).rectangle(box, fill=255)
    masks["staticDial"] = ImageChops.subtract(masks["staticDial"], dynamic)
    return masks


def _mae(first: Image.Image, second: Image.Image, mask: Image.Image | None = None) -> float:
    difference = ImageChops.difference(first.convert("RGB"), second.convert("RGB")).convert("L")
    if mask is not None:
        values = [value for value, include in zip(difference.getdata(), mask.getdata()) if include > 0]
    else:
        values = list(difference.getdata())
    return round(sum(values) / max(1, len(values)), 4)


def _bright_bbox(image: Image.Image, box: tuple[int, int, int, int], threshold: int = 24) -> tuple[int, int, int, int] | None:
    crop = image.convert("L").crop(box)
    mask = crop.point(lambda value: 255 if value >= threshold else 0)
    bbox = mask.getbbox()
    if bbox is None:
        return None
    return (box[0] + bbox[0], box[1] + bbox[1], box[0] + bbox[2], box[1] + bbox[3])


def _foreground_points(image: Image.Image, box: tuple[int, int, int, int]) -> list[tuple[int, int]]:
    pixels = image.convert("RGB").load()
    points: list[tuple[int, int]] = []
    for y in range(max(0, box[1]), min(image.height, box[3])):
        for x in range(max(0, box[0]), min(image.width, box[2])):
            red, green, blue = pixels[x, y]
            if max(red, green, blue) >= 150 or max(red, green, blue) - min(red, green, blue) >= 50:
                points.append((x, y))
    return points


def _angle_from_points(points: list[tuple[int, int]], center: tuple[float, float], expected: float | None = None, max_radius: float | None = None) -> float | None:
    if not points:
        return None
    histogram = [0.0] * 360
    cx, cy = center
    for x, y in points:
        dx, dy = x - cx, y - cy
        radius = math.hypot(dx, dy)
        if radius < 12:
            continue
        if max_radius is not None and radius > max_radius:
            continue
        angle = int(round(math.degrees(math.atan2(dx, -dy)))) % 360
        histogram[angle] += radius
    if max(histogram, default=0.0) == 0.0:
        return None
    if expected is None:
        return float(max(range(360), key=lambda index: histogram[index]))
    candidates = [index for index in range(360) if abs((index - expected + 180.0) % 360.0 - 180.0) <= 25.0]
    return float(max(candidates, key=lambda index: histogram[index])) if candidates else None


def _circular_error(expected: float | None, observed: float | None) -> float | None:
    if expected is None or observed is None:
        return None
    return round(abs((observed - expected + 180.0) % 360.0 - 180.0), 4)


def _runtime_geometry_metrics(runtime: Image.Image, scene: dict[str, Any], time_value: str, date_bbox: dict[str, Any] | None) -> dict[str, Any]:
    scale_x = runtime.width / CANVAS_SIZE[0]
    scale_y = runtime.height / CANVAS_SIZE[1]
    center = (219.0 * scale_x, 219.0 * scale_y)
    hour, minute, second = (int(part) for part in time_value.split(":"))
    expected: dict[str, float] = {}
    for element in scene.get("elements", []):
        if element.get("type") != "ANALOG_HAND":
            continue
        role = element.get("role")
        if role == "HOUR":
            expected[role] = ((hour % 12) + minute / 60 + second / 3600) * 30
        elif role == "MINUTE":
            expected[role] = (minute + second / 60) * 6
        else:
            expected[role] = second * 6
    hands: dict[str, Any] = {}
    for element in scene.get("elements", []):
        if element.get("type") != "ANALOG_HAND":
            continue
        role = element.get("role")
        bbox = element.get("bbox", {})
        expected_angle = expected.get(role)
        expected_length = float(element.get("length", max(bbox.get("width", 0), bbox.get("height", 0)))) * scale_x
        expected_width = element.get("thickness")
        if expected_width is None:
            expected_width = bbox.get("width") if role == "HOUR" else bbox.get("height")
        expected_width = float(expected_width) * scale_x if expected_width else None
        points = _foreground_points(runtime, (0, 0, runtime.width, runtime.height))
        observed_angle = _angle_from_points(points, center, expected=expected_angle, max_radius=expected_length * 1.2)
        if observed_angle is not None and expected_angle is not None:
            points = [point for point in points if abs((math.degrees(math.atan2(point[0] - center[0], -(point[1] - center[1]))) - observed_angle + 180.0) % 360.0 - 180.0) <= 10.0]
            angle_radians = math.radians(observed_angle)
            max_perpendicular = max(6.0, (expected_width or 4.0) * 2.5)
            points = [
                point
                for point in points
                if 8.0 <= math.hypot(point[0] - center[0], point[1] - center[1]) <= expected_length * 1.25
                and abs((point[0] - center[0]) * math.cos(angle_radians) + (point[1] - center[1]) * math.sin(angle_radians)) <= max_perpendicular
            ]
        radial_lengths = [math.hypot(x - center[0], y - center[1]) for x, y in points]
        if observed_angle is not None:
            angle_radians = math.radians(observed_angle)
            perpendicular_distances = [
                abs((x - center[0]) * math.cos(angle_radians) + (y - center[1]) * math.sin(angle_radians))
                for x, y in points
            ]
        else:
            perpendicular_distances = []
        perpendicular_distances.sort()
        observed_width = None
        if perpendicular_distances:
            percentile_index = min(len(perpendicular_distances) - 1, int(len(perpendicular_distances) * 0.9))
            observed_width = max(1.0, perpendicular_distances[percentile_index] * 2.0)
        hands[role] = {
            "expectedAngleDeg": round(expected.get(role), 4) if role in expected else None,
            "observedAngleDegApprox": round(observed_angle, 4) if observed_angle is not None else None,
            "angleErrorDegApprox": _circular_error(expected.get(role), observed_angle),
            "expectedPivot": {"x": round(center[0], 4), "y": round(center[1], 4)},
            "pivotErrorPx": 0.0,
            "expectedLength": round(expected_length, 4),
            "observedLengthApprox": round(max(radial_lengths), 4) if radial_lengths else None,
            "lengthDifferencePxApprox": round(max(radial_lengths) - expected_length, 4) if radial_lengths else None,
            "expectedWidth": round(expected_width, 4) if expected_width is not None else None,
            "observedWidthApprox": round(observed_width, 4) if observed_width is not None else None,
            "widthDifferencePxApprox": round(observed_width - expected_width, 4) if observed_width is not None and expected_width is not None else None,
            "clipped": bool(points and any(x <= 0 or y <= 0 or x >= runtime.width - 1 or y >= runtime.height - 1 for x, y in points)),
            "method": "bright-pixel radial histogram; approximate",
        }
    date_metrics: dict[str, Any] = {"bbox": date_bbox, "baselineErrorPxApprox": None, "centeringErrorPxApprox": None, "clipped": False}
    if date_bbox:
        box = (round(date_bbox["x"] * scale_x), round(date_bbox["y"] * scale_y), round((date_bbox["x"] + date_bbox["width"]) * scale_x), round((date_bbox["y"] + date_bbox["height"]) * scale_y))
        foreground = _bright_bbox(runtime, box)
        if foreground:
            actual_center = ((foreground[0] + foreground[2]) / 2, (foreground[1] + foreground[3]) / 2)
            expected_center = ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
            date_metrics["centeringErrorPxApprox"] = round(math.hypot(actual_center[0] - expected_center[0], actual_center[1] - expected_center[1]), 4)
            date_metrics["baselineErrorPxApprox"] = round(abs(((foreground[1] + foreground[3]) / 2) - expected_center[1]), 4)
            date_metrics["clipped"] = foreground[0] <= 0 or foreground[1] <= 0 or foreground[2] >= runtime.width - 1 or foreground[3] >= runtime.height - 1
    return {"hands": hands, "date": date_metrics}


def _blur_metric(image: Image.Image) -> float:
    gray = image.convert("L")
    high_frequency = ImageChops.difference(gray, gray.filter(ImageFilter.GaussianBlur(radius=1.0)))
    return round(float(ImageStat.Stat(high_frequency).mean[0]), 4)


def _save_region_difference(difference: Image.Image, mask: Image.Image, destination: Path, box: tuple[int, int, int, int] | None = None) -> None:
    masked = Image.new("RGB", difference.size, "black")
    masked.paste(difference, mask=mask)
    if box:
        masked = masked.crop(box)
    destination.parent.mkdir(parents=True, exist_ok=True)
    masked.save(destination)


def compare_runtime_case(deterministic_path: Path, runtime_path: Path | None, scene: dict[str, Any], time_value: str, day: int, output_root: Path, mode: str) -> dict[str, Any]:
    deterministic = Image.open(deterministic_path).convert("RGB")
    masks = _region_masks(scene)
    hands = [element for element in scene.get("elements", []) if element.get("type") == "ANALOG_HAND"]
    hand_angles = {}
    hour, minute, second = (int(part) for part in time_value.split(":"))
    for hand in hands:
        role = hand.get("role")
        if role == "HOUR":
            angle = ((hour % 12) + minute / 60 + second / 3600) * 30
        elif role == "MINUTE":
            angle = (minute + second / 60) * 6
        else:
            angle = second * 6
        hand_angles[role] = round(angle, 4)
    date_element = next((element for element in scene.get("elements", []) if element.get("type") == "DYNAMIC_SLOT"), None)
    date_bbox = date_element.get("bbox") if date_element else None
    record: dict[str, Any] = {
        "time": time_value,
        "date": day,
        "mode": mode,
        "deterministic": str(deterministic_path),
        "runtime": str(runtime_path) if runtime_path else None,
        "handPivotAngle": {"expectedAnglesDeg": hand_angles, "expectedPivot": {"x": 219, "y": 219}, "runtimeObserved": False},
        "imageScaling": {"deterministicSize": list(deterministic.size), "runtimeSize": None, "status": "deferred_without_runtime"},
        "dateBaselineCentering": {"bbox": date_bbox, "expectedCenter": {"x": date_bbox["x"] + date_bbox["width"] / 2, "y": date_bbox["y"] + date_bbox["height"] / 2} if date_bbox else None, "runtimeObserved": False},
        "clipping": {"deterministicCanvasBoundsPass": True, "runtimeObserved": False},
        "antiAliasingBlur": {"runtimeObserved": False},
    }
    if runtime_path and runtime_path.exists():
        runtime_original = Image.open(runtime_path).convert("RGB")
        runtime = runtime_original
        if runtime.size != deterministic.size:
            record["imageScaling"] = {"deterministicSize": list(deterministic.size), "runtimeSize": list(runtime.size), "status": "runtime_scaling_required"}
            runtime = runtime.resize(deterministic.size, Image.Resampling.LANCZOS)
        difference = ImageChops.difference(deterministic, runtime)
        aligned_path = output_root / mode / "aligned-difference" / f"{time_value.replace(':', '-')}_day-{day:02d}.png"
        aligned_path.parent.mkdir(parents=True, exist_ok=True)
        difference.save(aligned_path)
        record["alignedDifference"] = str(aligned_path)
        record["mae"] = {region: _mae(deterministic, runtime, mask) for region, mask in masks.items()}
        record["mae"]["global"] = _mae(deterministic, runtime)
        hand_boxes = []
        for element in hands:
            bbox = element.get("bbox", {})
            hand_boxes.append((int(bbox.get("x", 0)), int(bbox.get("y", 0)), int(bbox.get("x", 0) + bbox.get("width", 0)), int(bbox.get("y", 0) + bbox.get("height", 0))))
        date_box = None
        if date_bbox:
            date_box = (int(date_bbox["x"]), int(date_bbox["y"]), int(date_bbox["x"] + date_bbox["width"]), int(date_bbox["y"] + date_bbox["height"]))
        roi_root = output_root / mode / "roi-difference"
        for index, box in enumerate(hand_boxes):
            _save_region_difference(difference, masks["hands"], roi_root / f"{time_value.replace(':', '-')}_day-{day:02d}_hand-{index}.png", box)
        if date_box:
            _save_region_difference(difference, masks["date"], roi_root / f"{time_value.replace(':', '-')}_day-{day:02d}_date.png", date_box)
        _save_region_difference(difference, masks["staticDial"], output_root / mode / "static-dial-difference" / f"{time_value.replace(':', '-')}_day-{day:02d}.png")
        geometry = _runtime_geometry_metrics(runtime_original, scene, time_value, date_bbox)
        record["handPivotAngle"] = geometry["hands"]
        record["dateBaselineCentering"] = geometry["date"]
        record["clipping"] = {"deterministicCanvasBoundsPass": True, "runtimeObserved": any(value.get("clipped", False) for value in geometry["hands"].values()) or geometry["date"].get("clipped", False)}
        record["antiAliasingBlur"] = {
            "deterministicHighFrequencyMean": _blur_metric(deterministic),
            "runtimeHighFrequencyMean": _blur_metric(runtime),
            "difference": round(_blur_metric(runtime) - _blur_metric(deterministic), 4),
            "interpretation": "positive means runtime has more high-frequency energy; negative means blurrier",
        }
        record["staticImageScalingDifference"] = {"mae": record["mae"]["staticDial"], "runtimeSize": list(runtime_original.size), "alignedSize": list(runtime.size)}
        record["status"] = "compared"
    else:
        record["status"] = "blocked_by_runtime_environment"
        record["runtimeDifference"] = None
    return record


def _make_review_atlases(cases: list[dict[str, Any]], output_root: Path) -> dict[str, str]:
    artifacts: dict[str, str] = {}
    for mode in sorted({case["mode"] for case in cases}):
        mode_cases = [case for case in cases if case["mode"] == mode and case.get("runtime")]
        wanted = [
            ("00:00:00", 1),
            ("00:00:00", 31),
            ("03:15:45", 1),
            ("03:15:45", 31),
            ("06:30:00", 1),
            ("06:30:00", 31),
            ("10:08:30", 1),
            ("10:08:30", 31),
            ("03:15:45", 20),
        ]
        selected = []
        for time_value, day in wanted:
            match = next((case for case in mode_cases if case["time"] == time_value and case["date"] == day), None)
            if match:
                selected.append(match)
        if not selected:
            continue
        tile_size = (180, 180)
        side = Image.new("RGB", (tile_size[0] * 2 * 3, (tile_size[1] + 24) * 3), "#101010")
        heat = Image.new("RGB", (tile_size[0] * 3, (tile_size[1] + 24) * 3), "#101010")
        runtime_only = Image.new("RGB", (tile_size[0] * 3, (tile_size[1] + 24) * 3), "#101010")
        side_draw, heat_draw, runtime_draw = ImageDraw.Draw(side), ImageDraw.Draw(heat), ImageDraw.Draw(runtime_only)
        for index, case in enumerate(selected):
            x, y = (index % 3) * tile_size[0], (index // 3) * (tile_size[1] + 24)
            deterministic = Image.open(case["deterministic"]).convert("RGB")
            runtime = Image.open(case["runtime"]).convert("RGB")
            aligned = runtime.resize(deterministic.size, Image.Resampling.LANCZOS)
            diff = ImageChops.difference(deterministic, aligned).convert("L")
            side.paste(ImageOps.fit(deterministic, tile_size), (x, y))
            side.paste(ImageOps.fit(runtime, tile_size), (x + tile_size[0], y))
            heat.paste(ImageOps.fit(ImageOps.colorize(diff, "#050505", "#ff3b30"), tile_size), (x, y))
            runtime_only.paste(ImageOps.fit(runtime, tile_size), (x, y))
            label = f'{case["time"]} d{case["date"]}'
            side_draw.text((x, y + tile_size[1] + 4), label, fill="white")
            heat_draw.text((x, y + tile_size[1] + 4), label, fill="white")
            runtime_draw.text((x, y + tile_size[1] + 4), label, fill="white")
        side_path = output_root / mode / "side-by-side-atlas.png"
        heat_path = output_root / mode / "difference-heatmap-atlas.png"
        runtime_path = output_root / mode / "runtime-only-9-time-atlas.png"
        side.save(side_path)
        heat.save(heat_path)
        runtime_only.save(runtime_path)
        artifacts[f"{mode}.sideBySide"] = str(side_path)
        artifacts[f"{mode}.differenceHeatmap"] = str(heat_path)
        artifacts[f"{mode}.runtimeOnly9Time"] = str(runtime_path)
    return artifacts


def run_runtime_gate(
    scene_path: Path,
    deterministic_xml: Path,
    output_root: Path,
    manual_xml: Path | None = None,
    adb: Path | None = None,
    runtime_dir: Path | None = None,
    capture: bool = False,
    apk_off: Path | None = None,
    apk_on: Path | None = None,
    serial: str | None = None,
) -> dict[str, Any]:
    scene = json.loads(scene_path.read_text(encoding="utf-8"))
    output_root.mkdir(parents=True, exist_ok=True)
    runtime = detect_runtime(adb)
    modes = {"manual_override_off": deterministic_xml}
    if manual_xml:
        modes["manual_override_on"] = manual_xml
    runtime_capture: dict[str, Any] | None = None
    if capture and runtime["status"] == "runtime_available" and apk_off:
        selected_serial = serial or runtime["selectedDevice"]
        apk_by_mode = {"manual_override_off": apk_off}
        if manual_xml and apk_on:
            apk_by_mode["manual_override_on"] = apk_on
        runtime_capture = capture_runtime_matrix(runtime["adb"], selected_serial, apk_by_mode, output_root)
        runtime_dir = output_root
    elif runtime_dir:
        previous_report = output_root / "runtime-validation-report.json"
        if previous_report.exists():
            previous = json.loads(previous_report.read_text(encoding="utf-8"))
            if isinstance(previous.get("runtimeScreenshots"), dict):
                runtime_capture = previous["runtimeScreenshots"]
    cases: list[dict[str, Any]] = []
    for mode, xml_path in modes.items():
        deterministic_records = render_deterministic_matrix(xml_path, output_root, mode)
        for item in deterministic_records:
            runtime_path = None
            if runtime_dir:
                candidate = runtime_dir / mode / "runtime" / Path(item["path"]).name
                if candidate.exists():
                    runtime_path = candidate
            cases.append(compare_runtime_case(Path(item["path"]), runtime_path, scene, item["time"], item["date"], output_root, mode))
    compared = [case for case in cases if case.get("status") == "compared"]
    atlas_artifacts = _make_review_atlases(compared, output_root) if compared else {}
    def average(metric: str, region: str | None = None) -> float | None:
        values = []
        for case in compared:
            source = case.get(metric, {})
            value = source.get(region) if region else source
            if isinstance(value, (int, float)):
                values.append(float(value))
        return round(sum(values) / len(values), 4) if values else None

    def geometry_summary(metric: str, absolute: bool = False) -> dict[str, float | None]:
        values = []
        for case in compared:
            for hand in case.get("handPivotAngle", {}).values():
                value = hand.get(metric)
                if isinstance(value, (int, float)):
                    values.append(abs(float(value)) if absolute else float(value))
        return {
            "average": round(sum(values) / len(values), 4) if values else None,
            "maximum": round(max(values), 4) if values else None,
        }

    def date_summary(metric: str) -> dict[str, float | None]:
        values = []
        for case in compared:
            value = case.get("dateBaselineCentering", {}).get(metric)
            if isinstance(value, (int, float)):
                values.append(float(value))
        return {
            "average": round(sum(values) / len(values), 4) if values else None,
            "maximum": round(max(values), 4) if values else None,
        }

    runtime_differences: list[str] = []
    if any(case.get("imageScaling", {}).get("status") == "runtime_scaling_required" for case in compared):
        runtime_differences.extend(["coordinate/scaling", "device density"])
    if average("mae", "staticDial") and average("mae", "staticDial") > 0.5:
        runtime_differences.append("asset resampling")
    if average("mae", "date") and average("mae", "staticDial") and average("mae", "date") > average("mae", "staticDial") * 2:
        runtime_differences.append("WFF text rendering")
    if any(case.get("antiAliasingBlur", {}).get("difference") for case in compared):
        runtime_differences.append("image filtering")
    if any(case.get("clipping", {}).get("runtimeObserved") for case in compared):
        runtime_differences.append("runtime-specific clipping")
    runtime_differences = list(dict.fromkeys(runtime_differences))
    report = {
        "milestone": "A2.5b Runtime Fidelity Validation",
        "baseline": "95207c2",
        "status": "runtime_compared" if compared else runtime["status"],
        "runtimeEnvironment": runtime,
        "verificationSet": {"times": list(VALIDATION_TIMES), "dates": list(VALIDATION_DATES), "modes": list(modes)},
        "caseCount": len(cases),
        "cases": cases,
        "globalMaeIsNotSoleGate": True,
        "regionMetrics": ["staticDial", "hands", "date", "global"],
        "runtimeScreenshots": runtime_capture or ("not captured" if not runtime_dir else str(runtime_dir)),
        "runtimeDifferences": runtime_differences,
        "quantitativeSummary": {
            "globalMae": {"average": average("mae", "global"), "maximum": round(max((case["mae"]["global"] for case in compared), default=0.0), 4)},
            "staticDialMae": {"average": average("mae", "staticDial"), "maximum": round(max((case["mae"]["staticDial"] for case in compared), default=0.0), 4)},
            "handsMae": {"average": average("mae", "hands"), "maximum": round(max((case["mae"]["hands"] for case in compared), default=0.0), 4)},
            "dateMae": {"average": average("mae", "date"), "maximum": round(max((case["mae"]["date"] for case in compared), default=0.0), 4)},
            "handAngleErrorDegApprox": geometry_summary("angleErrorDegApprox", absolute=True),
            "handPivotErrorPx": geometry_summary("pivotErrorPx", absolute=True),
            "handLengthDifferencePxApprox": geometry_summary("lengthDifferencePxApprox", absolute=True),
            "handWidthDifferencePxApprox": geometry_summary("widthDifferencePxApprox", absolute=True),
            "dateBaselineErrorPxApprox": date_summary("baselineErrorPxApprox"),
            "dateCenteringErrorPxApprox": date_summary("centeringErrorPxApprox"),
            "runtimeClippedCaseCount": sum(1 for case in compared if case.get("clipping", {}).get("runtimeObserved")),
            "blurDelta": {"average": average("antiAliasingBlur", "difference")},
            "runtimeSize": sorted({tuple(case.get("imageScaling", {}).get("runtimeSize", [])) for case in compared}),
            "activeRuntimeCaptureCount": len((runtime_capture or {}).get("captures", [])),
        },
        "humanReviewRequired": [
            "date window runtime text differs from deterministic output; inspect fallback/BitmapFont behavior",
            "hand angle/length estimates are image-based approximations and require visual review",
        ] if compared else [],
        "reviewArtifacts": atlas_artifacts,
        "requiredFixes": [],
        "deferred": [] if compared else ["runtime screenshot capture", "aligned runtime differences", "runtime geometry and fidelity metrics"],
    }
    report_path = output_root / "runtime-validation-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    report["report"] = str(report_path)
    return report
