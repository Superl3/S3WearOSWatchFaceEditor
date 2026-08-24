from __future__ import annotations

import argparse
import copy
import json
import shutil
from pathlib import Path

from .analyzer import analyze_image, analyze_product_photo
from .compare import compare_images, save_report, suggest_patches
from .compiler import compile_project
from .analog_validate import validate_dynamic_renders
from .model import apply_patches, editable_yaml, load_scene, save_scene, validate_scene
from .render import render_scene
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


def product_photo(reference: Path, output: Path, generative_fallback: Path | None = None, human_seed: Path | None = None) -> None:
    """Analyze a product photograph without pretending it is a clean digital screenshot."""
    if output.exists() and any(output.iterdir()):
        raise SystemExit(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    _copy_default_font(output)
    scene = analyze_product_photo(reference, output, generative_fallback_path=generative_fallback, human_seed_path=human_seed)
    save_scene(scene, output / "scene.json")
    (output / "editable.yaml").write_text(editable_yaml(scene), encoding="utf-8", newline="\n")
    render_scene(scene, output / "preview.png", output)
    compile_project(scene, output / "project", output)
    _copy_wrapper(output / "project")
    fallback_scene = copy.deepcopy(scene)
    for element in fallback_scene.get("elements", []):
        themed = element.get("themedGlyph")
        if themed:
            themed["enabled"] = False
    save_scene(fallback_scene, output / "scene.fallback.json")
    compile_project(fallback_scene, output / "project-fallback", output)
    _copy_wrapper(output / "project-fallback")
    xml_path = output / "project/watchface/src/main/res/raw/watchface.xml"
    fallback_xml_path = output / "project-fallback/watchface/src/main/res/raw/watchface.xml"
    glyph_report = json.loads((output / "glyph-report.json").read_text(encoding="utf-8")) if (output / "glyph-report.json").exists() else {}
    # A2b.2 review projects deliberately enable the themed BitmapFont while
    # retaining approved=false.  They are never used as the production scene.
    review_scene = copy.deepcopy(scene)
    for element in review_scene.get("elements", []):
        themed = element.get("themedGlyph")
        if themed:
            themed["enabled"] = True
            themed["approved"] = False
    review_project = output / "project-themed-review"
    compile_project(review_scene, review_project, output)
    _copy_wrapper(review_project)
    candidate_xmls: dict[str, Path] = {}
    for candidate in glyph_report.get("candidates", {}).get("3", []):
        candidate_id = str(candidate.get("candidate", "")).zfill(2)
        candidate_scene = copy.deepcopy(review_scene)
        for element in candidate_scene.get("elements", []):
            themed = element.get("themedGlyph")
            if not themed:
                continue
            themed["resources"]["3"] = str(Path("assets/glyphs/synthesized/candidates") / candidate["resource"]).replace("\\", "/")
            themed["metrics"]["3"] = candidate.get("metrics", themed["metrics"].get("3", {}))
        candidate_project = output / f"project-themed-review-candidate-{candidate_id}"
        compile_project(candidate_scene, candidate_project, output)
        _copy_wrapper(candidate_project)
        candidate_xmls[candidate_id] = candidate_project / "watchface/src/main/res/raw/watchface.xml"
    render_metadata = render_wff_xml(xml_path, output / "wff-rendered.png", fixed_time=scene["preview"]["time"])
    fallback_render_metadata = render_wff_xml(fallback_xml_path, output / "fallback-wff-rendered.png", fixed_time=scene["preview"]["time"])
    render_dir = output / "renders"
    render_times = ["10:08:30", "03:15:45", "06:30:00", "09:45:15"]
    render_paths = {time: render_dir / f"{time.replace(':', '-')}.png" for time in render_times}
    for time, path in render_paths.items():
        render_wff_xml(xml_path, path, fixed_time=time)
    dynamic_validation = validate_dynamic_renders(scene, render_paths)
    (output / "dynamic-validation.json").write_text(json.dumps(dynamic_validation, indent=2) + "\n", encoding="utf-8", newline="\n")
    review_xml_path = review_project / "watchface/src/main/res/raw/watchface.xml"
    human_review = generate_human_review_artifacts(scene, output, review_xml_path)
    comparison = compare_images(output / "reference.png", output / "wff-rendered.png")
    comparison["render"] = render_metadata
    comparison["fallbackRender"] = fallback_render_metadata
    comparison["dynamicValidation"] = dynamic_validation
    comparison["humanReview"] = human_review
    save_report(comparison, output / "comparison.json")


def human_review(output: Path) -> None:
    scene = load_scene(output / "scene.json")
    xml_path = output / "project/watchface/src/main/res/raw/watchface.xml"
    review_xml_path = output / "project-themed-review/watchface/src/main/res/raw/watchface.xml"
    if review_xml_path.exists():
        xml_path = review_xml_path
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
    photo_parser.add_argument("--human-seed", type=Path, help="optional human-provided missing-glyph seed image")
    review_parser = subparsers.add_parser("human-review")
    review_parser.add_argument("output", type=Path)
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
        product_photo(args.reference, args.out, args.generative_fallback, args.human_seed)
    elif args.command == "human-review":
        human_review(args.output)
