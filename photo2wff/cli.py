from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from .analyzer import analyze_image, analyze_product_photo
from .compare import compare_images, save_report, suggest_patches
from .compiler import compile_project
from .analog_validate import validate_dynamic_renders
from .model import apply_patches, editable_yaml, load_scene, save_scene, validate_scene
from .render import render_scene
from .runtime_validation import run_runtime_calibration, run_runtime_gate
from .wff_validate import validate_wff_xml
from .wff_render import render_wff_xml
from .human_review import generate_human_review_artifacts


def _copy_wrapper(output_project: Path) -> None:
    template = Path(__file__).resolve().parent.parent / "templates" / "gradle"
    if not template.exists():
        return
    for source in (template / "gradlew", template / "gradlew.bat"):
        if source.exists():
            destination = output_project / source.name
            shutil.copy2(source, destination)
    if (template / "wrapper").exists():
        shutil.copytree(template / "wrapper", output_project / "gradle/wrapper", dirs_exist_ok=True)


def _copy_default_font(output: Path) -> None:
    packaged = Path(__file__).resolve().parent.parent / "assets/fonts/pretendard.ttf"
    if packaged.exists():
        destination = output / "assets/fonts/pretendard.ttf"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(packaged, destination)


def quick(reference: Path, output: Path) -> None:
    if output.exists() and any(output.iterdir()):
        raise SystemExit(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    _copy_default_font(output)
    scene = analyze_image(reference, output)
    save_scene(scene, output / "scene.initial.json")
    save_scene(scene, output / "scene.json")
    (output / "editable.yaml").write_text(editable_yaml(scene), encoding="utf-8", newline="\n")
    render_scene(scene, output / "preview_initial.png", output)
    project = output / "project"
    compile_project(scene, project, output)
    _copy_wrapper(project)
    report = compare_images(output / "reference.png", output / "preview_initial.png")
    patches = suggest_patches(scene, output / "reference.png", output / "preview_initial.png")
    (output / "patches.json").write_text(json.dumps({"patches": patches}, indent=2) + "\n", encoding="utf-8", newline="\n")
    if patches:
        scene = apply_patches(scene, patches)
        save_scene(scene, output / "scene.json")
        (output / "editable.yaml").write_text(editable_yaml(scene), encoding="utf-8", newline="\n")
        compile_project(scene, project, output)
    render_scene(scene, output / "preview.png", output)
    report["corrected"] = compare_images(output / "reference.png", output / "preview.png")
    save_report(report, output / "comparison.json")


def refine(output: Path) -> None:
    scene = load_scene(output / "scene.json")
    render_scene(scene, output / "preview_before_refine.png", output)
    patches = suggest_patches(scene, output / "reference.png", output / "preview_before_refine.png")
    (output / "patches.refine.json").write_text(json.dumps({"patches": patches}, indent=2) + "\n", encoding="utf-8", newline="\n")
    if patches:
        scene = apply_patches(scene, patches)
        save_scene(scene, output / "scene.json")
        (output / "editable.yaml").write_text(editable_yaml(scene), encoding="utf-8", newline="\n")
        compile_project(scene, output / "project", output)
    render_scene(scene, output / "preview.png", output)
    save_report(compare_images(output / "reference.png", output / "preview.png"), output / "comparison.json")


def render(output: Path) -> None:
    scene = load_scene(output / "scene.json")
    render_scene(scene, output / "preview.png", output)
    save_report(compare_images(output / "reference.png", output / "preview.png"), output / "comparison.json")


def build(scene_path: Path, output: Path) -> None:
    """Build a fixed scene without invoking the Vision Analyzer."""
    if output.exists() and any(output.iterdir()):
        raise SystemExit(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    _copy_default_font(output)
    scene = load_scene(scene_path)
    save_scene(scene, output / "scene.json")
    (output / "editable.yaml").write_text(editable_yaml(scene), encoding="utf-8", newline="\n")
    render_scene(scene, output / "preview.png", output)
    compile_project(scene, output / "project", output)
    _copy_wrapper(output / "project")
    (output / "build-report.json").write_text(json.dumps({"scene": str(scene_path), "compiler": "photo2wff", "visionAnalyzerUsed": False}, indent=2) + "\n", encoding="utf-8", newline="\n")


def product_photo(reference: Path, output: Path, generative_fallback: Path | None = None, manual_glyph_dir: Path | None = None) -> None:
    """Analyze a product photograph without pretending it is a clean digital screenshot."""
    if output.exists() and any(output.iterdir()):
        raise SystemExit(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    _copy_default_font(output)
    scene = analyze_product_photo(reference, output, generative_fallback_path=generative_fallback, manual_glyph_dir=manual_glyph_dir)
    save_scene(scene, output / "scene.json")
    (output / "editable.yaml").write_text(editable_yaml(scene), encoding="utf-8", newline="\n")
    render_scene(scene, output / "preview.png", output)
    compile_project(scene, output / "project", output)
    _copy_wrapper(output / "project")
    xml_path = output / "project/watchface/src/main/res/raw/watchface.xml"
    render_metadata = render_wff_xml(xml_path, output / "wff-rendered.png", fixed_time=scene["preview"]["time"])
    render_dir = output / "renders"
    render_times = ["10:08:30", "03:15:45", "06:30:00", "09:45:15"]
    render_paths = {time: render_dir / f"{time.replace(':', '-')}.png" for time in render_times}
    for time, path in render_paths.items():
        render_wff_xml(xml_path, path, fixed_time=time)
    dynamic_validation = validate_dynamic_renders(scene, render_paths)
    (output / "dynamic-validation.json").write_text(json.dumps(dynamic_validation, indent=2) + "\n", encoding="utf-8", newline="\n")
    human_review = generate_human_review_artifacts(scene, output, xml_path)
    comparison = compare_images(output / "reference.png", output / "wff-rendered.png")
    comparison["render"] = render_metadata
    comparison["dynamicValidation"] = dynamic_validation
    comparison["humanReview"] = human_review
    save_report(comparison, output / "comparison.json")


def human_review(output: Path) -> None:
    scene = load_scene(output / "scene.json")
    xml_path = output / "project/watchface/src/main/res/raw/watchface.xml"
    manifest = generate_human_review_artifacts(scene, output, xml_path)
    comparison_path = output / "comparison.json"
    if comparison_path.exists():
        comparison = json.loads(comparison_path.read_text(encoding="utf-8"))
        comparison["humanReview"] = manifest
        save_report(comparison, comparison_path)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(prog="photo2wff")
    subparsers = parser.add_subparsers(dest="command", required=True)
    quick_parser = subparsers.add_parser("quick")
    quick_parser.add_argument("reference", type=Path)
    quick_parser.add_argument("--out", type=Path, default=Path("output"))
    refine_parser = subparsers.add_parser("refine")
    refine_parser.add_argument("output", type=Path)
    render_parser = subparsers.add_parser("render")
    render_parser.add_argument("output", type=Path)
    wff_render_parser = subparsers.add_parser("render-wff")
    wff_render_parser.add_argument("xml", type=Path)
    wff_render_parser.add_argument("--out", type=Path, default=Path("wff-rendered.png"))
    wff_render_parser.add_argument("--time", type=str)
    wff_render_parser.add_argument("--date", type=str)
    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("scene", type=Path)
    wff_parser = subparsers.add_parser("validate-wff")
    wff_parser.add_argument("xml", type=Path)
    wff_parser.add_argument("--jar", type=Path)
    build_parser = subparsers.add_parser("build")
    build_parser.add_argument("scene", type=Path)
    build_parser.add_argument("--out", type=Path, default=Path("sample-output"))
    photo_parser = subparsers.add_parser("product-photo")
    photo_parser.add_argument("reference", type=Path)
    photo_parser.add_argument("--out", type=Path, default=Path("product-photo-output"))
    photo_parser.add_argument("--generative-fallback", type=Path, help="optional 438x438 external inpainting candidate for unresolved regions")
    photo_parser.add_argument("--manual-glyph-dir", type=Path, help="optional directory containing user-supplied 0.png-9.png glyph overrides")
    review_parser = subparsers.add_parser("human-review")
    review_parser.add_argument("output", type=Path)
    runtime_parser = subparsers.add_parser("runtime-gate")
    runtime_parser.add_argument("scene", type=Path)
    runtime_parser.add_argument("xml", type=Path, help="deterministic WFF XML, normally the manual-override-off project")
    runtime_parser.add_argument("--out", type=Path, default=Path("runtime-validation"))
    runtime_parser.add_argument("--manual-xml", type=Path, help="optional manual-override-on WFF XML")
    runtime_parser.add_argument("--adb", type=Path, help="optional adb executable")
    runtime_parser.add_argument("--runtime-dir", type=Path, help="existing active-runtime screenshots root")
    runtime_parser.add_argument("--capture", action="store_true", help="clean-install, activate, set time/date, and capture the active WFF runtime")
    runtime_parser.add_argument("--apk-off", type=Path, help="manual-override-off APK for --capture")
    runtime_parser.add_argument("--apk-on", type=Path, help="manual-override-on APK for --capture")
    runtime_parser.add_argument("--serial", type=str, help="Wear OS adb serial for --capture")
    calibration_parser = subparsers.add_parser("runtime-calibration")
    calibration_parser.add_argument("scene", type=Path)
    calibration_parser.add_argument("xml", type=Path, help="deterministic WFF XML, normally the manual-override-off project")
    calibration_parser.add_argument("runtime_dir", type=Path, help="A2.5b active-runtime screenshot directory")
    calibration_parser.add_argument("--out", type=Path, default=Path("runtime-calibration"))
    calibration_parser.add_argument("--manual-xml", type=Path, help="optional manual-override-on WFF XML")
    args = parser.parse_args()
    if args.command == "quick":
        quick(args.reference, args.out)
    elif args.command == "refine":
        refine(args.output)
    elif args.command == "render":
        render(args.output)
    elif args.command == "render-wff":
        print(render_wff_xml(args.xml, args.out, fixed_time=args.time, fixed_date=args.date))
    elif args.command == "validate":
        validate_scene(json.loads(args.scene.read_text(encoding="utf-8")))
        print(f"valid: {args.scene}")
    elif args.command == "validate-wff":
        print(validate_wff_xml(args.xml, validator_jar=args.jar))
    elif args.command == "build":
        build(args.scene, args.out)
    elif args.command == "product-photo":
        product_photo(args.reference, args.out, args.generative_fallback, args.manual_glyph_dir)
    elif args.command == "human-review":
        human_review(args.output)
    elif args.command == "runtime-gate":
        print(json.dumps(run_runtime_gate(args.scene, args.xml, args.out, manual_xml=args.manual_xml, adb=args.adb, runtime_dir=args.runtime_dir, capture=args.capture, apk_off=args.apk_off, apk_on=args.apk_on, serial=args.serial), ensure_ascii=False, indent=2))
    elif args.command == "runtime-calibration":
        print(json.dumps(run_runtime_calibration(args.scene, args.xml, args.runtime_dir, args.out, manual_xml=args.manual_xml), ensure_ascii=False, indent=2))
