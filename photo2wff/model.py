from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .display_geometry import RoundedRect


CANVAS_SIZE = 438
SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schemas" / "scene.schema.json"
SUPPORTED_TYPES = {
    "TIME",
    "HOUR",
    "MINUTE",
    "SECOND",
    "DATE",
    "DYNAMIC_SLOT",
    "WEEKDAY",
    "TEXT",
    "ICON",
    "IMAGE",
    "STATIC_IMAGE",
    "ANALOG_HAND",
    "BATTERY",
    "STEPS",
    "HEART_RATE",
    "COMPLICATION",
    "RING",
    "LINE",
    "RECTANGLE",
    "CIRCLE",
}
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$")


class SceneValidationError(ValueError):
    """Raised when a scene cannot be compiled deterministically."""

    code = "SCENE_INVALID"

    def __init__(self, path: str, message: str):
        self.path = path or "$"
        self.message = message
        super().__init__(f"[{self.code}] {self.path}: {self.message}")

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "message": self.message}


def _fail(path: str, message: str) -> None:
    raise SceneValidationError(path, message)


def _number(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(path, "must be a number")
    return float(value)


def _color(value: Any, path: str) -> str:
    if not isinstance(value, str) or not COLOR_RE.match(value):
        _fail(path, "must be #RRGGBB or #RRGGBBAA")
    return value.upper()


def validate_scene(scene: dict[str, Any]) -> dict[str, Any]:
    """Validate and return a deep copy of a canonical scene."""
    if not isinstance(scene, dict):
        _fail("scene", "must be an object")
    try:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    except OSError as error:
        _fail("schema", f"cannot load scene.schema.json: {error}")
    schema_errors = sorted(Draft202012Validator(schema).iter_errors(scene), key=lambda error: list(error.path))
    if schema_errors:
        error = schema_errors[0]
        path = ".".join(str(part) for part in error.path) or "$"
        _fail(path, error.message)
    for key in ("schemaVersion", "canvas", "normalization", "background", "elements", "analysis"):
        if key not in scene:
            _fail("scene", f"missing required key '{key}'")

    canvas = scene["canvas"]
    if not isinstance(canvas, dict):
        _fail("canvas", "must be an object")
    if canvas.get("width") != CANVAS_SIZE or canvas.get("height") != CANVAS_SIZE:
        _fail("canvas", "width and height must both be 438 for the MVP")
    if str(canvas.get("shape", "")).upper() != "CIRCLE":
        _fail("canvas.shape", "must be 'CIRCLE'")
    for key, expected in (("centerX", 219), ("centerY", 219)):
        if canvas.get(key, expected) != expected:
            _fail(f"canvas.{key}", f"must be {expected}")

    clock = scene.get("clock")
    if clock is not None:
        if not isinstance(clock, dict) or clock.get("type") != "ANALOG":
            _fail("clock", "must be an ANALOG clock object")
        for key in ("centerX", "centerY", "confidence"):
            _number(clock.get(key), f"clock.{key}")
        if not 0 <= float(clock["confidence"]) <= 1:
            _fail("clock.confidence", "must be between 0 and 1")

    display_geometry = scene.get("displayGeometry")
    if display_geometry is not None:
        if not isinstance(display_geometry, dict):
            _fail("displayGeometry", "must be an object")
        if display_geometry.get("mappingPolicy") != "CENTER_PRESERVING_BOUNDARY_NORMALIZED":
            _fail("displayGeometry.mappingPolicy", "must use center-preserving boundary-normalized mapping")
        for key in ("source", "target"):
            value = display_geometry.get(key)
            if not isinstance(value, dict):
                _fail(f"displayGeometry.{key}", "must be an object")
            try:
                shape = RoundedRect.from_dict(value)
            except (KeyError, TypeError, ValueError) as error:
                _fail(f"displayGeometry.{key}", str(error))
            declared_shape = str(value.get("shape", "")).upper()
            if declared_shape == "CIRCLE" and not shape.is_circle:
                _fail(f"displayGeometry.{key}.shape", "CIRCLE requires width == height and radius == width / 2")
            if declared_shape == "ROUNDED_RECT" and shape.is_circle:
                _fail(f"displayGeometry.{key}.shape", "a circle must use the CIRCLE special-case label")
            if "isCircleSpecialCase" in value and bool(value["isCircleSpecialCase"]) != shape.is_circle:
                _fail(f"displayGeometry.{key}.isCircleSpecialCase", "does not match width, height, and radius")

    normalization = scene["normalization"]
    if not isinstance(normalization, dict):
        _fail("normalization", "must be an object")
    if normalization.get("inputType") not in {"SCREENSHOT", "PHOTOGRAPH", "CROPPED_SCREEN", "UNCERTAIN"}:
        _fail("normalization.inputType", "must identify the input as SCREENSHOT, PHOTOGRAPH, CROPPED_SCREEN, or UNCERTAIN")
    for key in ("rotationDegrees", "confidence"):
        if key in normalization:
            value = _number(normalization[key], f"normalization.{key}")
            if key == "confidence" and not 0 <= value <= 1:
                _fail("normalization.confidence", "must be between 0 and 1")

    background = scene["background"]
    if not isinstance(background, dict):
        _fail("background", "must be an object")
    background_type = str(background.get("type", "")).upper()
    if background_type == "SOLID":
        _color(background.get("color"), "background.color")
    elif background_type in {"LINEAR_GRADIENT", "RADIAL_GRADIENT"}:
        if not isinstance(background.get("colors"), list) or len(background["colors"]) < 2:
            _fail("background.colors", "gradient backgrounds require at least two colors")
        for index, color in enumerate(background["colors"]):
            _color(color, f"background.colors[{index}]")
    elif background_type == "IMAGE":
        if not isinstance(background.get("asset"), str) and not isinstance(background.get("resourceHint"), str):
            _fail("background", "IMAGE backgrounds require asset or resourceHint")
    elif background_type == "UNKNOWN":
        pass
    else:
        _fail("background.type", "must be SOLID, LINEAR_GRADIENT, RADIAL_GRADIENT, IMAGE, or UNKNOWN")

    elements = scene["elements"]
    if not isinstance(elements, list):
        _fail("elements", "must be an array")
    ids: set[str] = set()
    for index, element in enumerate(elements):
        path = f"elements[{index}]"
        if not isinstance(element, dict):
            _fail(path, "must be an object")
        element_id = element.get("id")
        if not isinstance(element_id, str) or not element_id:
            _fail(f"{path}.id", "must be a non-empty string")
        if element_id in ids:
            _fail(f"{path}.id", f"duplicate id '{element_id}'")
        ids.add(element_id)
        element_type = element.get("type")
        if element_type not in SUPPORTED_TYPES:
            _fail(f"{path}.type", f"unsupported primitive '{element_type}'")
        if not isinstance(element.get("dynamic"), bool):
            _fail(f"{path}.dynamic", "must be boolean")
        bbox = element.get("bbox")
        if not isinstance(bbox, dict):
            _fail(f"{path}.bbox", "must be an object")
        for coordinate in ("x", "y", "width", "height"):
            value = _number(bbox.get(coordinate), f"{path}.bbox.{coordinate}")
            if value < 0 or value > CANVAS_SIZE:
                _fail(f"{path}.bbox.{coordinate}", "must be between 0 and 438")
        if bbox["x"] + bbox["width"] > CANVAS_SIZE or bbox["y"] + bbox["height"] > CANVAS_SIZE:
            _fail(f"{path}.bbox", "must remain inside the 438×438 canvas")
        confidence = _number(element.get("confidence", 1.0), f"{path}.confidence")
        if not 0 <= confidence <= 1:
            _fail(f"{path}.confidence", "must be between 0 and 1")
        style = element.get("style", {})
        if not isinstance(style, dict):
            _fail(f"{path}.style", "must be an object")
        if "color" in style:
            _color(style["color"], f"{path}.style.color")
        for number_key in ("fontSize", "fontWeight", "letterSpacing", "alpha", "strokeWidth"):
            if number_key in style:
                _number(style[number_key], f"{path}.style.{number_key}")
        if element_type in {"TIME", "HOUR", "MINUTE", "SECOND"}:
            if not element["dynamic"]:
                _fail(f"{path}.dynamic", f"{element_type} must be dynamic")
            if element_type == "TIME" and element.get("format", "hh:mm") not in {"hh:mm", "HH:mm", "hh:mm:ss"}:
                _fail(f"{path}.format", "supported values are hh:mm, HH:mm, hh:mm:ss")
        if element_type == "DYNAMIC_SLOT":
            if not element["dynamic"]:
                _fail(f"{path}.dynamic", "DYNAMIC_SLOT must be dynamic")
            if element.get("slotType") != "DATE_DAY_OF_MONTH":
                _fail(f"{path}.slotType", "A2 supports only DATE_DAY_OF_MONTH")
            if element.get("format", "d") not in {"d", "dd"}:
                _fail(f"{path}.format", "DATE_DAY_OF_MONTH supports only d or dd")
            manual = element.get("manualGlyphs")
            if manual is not None:
                if not isinstance(manual, dict) or manual.get("type") != "MANUAL_GLYPH_OVERRIDE":
                    _fail(f"{path}.manualGlyphs", "must be a MANUAL_GLYPH_OVERRIDE object")
                if not isinstance(manual.get("family"), str) or not manual["family"]:
                    _fail(f"{path}.manualGlyphs.family", "must be a non-empty string")
                resources = manual.get("resources")
                if not isinstance(resources, dict) or not resources:
                    _fail(f"{path}.manualGlyphs.resources", "must contain at least one digit resource")
                for character, resource in resources.items():
                    if character not in "0123456789" or not isinstance(resource, str) or not resource:
                        _fail(f"{path}.manualGlyphs.resources", "keys must be digits and values must be asset paths")
        if element_type == "ANALOG_HAND":
            if not element["dynamic"]:
                _fail(f"{path}.dynamic", "ANALOG_HAND must be dynamic")
            if element.get("role") not in {"HOUR", "MINUTE", "SECOND"}:
                _fail(f"{path}.role", "must be HOUR, MINUTE, or SECOND")
            for number_key in ("observedAngleDeg", "length", "thickness", "pivotX", "pivotY"):
                if number_key not in element:
                    _fail(f"{path}.{number_key}", "is required for ANALOG_HAND")
                _number(element[number_key], f"{path}.{number_key}")
            if not 0 <= float(element["pivotX"]) <= 1 or not 0 <= float(element["pivotY"]) <= 1:
                _fail(f"{path}.pivot", "pivotX and pivotY must be between 0 and 1")
            if not isinstance(element.get("asset"), str) or not element["asset"]:
                _fail(f"{path}.asset", "ANALOG_HAND requires a canonical transparent asset")
        if element_type in {"IMAGE", "ICON", "STATIC_IMAGE"} and not isinstance(element.get("asset"), str) and not isinstance(element.get("assetInstruction"), dict):
            _fail(f"{path}", f"{element_type} requires asset or assetInstruction")
        if element_type == "TEXT" and not isinstance(element.get("text", ""), str):
            _fail(f"{path}.text", "must be a string")
        if element_type == "COMPLICATION" and not isinstance(element.get("dataSource", ""), str):
            _fail(f"{path}.dataSource", "must be a WFF data source name")
        if "rotation" in element:
            _number(element["rotation"], f"{path}.rotation")
        if "uncertainty" in element and not isinstance(element["uncertainty"], list):
            _fail(f"{path}.uncertainty", "must be an array of strings")

    analysis = scene["analysis"]
    if not isinstance(analysis, dict):
        _fail("analysis", "must be an object")
    if analysis.get("watchFaceCategory") not in {"MINIMAL_DIGITAL", "MINIMAL_ANALOG"}:
        _fail("analysis.watchFaceCategory", "must be MINIMAL_DIGITAL or MINIMAL_ANALOG")
    overall_confidence = _number(analysis.get("overallConfidence"), "analysis.overallConfidence")
    if not 0 <= overall_confidence <= 1:
        _fail("analysis.overallConfidence", "must be between 0 and 1")
    for key in ("requiresStaticAssetExtraction", "requiresHumanReview"):
        if not isinstance(analysis.get(key), bool):
            _fail(f"analysis.{key}", "must be boolean")

    return copy.deepcopy(scene)


def load_scene(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return validate_scene(json.load(handle))


def save_scene(scene: dict[str, Any], path: Path) -> None:
    validated = validate_scene(scene)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(validated, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def apply_patches(scene: dict[str, Any], patches: list[dict[str, Any]]) -> dict[str, Any]:
    result = copy.deepcopy(scene)
    by_id = {element["id"]: element for element in result["elements"]}
    for patch in patches:
        element = by_id.get(patch.get("element"))
        if element is None:
            _fail("patch.element", f"unknown element '{patch.get('element')}'")
        property_path = patch.get("property", "")
        if not isinstance(property_path, str) or not property_path:
            _fail("patch.property", "must be a non-empty property path")
        target: Any = element
        parts = property_path.split(".")
        for part in parts[:-1]:
            if not isinstance(target, dict) or part not in target:
                _fail(f"patch.{property_path}", "path does not exist")
            target = target[part]
        leaf = parts[-1]
        if not isinstance(target, dict) or leaf not in target:
            _fail(f"patch.{property_path}", "path does not exist")
        delta = patch.get("delta")
        if not isinstance(delta, (int, float)):
            _fail("patch.delta", "must be numeric")
        if not isinstance(target[leaf], (int, float)):
            _fail(f"patch.{property_path}", "delta patches require a numeric property")
        target[leaf] += delta
    return validate_scene(result)


def editable_yaml(scene: dict[str, Any]) -> str:
    lines = ["# Human-editable projection of scene.json", "canvas:", "  width: 438", "  height: 438", "elements:"]
    for element in scene["elements"]:
        bbox = element["bbox"]
        style = element.get("style", {})
        lines.extend(
            [
                f"  {element['id']}:",
                f"    type: {element['type']}",
                f"    dynamic: {str(element['dynamic']).lower()}",
                f"    x: {bbox['x']}",
                f"    y: {bbox['y']}",
                f"    width: {bbox['width']}",
                f"    height: {bbox['height']}",
            ]
        )
        for key in ("fontFamily", "fontWeight", "fontSize", "letterSpacing", "alignment", "color"):
            if key in style:
                lines.append(f"    {key}: {style[key]}")
        if "format" in element:
            lines.append(f"    format: {element['format']}")
        if "asset" in element:
            lines.append(f"    asset: {element['asset']}")
        if "text" in element:
            lines.append(f"    text: {element['text']}")
    return "\n".join(lines) + "\n"
