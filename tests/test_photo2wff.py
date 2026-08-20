from __future__ import annotations

import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from photo2wff.analyzer import analyze_image
from photo2wff.compare import compare_images, suggest_patches
from photo2wff.compiler import compile_watchface_xml
from photo2wff.model import SceneValidationError, apply_patches, validate_scene
from photo2wff.render import render_scene
from photo2wff.wff_validate import validate_wff_xml


def basic_scene() -> dict:
    return {
        "schemaVersion": "1.0",
        "canvas": {"width": 438, "height": 438, "shape": "CIRCLE", "centerX": 219, "centerY": 219},
        "normalization": {"inputType": "SCREENSHOT", "rotationDegrees": 0.0, "confidence": 0.98, "requiresPerspectiveCorrection": False},
        "background": {"type": "SOLID", "color": "#000000"},
        "preview": {"time": "10:08", "date": "08.20", "weekday": "THU"},
        "elements": [
            {"id": "time_primary", "type": "TIME", "dynamic": True, "bbox": {"x": 80, "y": 130, "width": 278, "height": 95}, "format": "hh:mm", "style": {"fontFamily": "Pretendard", "fontSize": 92, "fontWeight": 400, "color": "#FFFFFF"}, "confidence": 1},
            {"id": "date", "type": "DATE", "dynamic": True, "bbox": {"x": 160, "y": 250, "width": 118, "height": 32}, "style": {"fontFamily": "Pretendard", "fontSize": 22, "color": "#9EA4AD"}, "confidence": 1},
        ],
        "analysis": {"watchFaceCategory": "MINIMAL_DIGITAL", "overallConfidence": 1.0, "requiresStaticAssetExtraction": False, "requiresHumanReview": False},
    }


class Photo2WFFTests(unittest.TestCase):
    def test_scene_validation_rejects_out_of_bounds(self):
        scene = basic_scene()
        scene["elements"][0]["bbox"]["x"] = 300
        with self.assertRaises(SceneValidationError):
            validate_scene(scene)

    def test_scene_validation_has_machine_readable_error(self):
        scene = basic_scene()
        scene["elements"][0]["type"] = "NOT_A_PRIMITIVE"
        with self.assertRaises(SceneValidationError) as context:
            validate_scene(scene)
        self.assertEqual(context.exception.code, "SCENE_INVALID")
        self.assertIn("type", context.exception.path)

    def test_compiler_emits_parseable_native_time_and_date(self):
        xml = compile_watchface_xml(basic_scene(), {})
        root = ET.fromstring(xml)
        self.assertEqual(root.tag, "WatchFace")
        self.assertIsNotNone(root.find(".//DigitalClock/TimeText"))
        self.assertEqual(root.find(".//PartText/Text/Font/Template/Parameter").attrib["expression"], "[MONTH_Z]")

    def test_wff_structural_validation(self):
        with tempfile.TemporaryDirectory() as temp:
            xml_path = Path(temp) / "watchface.xml"
            xml_path.write_text(compile_watchface_xml(basic_scene(), {}), encoding="utf-8")
            self.assertEqual(validate_wff_xml(xml_path), "structural validation passed")

    def test_split_time_primitives_compile(self):
        scene = basic_scene()
        scene["elements"] = [{"id": "hour", "type": "HOUR", "dynamic": True, "bbox": {"x": 100, "y": 100, "width": 80, "height": 80}, "style": {"fontFamily": "Pretendard", "fontSize": 70, "color": "#FFFFFF"}, "confidence": 1}]
        xml = compile_watchface_xml(scene, {})
        self.assertIn("[HOUR_0_23_Z]", xml)

    def test_render_and_compare(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            scene = basic_scene()
            reference = root / "reference.png"
            preview = root / "preview.png"
            render_scene(scene, reference, root)
            render_scene(scene, preview, root)
            report = compare_images(reference, preview)
            self.assertEqual(report["meanAbsoluteError"], 0)
            self.assertEqual(suggest_patches(scene, reference, preview), [])

    def test_analyzer_detects_large_time_band(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            reference = root / "reference.png"
            image = Image.new("RGB", (438, 438), "black")
            draw = ImageDraw.Draw(image)
            font = ImageFont.truetype("C:/Windows/Fonts/segoeui.ttf", 92)
            draw.text((219, 177), "10:08", font=font, fill="white", anchor="mm")
            image.save(reference)
            scene = analyze_image(reference, root / "output")
            self.assertTrue(any(element["type"] == "TIME" and element["dynamic"] for element in scene["elements"]))
            self.assertEqual(scene["canvas"]["shape"], "CIRCLE")
            self.assertIn("normalization", scene)

    def test_numeric_patch_is_editable(self):
        scene = basic_scene()
        patched = apply_patches(scene, [{"element": "time_primary", "property": "bbox.x", "delta": 4}])
        self.assertEqual(patched["elements"][0]["bbox"]["x"], 84)


if __name__ == "__main__":
    unittest.main()
