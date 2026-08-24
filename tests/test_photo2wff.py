from __future__ import annotations

import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from photo2wff.analyzer import analyze_image
from photo2wff.analog_validate import validate_dynamic_renders
from photo2wff.compare import compare_images, suggest_patches
from photo2wff.compiler import compile_watchface_xml
from photo2wff.date_window import extract_date_day_of_month_window
from photo2wff.display_geometry import RoundedRect, boundary_normalized_map, inverse_raster_map, map_analog_hand
from photo2wff.model import SceneValidationError, apply_patches, load_scene, validate_scene
from photo2wff.measurement_correctness import FIDUCIALS, _fit_geometry
from photo2wff.occlusion import reconstruct_occluded_dial
from photo2wff.production_port import _make_production_dial, _production_scene
from photo2wff.render import render_scene
from photo2wff.runtime_validation import _region_masks, run_runtime_gate
from photo2wff.wff_validate import WffValidationError, lint_wff_xml, validate_wff_xml
from photo2wff.wff_render import render_wff_xml


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

    def test_dynamic_slot_compiles_day_of_month(self):
        scene = basic_scene()
        scene["elements"] = [
            {
                "id": "date_day_of_month",
                "type": "DYNAMIC_SLOT",
                "slotType": "DATE_DAY_OF_MONTH",
                "dynamic": True,
                "bbox": {"x": 344, "y": 207, "width": 48, "height": 28},
                "format": "d",
                "style": {"fontFamily": "SYNC_TO_DEVICE", "fontSize": 24, "alignment": "center", "color": "#EEE3DC"},
                "confidence": 0.93,
            }
        ]
        xml = compile_watchface_xml(scene, {})
        self.assertIn("[DAY]", xml)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            xml_path = root / "watchface.xml"
            xml_path.write_text(xml, encoding="utf-8")
            first = root / "day-01.png"
            second = root / "day-31.png"
            render_wff_xml(xml_path, first, fixed_date="2024-08-01")
            render_wff_xml(xml_path, second, fixed_date="2024-08-31")
            self.assertNotEqual(first.read_bytes(), second.read_bytes())

    def test_manual_glyph_override_switches_whole_date_to_pretendard_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            drawable = root / "project" / "watchface" / "src" / "main" / "res" / "drawable"
            raw = root / "project" / "watchface" / "src" / "main" / "res" / "raw"
            drawable.mkdir(parents=True)
            raw.mkdir(parents=True)
            manual_asset = "assets/manual-glyphs/manual_glyph_8.png"
            manual = Image.new("RGBA", (13, 21), (0, 0, 0, 0))
            ImageDraw.Draw(manual).rectangle((1, 1, 11, 19), outline=(255, 40, 40, 255), width=2)
            (root / manual_asset).parent.mkdir(parents=True)
            manual.save(root / manual_asset)
            scene = basic_scene()
            scene["elements"] = [{
                "id": "date_day_of_month",
                "type": "DYNAMIC_SLOT",
                "slotType": "DATE_DAY_OF_MONTH",
                "dynamic": True,
                "bbox": {"x": 170, "y": 200, "width": 98, "height": 36},
                "format": "d",
                "style": {"fontFamily": "Pretendard", "fontSize": 24, "alignment": "center", "color": "#FFFFFF"},
                "manualGlyphs": {
                    "type": "MANUAL_GLYPH_OVERRIDE",
                    "family": "Photo2WFFManualGlyphs",
                    "fallbackFamily": "Pretendard",
                    "resources": {"8": manual_asset},
                    "metrics": {"8": {"width": 13, "height": 21}},
                },
                "confidence": 1,
            }]
            xml = compile_watchface_xml(scene, {manual_asset: "manual_glyph_8"})
            xml_path = raw / "watchface.xml"
            xml_path.write_text(xml, encoding="utf-8")
            shutil_target = drawable / "manual_glyph_8.png"
            manual.save(shutil_target)
            self.assertIn("PASSED", validate_wff_xml(xml_path))
            parsed = ET.fromstring(xml)
            self.assertIsNotNone(parsed.find("./BitmapFonts/BitmapFont/Character[@name='8']"))
            self.assertIsNone(parsed.find(".//*[@fallbackFamily]"))
            self.assertEqual(parsed.find(".//Condition/Expressions/Expression").text, "[DAY] == 8")
            self.assertIsNotNone(parsed.find(".//Condition/Compare/PartText/Text/BitmapFont"))
            self.assertIsNotNone(parsed.find(".//Condition/Default/PartText/Text/Font"))
            self.assertIn("<Template>%d<Parameter expression=\"[DAY]\"/></Template>", xml)
            provided = root / "provided.png"
            fallback = root / "fallback.png"
            render_wff_xml(xml_path, provided, fixed_date="2024-08-08")
            render_wff_xml(xml_path, fallback, fixed_date="2024-08-09")
            self.assertNotEqual(provided.read_bytes(), fallback.read_bytes())
            with Image.open(provided) as rendered:
                foreground = rendered.convert("RGB").crop((170, 200, 268, 236)).getbbox()
                self.assertIsNotNone(foreground)
                self.assertLessEqual(foreground[2] - foreground[0], 15)
                self.assertGreaterEqual(foreground[1], 5)
                self.assertLess(foreground[1], 10)
            with Image.open(shutil_target) as copied:
                self.assertEqual(copied.size, (13, 21))

    def test_compiler_maps_scene_alignment_to_official_wff_values(self):
        scene = basic_scene()
        scene["elements"][1]["style"]["alignment"] = "left"
        root = ET.fromstring(compile_watchface_xml(scene, {}))
        self.assertEqual(root.find(".//PartText/Text").attrib["align"], "START")

    def test_runtime_gate_reports_blocked_environment_without_faking_capture(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            scene = basic_scene()
            scene_path = root / "scene.json"
            scene_path.write_text(json.dumps(scene), encoding="utf-8")
            xml_path = root / "watchface.xml"
            xml_path.write_text(compile_watchface_xml(scene, {}), encoding="utf-8")
            report = run_runtime_gate(scene_path, xml_path, root / "runtime-validation", adb=root / "missing-adb.exe")
            self.assertEqual(report["status"], "blocked_by_runtime_environment")
            self.assertEqual(report["caseCount"], 20)
            self.assertTrue(all(case["status"] == "blocked_by_runtime_environment" for case in report["cases"]))
            self.assertTrue((root / "runtime-validation" / "runtime-validation-report.json").exists())

    def test_wff_structural_lint_is_not_official_validation(self):
        with tempfile.TemporaryDirectory() as temp:
            xml_path = Path(temp) / "watchface.xml"
            xml_path.write_text(compile_watchface_xml(basic_scene(), {}), encoding="utf-8")
            self.assertEqual(lint_wff_xml(xml_path), "Photo2WFF structural lint passed")

    def test_official_validation_fails_closed_without_validator(self):
        with tempfile.TemporaryDirectory() as temp:
            xml_path = Path(temp) / "watchface.xml"
            xml_path.write_text(compile_watchface_xml(basic_scene(), {}), encoding="utf-8")
            with self.assertRaises(WffValidationError):
                validate_wff_xml(xml_path, validator_jar=Path(temp) / "missing-validator.jar")

    def test_geometry_calibration_fits_fiducials_independently(self):
        with tempfile.TemporaryDirectory() as temp:
            image = Image.new("RGB", (500, 500), "black")
            draw = ImageDraw.Draw(image)
            for _, (x, y), color in FIDUCIALS:
                runtime_x = 1.04 * x - 0.01 * y + 3.5
                runtime_y = 0.01 * x + 1.03 * y + 4.25
                draw.ellipse((runtime_x - 4, runtime_y - 4, runtime_x + 4, runtime_y + 4), fill=color)
            path = Path(temp) / "fiducials.png"
            image.save(path)
            fitted = _fit_geometry(path)
            self.assertEqual(fitted["status"], "measured")
            self.assertLess(fitted["fitResidualRmsPx"], 0.5)
            self.assertAlmostEqual(fitted["matrix"]["tx"], 3.5, delta=0.5)
            self.assertAlmostEqual(fitted["matrix"]["ty"], 4.25, delta=0.5)

    def test_hand_roi_uses_time_rotated_asset_alpha(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            asset = root / "assets" / "hand.png"
            asset.parent.mkdir()
            image = Image.new("RGBA", (9, 100), (0, 0, 0, 0))
            ImageDraw.Draw(image).rectangle((3, 0, 5, 94), fill="white")
            image.save(asset)
            scene = basic_scene()
            scene["elements"] = [{
                "id": "minute",
                "type": "ANALOG_HAND",
                "role": "MINUTE",
                "dynamic": True,
                "bbox": {"x": 215, "y": 124, "width": 9, "height": 100},
                "asset": "assets/hand.png",
                "pivotX": 0.5,
                "pivotY": 0.95,
                "confidence": 1,
            }]
            north = _region_masks(scene, "00:00:00", root)["hands"]
            east = _region_masks(scene, "03:15:00", root)["hands"]
            self.assertNotEqual(north.getbbox(), east.getbbox())
            self.assertNotEqual(north.tobytes(), east.tobytes())

    def test_split_time_primitives_compile(self):
        scene = basic_scene()
        scene["elements"] = [{"id": "hour", "type": "HOUR", "dynamic": True, "bbox": {"x": 100, "y": 100, "width": 80, "height": 80}, "style": {"fontFamily": "Pretendard", "fontSize": 70, "color": "#FFFFFF"}, "confidence": 1}]
        xml = compile_watchface_xml(scene, {})
        self.assertIn("[HOUR_0_23_Z]", xml)

    def test_analog_clock_compiles_native_hands(self):
        scene = basic_scene()
        scene["clock"] = {"type": "ANALOG", "centerX": 219, "centerY": 219, "confidence": 1.0}
        scene["elements"] = [
            {"id": "dial", "type": "STATIC_IMAGE", "dynamic": False, "bbox": {"x": 0, "y": 0, "width": 438, "height": 438}, "asset": "assets/dial_clean.png", "confidence": 1},
            {"id": "hour", "type": "ANALOG_HAND", "role": "HOUR", "dynamic": True, "bbox": {"x": 207, "y": 142, "width": 24, "height": 89}, "asset": "assets/hour_hand.png", "observedAngleDeg": 306, "length": 77, "thickness": 8, "pivotX": 0.5, "pivotY": 0.865, "zIndex": 10, "confidence": 1},
            {"id": "minute", "type": "ANALOG_HAND", "role": "MINUTE", "dynamic": True, "bbox": {"x": 209, "y": 77, "width": 20, "height": 152}, "asset": "assets/minute_hand.png", "observedAngleDeg": 54, "length": 142, "thickness": 8, "pivotX": 0.5, "pivotY": 0.934, "zIndex": 20, "confidence": 1},
            {"id": "second", "type": "ANALOG_HAND", "role": "SECOND", "dynamic": True, "bbox": {"x": 215, "y": 41, "width": 8, "height": 187}, "asset": "assets/second_hand.png", "observedAngleDeg": 180, "length": 178, "thickness": 3, "pivotX": 0.5, "pivotY": 0.952, "zIndex": 30, "confidence": 1},
        ]
        resources = {"assets/dial_clean.png": "dial_clean", "assets/hour_hand.png": "hour_hand", "assets/minute_hand.png": "minute_hand", "assets/second_hand.png": "second_hand"}
        xml = compile_watchface_xml(scene, resources)
        root = ET.fromstring(xml)
        self.assertEqual(root.find(".//Metadata[@key='CLOCK_TYPE']").attrib["value"], "ANALOG")
        self.assertEqual([node.tag for node in root.findall(".//AnalogClock/*")], ["HourHand", "MinuteHand", "SecondHand"])

    def test_analog_preview_changes_with_fixed_time(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            scene = {
                "schemaVersion": "1.0",
                "canvas": {"width": 438, "height": 438, "shape": "CIRCLE", "centerX": 219, "centerY": 219},
                "normalization": {"inputType": "SCREENSHOT", "rotationDegrees": 0, "confidence": 1, "requiresPerspectiveCorrection": False},
                "background": {"type": "SOLID", "color": "#000000"},
                "preview": {"time": "10:08:30"},
                "clock": {"type": "ANALOG", "centerX": 219, "centerY": 219, "confidence": 1},
                "elements": [],
                "analysis": {"watchFaceCategory": "MINIMAL_ANALOG", "overallConfidence": 1, "requiresStaticAssetExtraction": False, "requiresHumanReview": False},
            }
            for role, asset, bbox, observed, length, thickness in [
                ("HOUR", "hour.png", {"x": 207, "y": 142, "width": 24, "height": 89}, 306, 77, 8),
                ("MINUTE", "minute.png", {"x": 209, "y": 77, "width": 20, "height": 152}, 54, 142, 8),
                ("SECOND", "second.png", {"x": 215, "y": 41, "width": 8, "height": 187}, 180, 178, 3),
            ]:
                image = Image.new("RGBA", (bbox["width"], bbox["height"]), (0, 0, 0, 0))
                ImageDraw.Draw(image).line((bbox["width"] // 2, 0, bbox["width"] // 2, bbox["height"] - 1), fill="white", width=thickness)
                image.save(root / asset)
                scene["elements"].append({"id": role.lower(), "type": "ANALOG_HAND", "role": role, "dynamic": True, "bbox": bbox, "asset": asset, "observedAngleDeg": observed, "length": length, "thickness": thickness, "pivotX": 0.5, "pivotY": 0.9, "zIndex": len(scene["elements"]) + 1, "confidence": 1})
            first = root / "first.png"
            render_scene(scene, first, root)
            scene["preview"]["time"] = "10:08:31"
            second = root / "second.png"
            render_scene(scene, second, root)
            self.assertNotEqual(first.read_bytes(), second.read_bytes())

            render_paths = {}
            for fixed_time in ("10:08:30", "03:15:45", "06:30:00"):
                scene["preview"]["time"] = fixed_time
                path = root / f"{fixed_time.replace(':', '-')}.png"
                render_scene(scene, path, root)
                render_paths[fixed_time] = path
            validation = validate_dynamic_renders(scene, render_paths)
            self.assertTrue(validation["dynamicHandROI"]["passed"])
            self.assertTrue(validation["sourceAngleGhost"]["passed"])
            self.assertTrue(validation["passed"])

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

    def test_rounded_rect_circle_is_a_special_case(self):
        circle = RoundedRect(438, 438, 219, 219, 219)
        self.assertTrue(circle.is_circle)
        for angle in range(0, 360, 30):
            import math

            direction = (math.sin(math.radians(angle)), -math.cos(math.radians(angle)))
            self.assertAlmostEqual(circle.boundary_distance(direction), 219, places=5)

    def test_boundary_normalized_mapping_preserves_radial_position(self):
        import math

        source = RoundedRect(388, 418, 44, 219, 219)
        target = RoundedRect(438, 438, 219, 219, 219)
        direction = (math.sin(math.radians(45)), -math.cos(math.radians(45)))
        source_boundary = source.boundary_distance(direction)
        target_boundary = target.boundary_distance(direction)
        point = (source.center_x + direction[0] * source_boundary * 0.62, source.center_y + direction[1] * source_boundary * 0.62)
        mapped = boundary_normalized_map(point, source, target)
        mapped_radius = math.hypot(mapped[0] - target.center_x, mapped[1] - target.center_y)
        self.assertAlmostEqual(mapped_radius / target_boundary, 0.62, places=5)

    def test_analog_hand_mapping_targets_clock_center_and_scales_length(self):
        source = RoundedRect(388, 418, 44, 219, 219)
        target = RoundedRect(438, 438, 219, 219, 219)
        mapped = map_analog_hand(
            {"id": "minute", "role": "MINUTE", "observedAngleDeg": 54, "length": 175, "thickness": 8},
            source,
            target,
            source_clock_center=(219.7, 221.5),
            target_clock_center=(219, 219),
        )
        self.assertEqual(mapped["targetPivot"], {"x": 219, "y": 219})
        self.assertGreater(mapped["targetLength"], 0)
        self.assertAlmostEqual(mapped["thicknessPreserved"], 8)

    def test_occlusion_engine_separates_observed_and_reconstructed_pixels(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            assets = root / "assets"
            assets.mkdir()
            reference = Image.new("RGB", (96, 96), "black")
            ImageDraw.Draw(reference).line((48, 48, 48, 12), fill="white", width=4)
            reference_path = root / "reference.png"
            reference.save(reference_path)
            before = reference.copy()
            ImageDraw.Draw(before).line((48, 48, 48, 12), fill="black", width=8)
            before.save(assets / "dial_clean.png")
            report = reconstruct_occluded_dial(
                reference_path,
                assets,
                root,
                (48, 48),
                {
                    "HOUR": {
                        "observedAngleDeg": 0,
                        "length": 36,
                        "thickness": 4,
                        "bbox": {"height": 42},
                    }
                },
            )
            self.assertFalse(report["generatedPixelsAreObservedTruth"])
            self.assertTrue((assets / "observed-mask.png").exists())
            self.assertTrue((assets / "hand-occlusion-mask.png").exists())
            self.assertTrue((assets / "reconstructed-mask.png").exists())
            self.assertTrue((assets / "dial-completed.png").exists())
            self.assertGreater(report["pixelCounts"]["observed"], 0)
            self.assertGreaterEqual(report["pixelCounts"]["unresolved"], 0)

    def test_date_window_removes_only_visible_glyph_and_preserves_frame(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            assets = root / "assets"
            assets.mkdir()
            reference = Image.new("RGB", (64, 64), "black")
            draw = ImageDraw.Draw(reference)
            draw.rounded_rectangle((10, 20, 53, 45), radius=5, outline=(220, 20, 40), width=3)
            draw.text((31, 32), "9", font=ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 18), fill="white", anchor="mm")
            reference_path = root / "reference.png"
            dial_path = assets / "dial_clean.png"
            reference.save(reference_path)
            reference.save(dial_path)
            metadata = extract_date_day_of_month_window(reference_path, dial_path, root)
            self.assertIsNotNone(metadata)
            self.assertEqual(metadata["semanticType"], "DATE_DAY_OF_MONTH")
            empty = Image.open(assets / "dial_empty_date.png").convert("RGB")
            self.assertEqual(empty.getpixel((10, 32)), (220, 20, 40))
            self.assertLess(max(empty.getpixel((31, 32))), 45)

    def test_inverse_raster_mapping_keeps_target_corners_outside_shape(self):
        source = RoundedRect(388, 418, 44, 219, 219)
        target = RoundedRect(438, 438, 219, 219, 219)
        image = Image.new("RGBA", (438, 438), (255, 255, 255, 255))
        mapped = inverse_raster_map(image, source, target)
        self.assertEqual(mapped.size, (438, 438))
        self.assertEqual(mapped.getpixel((0, 0))[3], 0)
        self.assertGreater(mapped.getpixel((219, 219))[3], 0)

    def test_production_port_uses_static_dial_native_hands_date_and_cap(self):
        source_root = Path(__file__).resolve().parents[1] / "hermes-a2-output"
        scene = _production_scene(load_scene(source_root / "scene.json"))
        elements = {element["id"]: element for element in scene["elements"]}
        self.assertEqual(elements["dial_complete"]["asset"], "assets/dial_complete.png")
        self.assertEqual(elements["date_day_of_month"]["slotType"], "DATE_DAY_OF_MONTH")
        self.assertEqual(elements["date_day_of_month"]["style"]["fontFamily"], "Pretendard")
        self.assertNotIn("manualGlyphs", elements["date_day_of_month"])
        self.assertEqual([elements[role]["zIndex"] for role in ("hour_hand", "minute_hand", "second_hand", "center_cap")], [10, 20, 30, 100])

        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "dial_complete.png"
            report = _make_production_dial(scene, source_root, destination)
            completed = Image.open(destination).convert("RGB")
            source = Image.open(source_root / "assets" / "dial-completed.png").convert("RGB")
            self.assertEqual(completed.getpixel((219, 43)), source.getpixel((219, 43)))
            self.assertLess(max(completed.getpixel((368, 220))), 20)
            self.assertGreater(max(completed.getpixel((224, 270))), 80)
            self.assertFalse(report["generatedPixelsAreObservedTruth"])
            self.assertTrue(report["requiresHumanReview"])


if __name__ == "__main__":
    unittest.main()
