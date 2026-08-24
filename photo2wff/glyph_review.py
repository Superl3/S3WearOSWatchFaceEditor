from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from .wff_render import render_wff_xml


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        Path("C:/Windows/Fonts/segoeuib.ttf") if bold else Path("C:/Windows/Fonts/segoeui.ttf"),
        Path("C:/Windows/Fonts/arialbd.ttf") if bold else Path("C:/Windows/Fonts/arial.ttf"),
    )
    for candidate in candidates:
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size)
    return ImageFont.load_default()


def _fit(image: Image.Image, size: tuple[int, int], background: str = "#0C0C0C") -> Image.Image:
    panel = Image.new("RGB", size, background)
    fitted = image.convert("RGBA")
    scale = min((size[0] - 12) / max(1, fitted.width), (size[1] - 42) / max(1, fitted.height))
    fitted = fitted.resize((max(1, round(fitted.width * scale)), max(1, round(fitted.height * scale))), Image.Resampling.NEAREST)
    panel.paste(fitted, ((size[0] - fitted.width) // 2, 34), fitted)
    return panel


def _label(panel: Image.Image, text: str, color: str = "#FFFFFF") -> None:
    ImageDraw.Draw(panel).text((8, 8), text, fill=color, font=_font(14, True))


def _glyph_atlas(report: dict[str, Any], output_root: Path, path: Path, provenance: bool = False) -> None:
    tile = (130, 132)
    atlas = Image.new("RGB", (tile[0] * 5, tile[1] * 2), "#171717")
    draw = ImageDraw.Draw(atlas)
    for index, character in enumerate("0123456789"):
        glyph = report.get("glyphs", {}).get(character)
        if glyph:
            image = Image.open(output_root / glyph["resource"]).convert("RGBA")
            source = glyph.get("source", "UNKNOWN")
            color = "#69F0AE" if source == "OBSERVED" else "#FFB45C" if source == "SYNTHESIZED" else "#FF6F6F"
        else:
            image = Image.new("RGBA", (20, 24), (0, 0, 0, 0))
            source = "MISSING"
            color = "#FF6F6F"
        left = (index % 5) * tile[0]
        top = (index // 5) * tile[1]
        atlas.paste(_fit(image, tile), (left, top))
        label = f"{character}  {source}" if provenance else character
        draw.text((left + 8, top + 106), label, fill=color if provenance else "#FFFFFF", font=_font(14, True))
        if provenance and glyph:
            confidence = glyph.get("confidence", 0)
            draw.text((left + 8, top + 119), f"confidence={confidence:.2f}", fill="#BDBDBD", font=_font(10))
    title = "PROVENANCE: OBSERVED / SYNTHESIZED / MISSING" if provenance else "CANONICAL THEMED GLYPHS"
    draw.text((12, 2), title, fill="#FFFFFF", font=_font(12, True))
    path.parent.mkdir(parents=True, exist_ok=True)
    atlas.save(path)


def _candidate_sheet(report: dict[str, Any], output_root: Path, path: Path) -> None:
    candidates = [candidate for values in report.get("candidates", {}).values() for candidate in values]
    tile = (180, 155)
    columns = max(1, min(4, len(candidates)))
    rows = max(1, (len(candidates) + columns - 1) // columns)
    sheet = Image.new("RGB", (tile[0] * columns, tile[1] * rows + 44), "#151515")
    draw = ImageDraw.Draw(sheet)
    draw.text((12, 10), "MISSING GLYPH CANDIDATES — HUMAN REVIEW REQUIRED", fill="#FFFFFF", font=_font(16, True))
    for index, candidate in enumerate(candidates):
        left = (index % columns) * tile[0]
        top = 44 + (index // columns) * tile[1]
        image = Image.open(output_root / "assets/glyphs/synthesized/candidates" / candidate["resource"]).convert("RGBA")
        sheet.paste(_fit(image, tile), (left, top))
        draw.text((left + 8, top + 116), f"{candidate['character']}  #{candidate['candidate']}", fill="#FFB45C", font=_font(14, True))
        draw.text((left + 8, top + 133), f"{candidate['source']}  {candidate['confidence']:.2f}", fill="#D0D0D0", font=_font(10))
    if not candidates:
        draw.text((12, 60), "No missing glyph candidates", fill="#69F0AE", font=_font(18, True))
    path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(path)


def _reference_and_candidates(report: dict[str, Any], output_root: Path, path: Path) -> None:
    tile = (104, 108)
    sheet = Image.new("RGB", (tile[0] * 10, tile[1] * 2 + 48), "#151515")
    draw = ImageDraw.Draw(sheet)
    draw.text((10, 8), "REFERENCE GLYPHS  0 1 2 _ 4 5 6 7 8 9", fill="#FFFFFF", font=_font(15, True))
    for index, character in enumerate("0123456789"):
        glyph = report.get("glyphs", {}).get(character)
        is_observed = report.get("coverage", {}).get(character) == "OBSERVED"
        image = Image.open(output_root / glyph["resource"]).convert("RGBA") if glyph and is_observed else Image.new("RGBA", (20, 24), (0, 0, 0, 0))
        left = index * tile[0]
        panel = _fit(image, tile)
        panel_draw = ImageDraw.Draw(panel)
        panel_draw.text((tile[0] // 2 - 5, tile[1] - 20), character if is_observed else "_", fill="#69F0AE" if is_observed else "#FF6F6F", font=_font(15, True))
        sheet.paste(panel, (left, 40))
    draw.text((10, tile[1] + 53), "CANDIDATE 3", fill="#FFFFFF", font=_font(15, True))
    candidates = report.get("candidates", {}).get("3", [])
    for index, candidate in enumerate(candidates[:10]):
        left = index * tile[0]
        image = Image.open(output_root / "assets/glyphs/synthesized/candidates" / candidate["resource"]).convert("RGBA")
        panel = _fit(image, tile)
        ImageDraw.Draw(panel).text((tile[0] // 2 - 9, tile[1] - 20), f"#{candidate['candidate']}", fill="#FFB45C", font=_font(13, True))
        sheet.paste(panel, (left, 40 + tile[1]))
    path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(path)


def _candidate_context(report: dict[str, Any], output_root: Path, path: Path, center: tuple[float, float] = (219.7, 221.5), radius: float = 168.0) -> None:
    base = Image.open(output_root / "assets/dial_empty_date.png").convert("RGBA")
    candidate = report.get("candidates", {}).get("3", [])
    if candidate:
        glyph = Image.open(output_root / "assets/glyphs/synthesized/candidates" / candidate[0]["resource"]).convert("RGBA")
        angle = math.radians(90)
        x = round(center[0] + math.sin(angle) * radius - glyph.width / 2)
        y = round(center[1] - math.cos(angle) * radius - glyph.height / 2)
        overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
        overlay.alpha_composite(glyph, (x, y))
        base.alpha_composite(overlay)
    canvas = Image.new("RGB", (base.width, base.height + 56), "#111111")
    canvas.paste(base.convert("RGB"), (0, 0))
    draw = ImageDraw.Draw(canvas)
    draw.text((12, base.height + 7), "STYLE REVIEW ONLY: candidate 3 at 3 o'clock", fill="#FFB45C", font=_font(14, True))
    draw.text((12, base.height + 30), "Not added to final dial", fill="#FFFFFF", font=_font(13, True))
    path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(path)


def _date_comparison(scene: dict[str, Any], output_root: Path, themed_xml: Path, fallback_xml: Path, path: Path) -> dict[str, Any]:
    days = (1, 8, 11, 20, 31)
    render_dir = path.parent / "renders"
    render_dir.mkdir(parents=True, exist_ok=True)
    tile = (300, 300)
    atlas = Image.new("RGB", (tile[0] * 2, tile[1] * len(days)), "#151515")
    draw = ImageDraw.Draw(atlas)
    renders: dict[str, dict[str, str]] = {}
    for index, day in enumerate(days):
        themed_path = render_dir / f"themed-{day:02d}.png"
        fallback_path = render_dir / f"fallback-{day:02d}.png"
        render_wff_xml(themed_xml, themed_path, fixed_time=scene["preview"]["time"], fixed_date=f"2024-08-{day:02d}")
        render_wff_xml(fallback_xml, fallback_path, fixed_time=scene["preview"]["time"], fixed_date=f"2024-08-{day:02d}")
        for column, (label, image_path) in enumerate((("A FALLBACK", fallback_path), ("B THEMED", themed_path))):
            fitted = Image.open(image_path).convert("RGB").resize((tile[0], tile[1] - 30), Image.Resampling.LANCZOS)
            left = column * tile[0]
            top = index * tile[1]
            atlas.paste(fitted, (left, top))
            draw.text((left + 12, top + tile[1] - 24), f"DAY {day}  {label}", fill="#FFFFFF", font=_font(14, True))
        renders[str(day)] = {"fallback": str(fallback_path), "themed": str(themed_path)}
    path.parent.mkdir(parents=True, exist_ok=True)
    atlas.save(path)
    return {"days": list(days), "atlas": str(path), "renders": renders}


def _full_watch_atlas(scene: dict[str, Any], themed_xml: Path, path: Path) -> dict[str, Any]:
    days = (1, 8, 11, 20, 31)
    tile = (300, 330)
    atlas = Image.new("RGB", (tile[0] * 3, tile[1] * 2), "#151515")
    draw = ImageDraw.Draw(atlas)
    render_dir = path.parent / "full-watch-renders"
    render_dir.mkdir(parents=True, exist_ok=True)
    renders = {}
    for index, day in enumerate(days):
        render_path = render_dir / f"day-{day:02d}.png"
        render_wff_xml(themed_xml, render_path, fixed_time=scene["preview"]["time"], fixed_date=f"2024-08-{day:02d}")
        left = (index % 3) * tile[0]
        top = (index // 3) * tile[1]
        image = Image.open(render_path).convert("RGB").resize((280, 280), Image.Resampling.LANCZOS)
        atlas.paste(image, (left + 10, top + 10))
        draw.text((left + 14, top + 296), f"THEMED DAY {day}", fill="#69F0AE", font=_font(15, True))
        renders[str(day)] = str(render_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    atlas.save(path)
    return {"atlas": str(path), "renders": renders, "days": list(days)}


def _observed_fidelity_review(report: dict[str, Any], output_root: Path, path: Path) -> dict[str, Any]:
    """Show source ROI, provenance overlay, raw ROI, canonical, and reprojection."""
    tile = (190, 166)
    columns = 5
    observed = [
        (character, max(values, key=lambda item: (item.get("confidence", 0), item.get("metrics", {}).get("inkCoverage", 0))))
        for character, values in report.get("observations", {}).items()
        if report.get("coverage", {}).get(character) == "OBSERVED" and values
    ]
    sheet = Image.new("RGB", (tile[0] * columns, tile[1] * max(1, len(observed))), "#151515")
    draw = ImageDraw.Draw(sheet)
    labels = ("SOURCE ROI", "MASK / RECON", "RAW EXTRACTION", "CANONICAL", "REPROJECTED")
    dial = Image.open(output_root / "assets/dial-completed.png").convert("RGB")
    hand_mask = Image.open(output_root / "assets/hand-occlusion-mask.png").convert("L")
    reconstructed_mask = Image.open(output_root / "assets/reconstructed-mask.png").convert("L")
    for row, (character, observation) in enumerate(observed):
        roi = tuple(int(value) for value in observation["sourceROI"])
        source_roi = dial.crop(roi)
        raw = Image.open(output_root / observation["raw"]).convert("RGBA")
        canonical = Image.open(output_root / observation["canonical"]).convert("RGBA")
        overlay = source_roi.convert("RGBA")
        overlay_pixels = overlay.load()
        local_hand = hand_mask.crop(roi)
        local_reconstructed = reconstructed_mask.crop(roi)
        for y in range(overlay.height):
            for x in range(overlay.width):
                red = local_hand.getpixel((x, y)) > 20
                cyan = local_reconstructed.getpixel((x, y)) > 20
                if red or cyan:
                    base = overlay_pixels[x, y]
                    tint = (255, 64, 64, 125) if red else (64, 220, 255, 125)
                    overlay_pixels[x, y] = tuple(round(base[channel] * 0.45 + tint[channel] * 0.55) for channel in range(3)) + (255,)
        canonical_label = Image.open(output_root / observation["canonicalLabel"]).convert("RGBA")
        reprojected = canonical_label.rotate(-float(observation["localRotationDeg"]), resample=Image.Resampling.BICUBIC, expand=True)
        panels = (source_roi, overlay, raw, canonical, reprojected)
        for column, (label, image) in enumerate(zip(labels, panels)):
            left = column * tile[0]
            top = row * tile[1]
            fitted = _fit(image, tile)
            sheet.paste(fitted, (left, top))
            draw.text((left + 8, top + 6), label, fill="#FFFFFF", font=_font(11, True))
            if column == 0:
                draw.text((left + 8, top + 144), f"DIGIT {character}  {observation['id']}", fill="#69F0AE", font=_font(11, True))
            elif column == 1:
                draw.text((left + 8, top + 144), f"recon={observation.get('reconstructedMaskOverlapRatio', 0):.2f}", fill="#64DCFF", font=_font(11))
    path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(path)
    return {
        "artifact": str(path),
        "digits": [character for character, _ in observed],
        "columns": list(labels),
        "source": "assets/dial-completed.png",
        "nativeResolution": True,
    }


def generate_glyph_review_artifacts(scene: dict[str, Any], output_root: Path, themed_xml: Path, fallback_xml: Path, report: dict[str, Any]) -> dict[str, Any]:
    review_dir = output_root / "human-review" / "glyphs"
    review_dir.mkdir(parents=True, exist_ok=True)
    canonical = review_dir / "canonical-glyph-atlas.png"
    provenance = review_dir / "provenance-atlas.png"
    candidates = review_dir / "candidate-review-sheet.png"
    reference_candidates = review_dir / "reference-vs-candidates.png"
    context = review_dir / "candidate-3-dial-context.png"
    date_comparison = review_dir / "date-fallback-vs-themed-atlas.png"
    full_watch = review_dir / "full-watch-themed-atlas.png"
    observed_fidelity = review_dir / "observed-glyph-fidelity-review.png"
    _glyph_atlas(report, output_root, canonical)
    _glyph_atlas(report, output_root, provenance, provenance=True)
    _candidate_sheet(report, output_root, candidates)
    _reference_and_candidates(report, output_root, reference_candidates)
    _candidate_context(report, output_root, context)
    comparison = _date_comparison(scene, output_root, themed_xml, fallback_xml, date_comparison)
    full_watch_report = _full_watch_atlas(scene, themed_xml, full_watch)
    fidelity_report = _observed_fidelity_review(report, output_root, observed_fidelity)
    manifest = {
        "milestone": "A2b Themed Glyph Reconstruction",
        "status": report.get("status", "incomplete"),
        "coverage": report.get("coverage", {}),
        "dateStyleRelation": report.get("dateStyleRelation", {}),
        "externalModelStatus": report.get("externalModelStatus", "external synthesis unavailable"),
        "artifacts": {
            "canonicalGlyphAtlas": str(canonical),
            "provenanceAtlas": str(provenance),
            "candidateReviewSheet": str(candidates),
            "referenceVsCandidates": str(reference_candidates),
            "candidate3DialContext": str(context),
            "dateFallbackVsThemed": comparison,
            "fullWatchThemed": full_watch_report,
            "observedGlyphFidelity": fidelity_report,
        },
        "requiresHumanReview": report.get("requiresHumanReview", True),
        "deviceOrEmulatorVerification": "deferred",
    }
    manifest_path = review_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return manifest
