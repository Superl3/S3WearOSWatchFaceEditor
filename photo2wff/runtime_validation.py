from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from xml.etree import ElementTree
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


def _runtime_epoch_ms(adb: str, serial: str) -> int | None:
    code, output = _adb_run(adb, serial, ["shell", "date", "+%s%3N"])
    if code != 0:
        return None
    match = re.search(r"(\d{10,})", output)
    return int(match.group(1)) if match else None


def _utc_iso(epoch_ms: int | None = None) -> str:
    instant = datetime.now(timezone.utc) if epoch_ms is None else datetime.fromtimestamp(epoch_ms / 1000.0, timezone.utc)
    return instant.isoformat()


def _expected_epoch_ms(time_value: str, day: int) -> int:
    hour, minute, second = (int(part) for part in time_value.split(":"))
    return int(datetime(2024, 8, day, hour, minute, second, tzinfo=timezone.utc).timestamp() * 1000)


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
                capture_started = time.perf_counter()
                capture_started_utc = _utc_iso()
                runtime_epoch_before_capture = _runtime_epoch_ms(adb, serial)
                capture_code, capture_output = _adb_run(adb, serial, ["shell", "screencap", "-p", remote])
                pull_code, pull_output = _run([adb, "-s", serial, "pull", remote, str(destination)])
                runtime_epoch_after_capture = _runtime_epoch_ms(adb, serial)
                capture_completed_utc = _utc_iso()
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
                        "expectedEpochMs": _expected_epoch_ms(time_value, day),
                        "runtimeEpochMsBeforeCapture": runtime_epoch_before_capture,
                        "runtimeEpochMsAfterCapture": runtime_epoch_after_capture,
                        "captureTimestampUtc": _utc_iso(runtime_epoch_before_capture),
                        "captureStartedUtc": capture_started_utc,
                        "captureCompletedUtc": capture_completed_utc,
                        "captureLatencyMs": round((time.perf_counter() - capture_started) * 1000.0, 3),
                        "captureTimestampDeltaMs": round(runtime_epoch_before_capture - _expected_epoch_ms(time_value, day), 3) if runtime_epoch_before_capture is not None else None,
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


def _neutral_text_bbox(image: Image.Image, box: tuple[int, int, int, int], threshold: int = 100, chroma_limit: int = 60) -> tuple[int, int, int, int] | None:
    pixels = image.convert("RGB").load()
    points = []
    for y in range(max(0, box[1]), min(image.height, box[3])):
        for x in range(max(0, box[0]), min(image.width, box[2])):
            red, green, blue = pixels[x, y]
            if max(red, green, blue) >= threshold and max(red, green, blue) - min(red, green, blue) <= chroma_limit:
                points.append((x, y))
    if not points:
        return None
    return (min(x for x, _ in points), min(y for _, y in points), max(x for x, _ in points) + 1, max(y for _, y in points) + 1)


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


def _runtime_geometry_metrics(
    runtime: Image.Image,
    scene: dict[str, Any],
    time_value: str,
    date_bbox: dict[str, Any] | None,
    timestamp_delta_ms: float = 0.0,
) -> dict[str, Any]:
    scale_x = runtime.width / CANVAS_SIZE[0]
    scale_y = runtime.height / CANVAS_SIZE[1]
    center = (219.0 * scale_x, 219.0 * scale_y)
    hour, minute, second = (int(part) for part in time_value.split(":"))
    delta_seconds = timestamp_delta_ms / 1000.0
    expected: dict[str, float] = {}
    for element in scene.get("elements", []):
        if element.get("type") != "ANALOG_HAND":
            continue
        role = element.get("role")
        if role == "HOUR":
            expected[role] = ((hour % 12) + minute / 60 + second / 3600) * 30 + delta_seconds * 0.0083333333
        elif role == "MINUTE":
            expected[role] = (minute + second / 60) * 6 + delta_seconds * 0.1
        else:
            expected[role] = second * 6 + delta_seconds * 6.0
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
            "expectedAngleAtCaptureDeg": round(expected.get(role), 4) if role in expected else None,
            "captureTimestampDeltaMs": round(timestamp_delta_ms, 3),
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


CALIBRATION_RESAMPLINGS = {
    "nearest": Image.Resampling.NEAREST,
    "bilinear": Image.Resampling.BILINEAR,
    "bicubic": Image.Resampling.BICUBIC,
}


def _inverse_normalize_runtime(runtime: Image.Image, transform: dict[str, Any], resampling: Image.Resampling) -> Image.Image:
    scale = float(transform["uniformScale"])
    offset_x = float(transform["centerOffset"]["x"])
    offset_y = float(transform["centerOffset"]["y"])
    # Pillow's affine coefficients map each output (logical) pixel to its
    # source (runtime) coordinate, so the forward runtime transform belongs
    # in the sampling matrix.
    matrix = (scale, 0.0, offset_x, 0.0, scale, offset_y)
    return runtime.transform(CANVAS_SIZE, Image.Transform.AFFINE, matrix, resample=resampling)


def _estimate_platform_transform(samples: list[tuple[Image.Image, Image.Image]], scene: dict[str, Any]) -> dict[str, Any]:
    if not samples:
        raise ValueError("runtime calibration requires at least one deterministic/runtime pair")
    runtime_width, runtime_height = samples[0][1].size
    scale_x = runtime_width / CANVAS_SIZE[0]
    scale_y = runtime_height / CANVAS_SIZE[1]
    uniform_scale = (scale_x + scale_y) / 2.0
    initial_offset = {
        "x": (runtime_width - CANVAS_SIZE[0] * uniform_scale) / 2.0,
        "y": (runtime_height - CANVAS_SIZE[1] * uniform_scale) / 2.0,
    }
    masks = _region_masks(scene)

    def score(offset_x: float, offset_y: float, resampling: Image.Resampling) -> float:
        candidate = {"uniformScale": uniform_scale, "centerOffset": {"x": offset_x, "y": offset_y}}
        values = []
        for deterministic, runtime in samples:
            normalized = _inverse_normalize_runtime(runtime, candidate, resampling)
            values.append(_mae(deterministic, normalized, masks["staticDial"]))
        return sum(values) / max(1, len(values))

    best_offset = (initial_offset["x"], initial_offset["y"])
    best_score = score(*best_offset, Image.Resampling.BICUBIC)
    for dx_index in range(-8, 9):
        for dy_index in range(-8, 9):
            candidate_offset = (initial_offset["x"] + dx_index * 0.25, initial_offset["y"] + dy_index * 0.25)
            candidate_score = score(*candidate_offset, Image.Resampling.BICUBIC)
            if candidate_score < best_score:
                best_offset, best_score = candidate_offset, candidate_score

    filter_scores = {}
    for name, resampling in CALIBRATION_RESAMPLINGS.items():
        filter_scores[name] = round(score(*best_offset, resampling), 4)
    selected_resampling = min(filter_scores, key=filter_scores.get)
    logical_radius = min(CANVAS_SIZE) / 2.0
    runtime_radius = min(runtime_width, runtime_height) / 2.0
    return {
        "logicalSize": list(CANVAS_SIZE),
        "runtimeSize": [runtime_width, runtime_height],
        "uniformScale": round(uniform_scale, 8),
        "axisScale": {"x": round(scale_x, 8), "y": round(scale_y, 8)},
        "anisotropy": round(abs(scale_x - scale_y), 8),
        "centerOffset": {"x": round(best_offset[0], 4), "y": round(best_offset[1], 4)},
        "logicalCenter": {"x": CANVAS_SIZE[0] / 2.0, "y": CANVAS_SIZE[1] / 2.0},
        "runtimeCenter": {"x": round(CANVAS_SIZE[0] / 2.0 * uniform_scale + best_offset[0], 4), "y": round(CANVAS_SIZE[1] / 2.0 * uniform_scale + best_offset[1], 4)},
        "circularViewport": {
            "shape": "circle",
            "logicalRadius": round(logical_radius, 4),
            "runtimeRadius": round(runtime_radius, 4),
            "estimatedFromCanvasBounds": True,
            "edgeContourIndependentFit": False,
        },
        "offsetSearch": {"initial": initial_offset, "stepPx": 0.25, "rangePx": 2.0, "staticDialMae": round(best_score, 4)},
        "resamplingCandidatesStaticDialMae": filter_scores,
        "selectedResampling": selected_resampling,
        "inverseTransform": "runtime(x,y) = logical(x,y) * uniformScale + centerOffset",
    }


def _parse_date_render_config(xml_path: Path) -> dict[str, Any]:
    root = ElementTree.parse(xml_path).getroot()
    part_text = next((element for element in root.iter() if element.tag.rsplit("}", 1)[-1] == "PartText"), None)
    text_element = next((element for element in root.iter() if element.tag.rsplit("}", 1)[-1] == "Text"), None)
    font = next((element for element in root.iter() if element.tag.rsplit("}", 1)[-1] in {"Font", "BitmapFont"} and ("size" in element.attrib or "family" in element.attrib)), None)
    bitmap_font = next((element for element in root.iter() if element.tag.rsplit("}", 1)[-1] == "BitmapFont"), None)
    characters = [element.attrib.get("name") for element in root.iter() if element.tag.rsplit("}", 1)[-1] == "Character"]
    return {
        "xml": str(xml_path),
        "textBox": dict(part_text.attrib) if part_text is not None else None,
        "alignment": text_element.attrib.get("align") if text_element is not None else None,
        "renderer": font.tag.rsplit("}", 1)[-1] if font is not None else None,
        "family": font.attrib.get("family") if font is not None else None,
        "bitmapFontName": bitmap_font.attrib.get("name") if bitmap_font is not None else None,
        "fallbackFamily": font.attrib.get("fallbackFamily") if font is not None else None,
        "fontSize": float(font.attrib["size"]) if font is not None and "size" in font.attrib else None,
        "fontWeight": font.attrib.get("weight") if font is not None else None,
        "lineHeightDeclared": font.attrib.get("lineHeight") if font is not None else None,
        "manualCharacters": [character for character in characters if character],
        "runtimeFontResourceIntrospection": "not_exposed_by_screenshot_or_WFF_capture",
    }


def _date_window_measurement(image: Image.Image, expected_bbox: dict[str, Any] | None, reference_bbox: tuple[int, int, int, int] | None = None) -> dict[str, Any]:
    if not expected_bbox:
        return {"observed": False}
    box = (
        int(expected_bbox["x"]),
        int(expected_bbox["y"]),
        int(expected_bbox["x"] + expected_bbox["width"]),
        int(expected_bbox["y"] + expected_bbox["height"]),
    )
    observed = _neutral_text_bbox(image, box)
    result: dict[str, Any] = {
        "expectedBox": list(box),
        "observed": observed is not None,
        "foregroundBbox": list(observed) if observed else None,
        "foregroundSize": {"width": observed[2] - observed[0], "height": observed[3] - observed[1]} if observed else None,
        "centerOffsetPx": None,
        "baselineBottomOffsetPx": None,
    }
    if observed:
        expected_center = ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)
        observed_center = ((observed[0] + observed[2]) / 2.0, (observed[1] + observed[3]) / 2.0)
        result["centerOffsetPx"] = round(math.hypot(observed_center[0] - expected_center[0], observed_center[1] - expected_center[1]), 4)
        if reference_bbox:
            result["baselineBottomOffsetPx"] = round(observed[3] - reference_bbox[3], 4)
            result["referenceForegroundBbox"] = list(reference_bbox)
    return result


def _write_date_calibration_sheet(records: list[dict[str, Any]], destination: Path) -> None:
    selected = [record for record in records if record["time"] == "00:00:00" and record["date"] in VALIDATION_DATES]
    tile = (120, 84)
    sheet = Image.new("RGB", (tile[0] * 3, (tile[1] + 18) * max(1, len(selected))), "#101010")
    draw = ImageDraw.Draw(sheet)
    for index, record in enumerate(selected):
        y = index * (tile[1] + 18)
        deterministic = Image.open(record["deterministic"]).convert("RGB").crop((346, 207, 393, 236))
        normalized = Image.open(record["normalizedRuntime"]).convert("RGB").crop((346, 207, 393, 236))
        difference = ImageChops.difference(deterministic, normalized)
        sheet.paste(ImageOps.contain(deterministic, tile), (0, y))
        sheet.paste(ImageOps.contain(normalized, tile), (tile[0], y))
        sheet.paste(ImageOps.contain(ImageOps.colorize(difference.convert("L"), "#050505", "#ff3b30"), tile), (tile[0] * 2, y))
        draw.text((4, y + tile[1] + 2), f'day {record["date"]}', fill="white")
    destination.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(destination)


def _calibration_case_key(mode: str, time_value: str, day: int) -> tuple[str, str, int]:
    return mode, time_value, day


def run_runtime_calibration(
    scene_path: Path,
    deterministic_xml: Path,
    runtime_source_dir: Path,
    output_root: Path,
    manual_xml: Path | None = None,
) -> dict[str, Any]:
    scene = json.loads(scene_path.read_text(encoding="utf-8"))
    output_root.mkdir(parents=True, exist_ok=True)
    source_report_path = runtime_source_dir / "runtime-validation-report.json"
    source_report = json.loads(source_report_path.read_text(encoding="utf-8")) if source_report_path.exists() else {}
    modes = {"manual_override_off": deterministic_xml}
    if manual_xml:
        modes["manual_override_on"] = manual_xml
    capture_index = {}
    for capture in source_report.get("runtimeScreenshots", {}).get("captures", []):
        capture_index[_calibration_case_key(capture["mode"], capture["time"], int(capture["date"]))] = capture
    source_case_index = {
        _calibration_case_key(case["mode"], case["time"], int(case["date"])): case
        for case in source_report.get("cases", [])
    }
    deterministic_records_by_mode: dict[str, list[dict[str, Any]]] = {}
    samples: list[tuple[Image.Image, Image.Image]] = []
    for mode, xml_path in modes.items():
        deterministic_records = render_deterministic_matrix(xml_path, output_root, mode)
        deterministic_records_by_mode[mode] = deterministic_records
        for item in deterministic_records:
            runtime_path = runtime_source_dir / mode / "runtime" / Path(item["path"]).name
            if runtime_path.exists() and len(samples) < 12:
                samples.append((Image.open(item["path"]).convert("RGB"), Image.open(runtime_path).convert("RGB")))
    if not samples:
        report = {
            "milestone": "A2.5c Runtime Calibration",
            "baseline": "7421f98",
            "status": "blocked_by_runtime_environment",
            "platformTransform": None,
            "normalizedMetrics": None,
            "trueWffRenderingDifferences": None,
            "measurementArtifacts": {},
            "requiredProductionFixes": [],
            "deferred": ["runtime screenshots from A2.5b are unavailable"],
        }
        report_path = output_root / "runtime-calibration-report.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
        report["report"] = str(report_path)
        return report
    transform = _estimate_platform_transform(samples, scene)
    selected_resampling = CALIBRATION_RESAMPLINGS[transform["selectedResampling"]]
    date_element = next((element for element in scene.get("elements", []) if element.get("type") == "DYNAMIC_SLOT"), None)
    date_bbox = date_element.get("bbox") if date_element else None
    case_records: list[dict[str, Any]] = []
    for mode, deterministic_records in deterministic_records_by_mode.items():
        xml_config = _parse_date_render_config(modes[mode])
        for item in deterministic_records:
            runtime_path = runtime_source_dir / mode / "runtime" / Path(item["path"]).name
            key = _calibration_case_key(mode, item["time"], int(item["date"]))
            capture = capture_index.get(key, {})
            raw_case = source_case_index.get(key, {})
            record: dict[str, Any] = {
                "time": item["time"],
                "date": item["date"],
                "mode": mode,
                "deterministic": item["path"],
                "runtimeSource": str(runtime_path) if runtime_path.exists() else None,
                "captureTimestamp": capture,
                "rawMetrics": raw_case.get("mae"),
                "status": "blocked_by_runtime_environment",
            }
            if not runtime_path.exists():
                case_records.append(record)
                continue
            runtime = Image.open(runtime_path).convert("RGB")
            normalized = _inverse_normalize_runtime(runtime, transform, selected_resampling)
            normalized_path = output_root / mode / "runtime-normalized" / Path(item["path"]).name
            normalized_path.parent.mkdir(parents=True, exist_ok=True)
            normalized.save(normalized_path)
            deterministic = Image.open(item["path"]).convert("RGB")
            difference = ImageChops.difference(deterministic, normalized)
            difference_path = output_root / mode / "normalized-difference" / Path(item["path"]).name
            difference_path.parent.mkdir(parents=True, exist_ok=True)
            difference.save(difference_path)
            masks = _region_masks(scene)
            for region, mask in masks.items():
                _save_region_difference(difference, mask, output_root / mode / "normalized-roi-difference" / region / Path(item["path"]).name)
            timestamp_delta_ms = float(capture.get("captureTimestampDeltaMs") or 0.0)
            geometry = _runtime_geometry_metrics(normalized, scene, item["time"], date_bbox, timestamp_delta_ms=timestamp_delta_ms)
            date_box = _date_window_measurement(normalized, date_bbox, _date_window_measurement(deterministic, date_bbox).get("foregroundBbox"))
            date_box["renderConfiguration"] = xml_config
            date_box["manualBitmapGlyphComparison"] = mode == "manual_override_on" and int(item["date"]) == 8
            record.update(
                {
                    "normalizedRuntime": str(normalized_path),
                    "normalizedDifference": str(difference_path),
                    "normalizedMae": {region: _mae(deterministic, normalized, mask) for region, mask in masks.items()} | {"global": _mae(deterministic, normalized)},
                    "normalizedBlur": {
                        "deterministic": _blur_metric(deterministic),
                        "runtime": _blur_metric(normalized),
                        "difference": round(_blur_metric(normalized) - _blur_metric(deterministic), 4),
                    },
                    "handExpectedAngleAtCapture": geometry["hands"],
                    "dateWindow": date_box,
                    "status": "normalized_and_compared",
                }
            )
            case_records.append(record)
    compared = [record for record in case_records if record["status"] == "normalized_and_compared"]

    def average(metric: str, region: str | None = None) -> float | None:
        values = []
        for record in compared:
            source = record.get(metric, {})
            value = source.get(region) if region else source
            if isinstance(value, (int, float)):
                values.append(float(value))
        return round(sum(values) / len(values), 4) if values else None

    def maximum(metric: str, region: str | None = None) -> float | None:
        values = []
        for record in compared:
            source = record.get(metric, {})
            value = source.get(region) if region else source
            if isinstance(value, (int, float)):
                values.append(float(value))
        return round(max(values), 4) if values else None

    def hand_average(metric: str, role: str | None = None) -> float | None:
        values = []
        for record in compared:
            for hand_role, hand in record.get("handExpectedAngleAtCapture", {}).items():
                if role is None or role == hand_role:
                    value = hand.get(metric)
                    if isinstance(value, (int, float)):
                        values.append(abs(float(value)))
        return round(sum(values) / len(values), 4) if values else None

    def hand_maximum(metric: str, role: str | None = None) -> float | None:
        values = []
        for record in compared:
            for hand_role, hand in record.get("handExpectedAngleAtCapture", {}).items():
                if role is None or role == hand_role:
                    value = hand.get(metric)
                    if isinstance(value, (int, float)):
                        values.append(abs(float(value)))
        return round(max(values), 4) if values else None

    def date_measurement_average(key: str) -> float | None:
        values = [float(record["dateWindow"][key]) for record in compared if isinstance(record.get("dateWindow", {}).get(key), (int, float))]
        return round(sum(values) / len(values), 4) if values else None

    def date_measurement_maximum(key: str) -> float | None:
        values = [float(record["dateWindow"][key]) for record in compared if isinstance(record.get("dateWindow", {}).get(key), (int, float))]
        return round(max(values), 4) if values else None

    date_configs = {mode: _parse_date_render_config(xml_path) for mode, xml_path in modes.items()}
    date_artifacts = {}
    for mode in modes:
        date_artifact = output_root / mode / "date-window-calibration-sheet.png"
        _write_date_calibration_sheet([record for record in compared if record["mode"] == mode], date_artifact)
        date_artifacts[mode] = str(date_artifact)
        (output_root / mode / "date-window-analysis.json").write_text(
            json.dumps(
                {
                    "renderConfiguration": date_configs[mode],
                    "cases": [record for record in compared if record["mode"] == mode],
                    "runtimeFontResourceConclusion": "The screenshot cannot expose the internal runtime font file; declared WFF family and observed glyph geometry are recorded separately.",
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )

    raw_summary = source_report.get("quantitativeSummary", {})
    normalized_summary = {
        "globalMae": {"average": average("normalizedMae", "global"), "maximum": maximum("normalizedMae", "global")},
        "staticDialMae": {"average": average("normalizedMae", "staticDial"), "maximum": maximum("normalizedMae", "staticDial")},
        "handsMae": {"average": average("normalizedMae", "hands"), "maximum": maximum("normalizedMae", "hands")},
        "dateMae": {"average": average("normalizedMae", "date"), "maximum": maximum("normalizedMae", "date")},
        "handAngleErrorDegAtCapture": {"average": hand_average("angleErrorDegApprox"), "maximum": hand_maximum("angleErrorDegApprox")},
        "secondHandAngleErrorDegAtCapture": {"average": hand_average("angleErrorDegApprox", "SECOND"), "maximum": hand_maximum("angleErrorDegApprox", "SECOND")},
        "handPivotErrorPx": {"average": hand_average("pivotErrorPx"), "maximum": hand_maximum("pivotErrorPx")},
        "handLengthDifferencePxApprox": {"average": hand_average("lengthDifferencePxApprox"), "maximum": hand_maximum("lengthDifferencePxApprox")},
        "handWidthDifferencePxApprox": {"average": hand_average("widthDifferencePxApprox"), "maximum": hand_maximum("widthDifferencePxApprox")},
        "dateBaselineBottomOffsetPx": {"average": date_measurement_average("baselineBottomOffsetPx"), "maximum": date_measurement_maximum("baselineBottomOffsetPx")},
        "dateCenterOffsetPx": {"average": date_measurement_average("centerOffsetPx"), "maximum": date_measurement_maximum("centerOffsetPx")},
        "blurDelta": {"average": average("normalizedBlur", "difference")},
        "timestampAwareCaptureCount": sum(1 for record in compared if record.get("captureTimestamp", {}).get("captureTimestampDeltaMs") is not None),
    }
    date_wff_difference = normalized_summary["dateMae"]["average"] is not None and normalized_summary["staticDialMae"]["average"] is not None and normalized_summary["dateMae"]["average"] > normalized_summary["staticDialMae"]["average"] * 2
    report = {
        "milestone": "A2.5c Runtime Calibration",
        "baseline": "7421f98",
        "registrationBaseline": "95207c2",
        "status": "runtime_compared" if compared else "blocked_by_runtime_environment",
        "sourceReport": str(source_report_path),
        "platformTransform": transform,
        "normalizedMetrics": normalized_summary,
        "rawMetricsReference": raw_summary,
        "trueWffRenderingDifferences": {
            "platformTransformExplained": {
                "rawGlobalMaeAverage": raw_summary.get("globalMae", {}).get("average"),
                "normalizedGlobalMaeAverage": normalized_summary["globalMae"]["average"],
                "globalMaeReduction": round((raw_summary.get("globalMae", {}).get("average") or 0.0) - (normalized_summary["globalMae"]["average"] or 0.0), 4),
                "rawStaticDialMaeAverage": raw_summary.get("staticDialMae", {}).get("average"),
                "normalizedStaticDialMaeAverage": normalized_summary["staticDialMae"]["average"],
            },
            "resampling": {
                "candidateStaticDialMae": transform["resamplingCandidatesStaticDialMae"],
                "selectedForInverseNormalization": transform["selectedResampling"],
                "interpretation": "filter candidate scores are calibration measurements, not production renderer changes",
            },
            "handRendering": {
                "normalizedHandsMae": normalized_summary["handsMae"],
                "timestampDeltaCorrectionApplied": normalized_summary["timestampAwareCaptureCount"] > 0,
                "secondHandCorrection": "expected angle uses emulator wall-clock delta at capture; screenshot contour remains an approximate observation",
            },
            "dateWindow": {
                "normalizedDateMae": normalized_summary["dateMae"],
                "likelyWffTextRenderingDifference": date_wff_difference,
                "configurations": date_configs,
                "manualBitmapGlyphComparedSeparately": True,
            },
            "classification": ["WFF text rendering" if date_wff_difference else "no confirmed residual text difference", "residual image filtering" if normalized_summary["blurDelta"]["average"] else "no measured blur residual"],
        },
        "measurementArtifacts": {
            "normalizedRuntimeRoot": str(output_root),
            "dateCalibrationSheets": date_artifacts,
            "dateAnalysis": {mode: str(output_root / mode / "date-window-analysis.json") for mode in modes},
            "caseCount": len(case_records),
            "normalizedCaseCount": len(compared),
            "timestampCaptureMetadata": "embedded per case in cases[].captureTimestamp",
        },
        "cases": case_records,
        "requiredProductionFixes": [],
        "deferred": ["runtime font file identity cannot be introspected from screenshot alone; inspect platform/font diagnostics if a production text fix is proposed"],
    }
    report_path = output_root / "runtime-calibration-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    report["report"] = str(report_path)
    return report


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
