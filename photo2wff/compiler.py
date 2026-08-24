from __future__ import annotations

import re
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from xml.dom import minidom

from .model import validate_scene
from .render import render_shape_asset
from .wff_validate import validate_wff_xml


def _xml_color(value: str) -> str:
    value = value.upper()
    if len(value) == 7:
        return value
    if len(value) == 9:
        return f"#{value[7:9]}{value[1:7]}"
    return value


def _weight(value: Any) -> str:
    value = int(value or 400)
    if value <= 200:
        return "THIN"
    if value <= 350:
        return "LIGHT"
    if value <= 500:
        return "NORMAL"
    if value <= 700:
        return "BOLD"
    return "BLACK"


def _font_attributes(style: dict[str, Any], include_text: bool = False) -> dict[str, str]:
    family = str(style.get("fontFamily", "SYNC_TO_DEVICE")).lower()
    if family == "sync_to_device":
        family = "SYNC_TO_DEVICE"
    attrs = {"family": family, "size": str(int(style.get("fontSize", 20))), "weight": _weight(style.get("fontWeight", 400)), "slant": "NORMAL", "color": _xml_color(style.get("color", "#FFFFFF"))}
    return attrs


def _text_alignment(value: Any) -> str:
    return {"LEFT": "START", "CENTER": "CENTER", "RIGHT": "END"}.get(str(value).upper(), "CENTER")


def _part_text(element: dict[str, Any], expression: str | None = None, template: str | None = None, manual_glyphs: dict[str, Any] | None = None) -> ET.Element:
    bbox = element["bbox"]
    style = element.get("style", {})
    part = ET.Element("PartText", {"x": str(bbox["x"]), "y": str(bbox["y"]), "width": str(bbox["width"]), "height": str(bbox["height"])})
    text = ET.SubElement(part, "Text", {"align": _text_alignment(style.get("alignment", "center"))})
    if manual_glyphs:
        font = ET.SubElement(text, "BitmapFont", {"family": str(manual_glyphs["family"]), "size": str(int(style.get("fontSize", 24))), "color": _xml_color(style.get("color", "#FFFFFF"))})
    else:
        font = ET.SubElement(text, "Font", _font_attributes(style))
    if template is not None:
        template_node = ET.SubElement(font, "Template")
        template_node.text = template
        ET.SubElement(template_node, "Parameter", {"expression": expression or "[DAY]"})
    elif expression is not None:
        template_node = ET.SubElement(font, "Template")
        template_node.text = "%d" if expression == "[DAY]" else "%s"
        ET.SubElement(template_node, "Parameter", {"expression": expression})
    else:
        font.text = str(element.get("text", ""))
    if element.get("launch"):
        ET.SubElement(part, "Launch", {"target": str(element["launch"])})
    return part


def _time_clock(element: dict[str, Any]) -> ET.Element:
    bbox = element["bbox"]
    style = element.get("style", {})
    clock = ET.Element("DigitalClock", {"x": str(bbox["x"]), "y": str(bbox["y"]), "width": str(bbox["width"]), "height": str(bbox["height"])})
    time_format = element.get("format", "hh:mm").replace("HH", "hh")
    interactive = ET.SubElement(clock, "TimeText", {"format": time_format, "hourFormat": "SYNC_TO_DEVICE", "align": "CENTER", "x": "0", "y": "0", "width": str(bbox["width"]), "height": str(bbox["height"]), "alpha": "255"})
    ET.SubElement(interactive, "Variant", {"mode": "AMBIENT", "target": "alpha", "value": "0"})
    ET.SubElement(interactive, "Font", _font_attributes(style))
    ambient = ET.SubElement(clock, "TimeText", {"format": time_format, "hourFormat": "SYNC_TO_DEVICE", "align": "CENTER", "x": "0", "y": "0", "width": str(bbox["width"]), "height": str(bbox["height"]), "alpha": "0"})
    ET.SubElement(ambient, "Variant", {"mode": "AMBIENT", "target": "alpha", "value": "255"})
    ambient_style = dict(style)
    ambient_style["fontWeight"] = min(int(style.get("fontWeight", 400)), 300)
    ET.SubElement(ambient, "Font", _font_attributes(ambient_style))
    return clock


def _asset_part(element: dict[str, Any], resource: str) -> ET.Element:
    bbox = element["bbox"]
    part = ET.Element("PartImage", {"x": str(bbox["x"]), "y": str(bbox["y"]), "width": str(bbox["width"]), "height": str(bbox["height"])})
    ET.SubElement(part, "Image", {"resource": resource})
    return part


def _append_manual_glyph_fonts(root: ET.Element, scene: dict[str, Any], resource_names: dict[str, str]) -> None:
    definitions: dict[str, dict[str, Any]] = {}
    for element in scene["elements"]:
        manual = element.get("manualGlyphs")
        if manual:
            definitions.setdefault(str(manual["family"]), manual)
    if not definitions:
        return
    fonts_node = ET.SubElement(root, "BitmapFonts")
    for family, manual in sorted(definitions.items()):
        font_node = ET.SubElement(fonts_node, "BitmapFont", {"name": family})
        metrics = manual.get("metrics", {})
        for character, asset in sorted(manual.get("resources", {}).items()):
            if asset not in resource_names:
                raise ValueError(f"manual glyph '{character}' references missing asset '{asset}'")
            glyph_metrics = metrics.get(character, {})
            ET.SubElement(font_node, "Character", {"name": str(character), "resource": resource_names[asset], "width": str(int(glyph_metrics.get("width", 1))), "height": str(int(glyph_metrics.get("height", 1)))})


def _manual_days(manual: dict[str, Any], padded: bool) -> list[int]:
    available = set(str(character) for character in manual.get("resources", {}))
    return [
        day
        for day in range(1, 32)
        if set(f"{day:02d}" if padded else str(day)).issubset(available)
    ]


def _dynamic_day_part(element: dict[str, Any]) -> ET.Element:
    padded = element.get("format", "d") == "dd"
    expression = "[DAY_Z]" if padded else "[DAY]"
    fallback = _part_text(element, expression)
    manual = element.get("manualGlyphs") or {}
    if not manual.get("resources"):
        return fallback
    manual_days = _manual_days(manual, padded)
    if not manual_days:
        return fallback
    if len(manual_days) == 31:
        return _part_text(element, expression, manual_glyphs=manual)
    condition = ET.Element("Condition")
    expressions = ET.SubElement(condition, "Expressions")
    availability = " || ".join(f"[DAY] == {day}" for day in manual_days)
    ET.SubElement(expressions, "Expression", {"name": "manual_date_available"}).text = availability
    ET.SubElement(condition, "Compare", {"expression": "manual_date_available"}).append(
        _part_text(element, expression, manual_glyphs=manual)
    )
    ET.SubElement(condition, "Default").append(fallback)
    return condition


def _pretty_xml(root: ET.Element) -> str:
    raw = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    formatted = minidom.parseString(raw).toprettyxml(indent="  ", encoding="utf-8").decode("utf-8")
    template_pattern = re.compile(r"<Template>\s*([^<]*?)\s*((?:<Parameter\b[^>]*/>\s*)+)</Template>", re.DOTALL)

    def compact_template(match: re.Match[str]) -> str:
        parameters = re.sub(r">\s+<", "><", match.group(2).strip())
        return f"<Template>{match.group(1).strip()}{parameters}</Template>"

    return template_pattern.sub(compact_template, formatted)


def compile_watchface_xml(scene: dict[str, Any], resource_names: dict[str, str]) -> str:
    scene = validate_scene(scene)
    root = ET.Element("WatchFace", {"width": "438", "height": "438"})
    is_analog = any(element["type"] == "ANALOG_HAND" for element in scene["elements"])
    ET.SubElement(root, "Metadata", {"key": "CLOCK_TYPE", "value": "ANALOG" if is_analog else "DIGITAL"})
    ET.SubElement(root, "Metadata", {"key": "PREVIEW_TIME", "value": str(scene.get("preview", {}).get("time", "10:08:32"))})
    ET.SubElement(root, "Metadata", {"key": "PREVIEW_DATE", "value": str(scene.get("preview", {}).get("date", "08.20"))})
    _append_manual_glyph_fonts(root, scene, resource_names)
    background = scene["background"]
    if str(background.get("type", "")).upper() not in {"SOLID", "UNKNOWN", "IMAGE"}:
        raise ValueError(f"background type '{background.get('type')}' needs a rasterized background asset before WFF compilation")
    scene_node = ET.SubElement(root, "Scene", {"backgroundColor": _xml_color(background.get("color", "#000000"))})
    def append_element(element: dict[str, Any]) -> None:
        element_type = element["type"]
        if element_type == "TIME":
            scene_node.append(_time_clock(element))
        elif element_type == "HOUR":
            scene_node.append(_part_text(element, "[HOUR_0_23_Z]"))
        elif element_type == "MINUTE":
            scene_node.append(_part_text(element, "[MINUTE_Z]"))
        elif element_type == "SECOND":
            scene_node.append(_part_text(element, "[SECOND_Z]"))
        elif element_type == "DATE":
            scene_node.append(_part_text(element, "[MONTH_Z]", "%s.%s"))
            date_part = scene_node[-1]
            parameters = date_part.find("Text/Font/Template")
            ET.SubElement(parameters, "Parameter", {"expression": "[DAY_Z]"})
        elif element_type == "DYNAMIC_SLOT":
            if element.get("slotType") != "DATE_DAY_OF_MONTH":
                raise ValueError(f"element '{element['id']}' has unsupported DYNAMIC_SLOT type")
            scene_node.append(_dynamic_day_part(element))
        elif element_type == "WEEKDAY":
            scene_node.append(_part_text(element, "[DAY_OF_WEEK_S]"))
        elif element_type == "BATTERY":
            scene_node.append(_part_text(element, "[BATTERY_PERCENT]", "%s%%"))
        elif element_type == "STEPS":
            scene_node.append(_part_text(element, "[STEP_COUNT]"))
        elif element_type == "HEART_RATE":
            scene_node.append(_part_text(element, "round([HEART_RATE])"))
        elif element_type == "TEXT":
            asset = element.get("asset")
            if asset in resource_names:
                scene_node.append(_asset_part(element, resource_names[asset]))
            else:
                scene_node.append(_part_text(element))
        elif element_type in {"IMAGE", "ICON", "STATIC_IMAGE", "STATIC_ARTWORK"}:
            asset = element.get("asset")
            if asset not in resource_names:
                raise ValueError(f"element '{element['id']}' references missing asset '{asset}'")
            scene_node.append(_asset_part(element, resource_names[asset]))
        elif element_type == "ANALOG_HAND":
            raise AssertionError("ANALOG_HAND elements are emitted inside AnalogClock")
        elif element_type in {"RECTANGLE", "CIRCLE", "LINE", "RING"}:
            resource = resource_names[element["id"]]
            scene_node.append(_asset_part(element, resource))
        elif element_type == "COMPLICATION":
            raise ValueError(f"element '{element['id']}' is COMPLICATION; explicit complication slots are deferred until their WFF schema is selected")
        else:
            raise ValueError(f"element '{element['id']}' has no compiler mapping")

    ordered = sorted(scene["elements"], key=lambda element: int(element.get("zIndex", 0)))
    hand_elements = [element for element in ordered if element["type"] == "ANALOG_HAND"]
    if not hand_elements:
        for element in ordered:
            append_element(element)
        return _pretty_xml(root)

    first_hand_z = min(int(element.get("zIndex", 0)) for element in hand_elements)
    for element in ordered:
        if element["type"] != "ANALOG_HAND" and int(element.get("zIndex", 0)) < first_hand_z:
            append_element(element)

    clock_meta = scene.get("clock", {})
    analog = ET.Element("AnalogClock", {"x": "0", "y": "0", "width": "438", "height": "438", "pivotX": "0.5", "pivotY": "0.5"})
    for element in hand_elements:
        bbox = element["bbox"]
        role = element["role"].title() + "Hand"
        asset = element["asset"]
        if asset not in resource_names:
            raise ValueError(f"element '{element['id']}' references missing asset '{asset}'")
        hand = ET.SubElement(
            analog,
            role,
            {
                "resource": resource_names[asset],
                "x": str(bbox["x"]),
                "y": str(bbox["y"]),
                "width": str(bbox["width"]),
                "height": str(bbox["height"]),
                "pivotX": str(element["pivotX"]),
                "pivotY": str(element["pivotY"]),
            },
        )
        if element["role"] == "SECOND" and element.get("sweepFrequency"):
            ET.SubElement(hand, "Sweep", {"frequency": str(element["sweepFrequency"])})
    scene_node.append(analog)

    for element in ordered:
        if element["type"] != "ANALOG_HAND" and int(element.get("zIndex", 0)) >= first_hand_z:
            append_element(element)
    return _pretty_xml(root)


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def compile_project(scene: dict[str, Any], project_dir: Path, source_root: Path) -> None:
    scene = validate_scene(scene)
    template_root = Path(__file__).resolve().parent.parent / "templates" / "wff-minimal"
    if template_root.exists():
        for template_file in template_root.rglob("*"):
            if template_file.is_file() and template_file.name != "README.md":
                relative = template_file.relative_to(template_root)
                destination = project_dir / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(template_file, destination)
    res = project_dir / "watchface" / "src" / "main" / "res"
    drawable = res / "drawable"
    raw = res / "raw"
    values = res / "values"
    font_dir = res / "font"
    for directory in (drawable, raw, values, font_dir):
        directory.mkdir(parents=True, exist_ok=True)
    resource_names: dict[str, str] = {}
    assets_root = source_root / "assets"
    for element in scene["elements"]:
        asset = element.get("asset")
        if not asset:
            continue
        source_asset = source_root / asset
        if not source_asset.exists():
            raise ValueError(f"element '{element['id']}' references missing asset '{asset}'")
        resource_name = Path(asset).stem.lower().replace("-", "_")
        resource_names[asset] = resource_name
        shutil.copy2(source_asset, drawable / f"{resource_name}.png")
    for element in scene["elements"]:
        manual = element.get("manualGlyphs") or {}
        for manual_asset in manual.get("resources", {}).values():
            source_manual_asset = source_root / manual_asset
            if not source_manual_asset.exists():
                raise ValueError(f"manual glyph references missing asset '{manual_asset}'")
            manual_name = Path(manual_asset).stem.replace("-", "_").lower()
            resource_names[manual_asset] = manual_name
            shutil.copy2(source_manual_asset, drawable / f"{manual_name}.png")
    for element in scene["elements"]:
        if element["type"] in {"RECTANGLE", "CIRCLE", "LINE", "RING"}:
            resource_name = element["id"].lower().replace("-", "_")
            render_shape_asset(element, drawable / f"{resource_name}.png")
            resource_names[element["id"]] = resource_name
    preview_path = source_root / "preview.png"
    if not preview_path.exists():
        preview_path = source_root / "preview_initial.png"
    if preview_path.exists():
        shutil.copy2(preview_path, drawable / "preview.png")
    pretendard = assets_root / "fonts" / "pretendard.ttf"
    if pretendard.exists():
        shutil.copy2(pretendard, font_dir / "pretendard.ttf")
    else:
        for element in scene["elements"]:
            if element.get("style", {}).get("fontFamily", "").lower() == "pretendard":
                element.setdefault("style", {})["fontFamily"] = "SYNC_TO_DEVICE"
    xml = compile_watchface_xml(scene, resource_names)
    _write_text(raw / "watchface.xml", xml)
    validate_wff_xml(raw / "watchface.xml")
    _write_text(
        project_dir / "settings.gradle.kts",
        """pluginManagement { repositories { google(); mavenCentral(); gradlePluginPortal() } }\ndependencyResolutionManagement { repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS); repositories { google(); mavenCentral() } }\nrootProject.name = \"Photo2WFF\"\ninclude(\":watchface\")\n""",
    )
    _write_text(project_dir / "build.gradle.kts", "plugins { alias(libs.plugins.android.application) apply false }\n")
    _write_text(project_dir / "gradle.properties", "org.gradle.jvmargs=-Xmx2g -Dfile.encoding=UTF-8\nandroid.useAndroidX=true\n")
    _write_text(project_dir / "gradle/libs.versions.toml", """[versions]\nandroidGradlePlugin = \"9.0.0\"\n[plugins]\nandroid-application = { id = \"com.android.application\", version.ref = \"androidGradlePlugin\" }\n""")
    _write_text(
        project_dir / "watchface/build.gradle.kts",
        """plugins { alias(libs.plugins.android.application) }\n\nandroid {\n    namespace = \"com.photo2wff.watchface\"\n    compileSdk = 34\n    defaultConfig {\n        applicationId = \"com.photo2wff.watchface\"\n        minSdk = 33\n        targetSdk = 34\n        versionCode = 1\n        versionName = \"0.1.0\"\n    }\n    buildTypes {\n        debug { isMinifyEnabled = true }\n        release { isMinifyEnabled = true; isShrinkResources = false; signingConfig = signingConfigs.getByName(\"debug\") }\n    }\n}\n""",
    )
    _write_text(
        project_dir / "watchface/src/main/AndroidManifest.xml",
        """<manifest xmlns:android=\"http://schemas.android.com/apk/res/android\">\n  <uses-feature android:name=\"android.hardware.type.watch\" android:required=\"true\" />\n  <application android:label=\"@string/watch_face_name\" android:icon=\"@drawable/preview\" android:hasCode=\"false\">\n    <meta-data android:name=\"com.google.android.wearable.standalone\" android:value=\"true\" />\n    <property android:name=\"com.google.wear.watchface.format.version\" android:value=\"1\" />\n    <property android:name=\"com.google.wear.watchface.format.publisher\" android:value=\"Photo2WFF-0.1.0\" />\n  </application>\n</manifest>\n""",
    )
    _write_text(values / "strings.xml", "<resources><string name=\"watch_face_name\">Photo2WFF</string></resources>\n")
    _write_text(res / "xml/watch_face_info.xml", """<WatchFaceInfo>\n  <Preview value=\"@drawable/preview\" />\n  <Category value=\"CATEGORY_EMPTY\" />\n  <AvailableInRetail value=\"true\" />\n  <MultipleInstancesAllowed value=\"true\" />\n  <Editable value=\"false\" />\n  <FlavorsSupported value=\"false\" />\n</WatchFaceInfo>\n""")
