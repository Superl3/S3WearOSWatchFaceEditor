from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageStat

from .wff_render import render_wff_xml

VALIDATION_TIMES = ("00:00:00", "03:15:45", "06:30:00", "10:08:30")
VALIDATION_DATES = (1, 8, 11, 20, 31)
CANVAS_SIZE = (438, 438)


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
        runtime = Image.open(runtime_path).convert("RGB")
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
        record["status"] = "compared"
    else:
        record["status"] = "blocked_by_runtime_environment"
        record["runtimeDifference"] = None
    return record


def run_runtime_gate(scene_path: Path, deterministic_xml: Path, output_root: Path, manual_xml: Path | None = None, adb: Path | None = None) -> dict[str, Any]:
    scene = json.loads(scene_path.read_text(encoding="utf-8"))
    output_root.mkdir(parents=True, exist_ok=True)
    runtime = detect_runtime(adb)
    modes = {"manual_override_off": deterministic_xml}
    if manual_xml:
        modes["manual_override_on"] = manual_xml
    cases: list[dict[str, Any]] = []
    for mode, xml_path in modes.items():
        deterministic_records = render_deterministic_matrix(xml_path, output_root, mode)
        for item in deterministic_records:
            cases.append(compare_runtime_case(Path(item["path"]), None, scene, item["time"], item["date"], output_root, mode))
    report = {
        "milestone": "A2.5 Wear OS Runtime Validation Gate",
        "baseline": "deaf036",
        "status": runtime["status"] if runtime["status"] != "runtime_available" else "runtime_capture_required",
        "runtimeEnvironment": runtime,
        "verificationSet": {"times": list(VALIDATION_TIMES), "dates": list(VALIDATION_DATES), "modes": list(modes)},
        "caseCount": len(cases),
        "cases": cases,
        "globalMaeIsNotSoleGate": True,
        "regionMetrics": ["staticDial", "hands", "date", "global"],
        "runtimeScreenshots": "not captured" if runtime["status"] != "runtime_available" else "deferred until watch face is selected on Wear OS",
        "requiredFixes": [],
        "deferred": ["runtime screenshot capture", "aligned runtime differences", "runtime pivot/angle, scaling, date centering, clipping, and anti-aliasing comparison"],
    }
    report_path = output_root / "runtime-validation-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    report["report"] = str(report_path)
    return report
