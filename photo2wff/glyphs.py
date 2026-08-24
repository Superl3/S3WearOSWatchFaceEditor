from __future__ import annotations

import json
import math
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont


DIGITS = tuple(str(value) for value in range(10))
THEMED_GLYPH_TYPE = "THEMED_GLYPH"
THEMED_FAMILY = "PHOTO2WFF_THEMED"
# The date opening is 47 x 29 px.  22 x 27 retains the source stroke detail
# while still allowing two proportional digits to fit without a second resize.
CELL_SIZE = (22, 27)


class GlyphSynthesizer(ABC):
    """Adapter boundary for missing themed glyph synthesis."""

    name = "abstract"

    @property
    @abstractmethod
    def available(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def synthesize(self, character: str, references: dict[str, dict[str, Any]], output_dir: Path) -> list[dict[str, Any]]:
        raise NotImplementedError

    def status(self) -> dict[str, Any]:
        return {"adapter": self.name, "available": self.available}


class ExternalModelAdapter(GlyphSynthesizer):
    name = "ExternalModelAdapter"

    def __init__(self, model_name: str | None = None) -> None:
        self.model_name = model_name

    @property
    def available(self) -> bool:
        return False

    def synthesize(self, character: str, references: dict[str, dict[str, Any]], output_dir: Path) -> list[dict[str, Any]]:
        return []

    def status(self) -> dict[str, Any]:
        return {
            "adapter": self.name,
            "available": False,
            "model": self.model_name,
            "reason": "external synthesis unavailable",
        }


class LocalModelAdapter(GlyphSynthesizer):
    name = "LocalModelAdapter"

    @property
    def available(self) -> bool:
        return False

    def synthesize(self, character: str, references: dict[str, dict[str, Any]], output_dir: Path) -> list[dict[str, Any]]:
        return []

    def status(self) -> dict[str, Any]:
        return {
            "adapter": self.name,
            "available": False,
            "reason": "no local glyph-generation model is installed",
        }


class ManualAssetAdapter(GlyphSynthesizer):
    name = "ManualAssetAdapter"

    def __init__(self, assets: dict[str, Path] | None = None) -> None:
        self.assets = assets or {}

    @property
    def available(self) -> bool:
        return bool(self.assets)

    def synthesize(self, character: str, references: dict[str, dict[str, Any]], output_dir: Path) -> list[dict[str, Any]]:
        source = self.assets.get(character)
        if source is None or not source.exists():
            return []
        destination = output_dir / f"candidate_{character}_manual.png"
        destination.parent.mkdir(parents=True, exist_ok=True)
        Image.open(source).convert("RGBA").save(destination)
        return [
            {
                "character": character,
                "candidate": "manual",
                "resource": destination.name,
                "path": str(destination),
                "source": "MANUAL_ASSET",
                "synthetic": True,
                "confidence": 0.5,
                "requiresHumanReview": True,
            }
        ]


class DeterministicFallbackAdapter(GlyphSynthesizer):
    """Produces explicit, review-only candidates without pretending to be a model."""

    name = "DeterministicFallbackAdapter"

    def __init__(self, font_path: Path | None = None) -> None:
        self.font_path = font_path

    @property
    def available(self) -> bool:
        candidates = (
            self.font_path,
            Path("C:/Windows/Fonts/arial.ttf"),
            Path("C:/Windows/Fonts/segoeui.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        )
        return any(path is not None and path.exists() for path in candidates)

    def _font(self, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        candidates = (
            self.font_path,
            Path("C:/Windows/Fonts/arial.ttf"),
            Path("C:/Windows/Fonts/segoeui.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        )
        for path in candidates:
            if path is not None and path.exists():
                return ImageFont.truetype(str(path), size)
        return ImageFont.load_default()

    @staticmethod
    def _outline_glyph(character: str, font: ImageFont.ImageFont, canvas_size: tuple[int, int], inset: int) -> Image.Image:
        mask = Image.new("L", canvas_size, 0)
        draw = ImageDraw.Draw(mask)
        draw.text((canvas_size[0] / 2, canvas_size[1] / 2), character, font=font, fill=255, anchor="mm")
        eroded = mask.filter(ImageFilter.MinFilter(3 if inset <= 1 else 5))
        outline = ImageChops.subtract(mask, eroded)
        result = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
        result.paste((235, 229, 224, 255), mask=outline)
        return result

    def synthesize(self, character: str, references: dict[str, dict[str, Any]], output_dir: Path) -> list[dict[str, Any]]:
        if not self.available:
            return []
        output_dir.mkdir(parents=True, exist_ok=True)
        candidates: list[dict[str, Any]] = []
        for index, (size, inset, confidence) in enumerate(((28, 1, 0.36), (30, 1, 0.31), (27, 2, 0.27)), start=1):
            image = self._outline_glyph(character, self._font(size), (32, 36), inset)
            name = f"candidate_{character}_{index:02d}.png"
            path = output_dir / name
            image.save(path)
            candidates.append(
                {
                    "character": character,
                    "candidate": f"{index:02d}",
                    "resource": name,
                    "path": str(path),
                    "source": "DETERMINISTIC_FALLBACK",
                    "synthetic": True,
                    "confidence": confidence,
                    "requiresHumanReview": True,
                    "references": sorted(references),
                }
            )
        return candidates


def _dial_orientation(degrees: float) -> float:
    """Undo the full tangential rotation of a dial numeral.

    Decimal digits are not 180-degree symmetric.  Reducing this value modulo
    180 made 5/6/7 (and the second digit in 10/11) become different shapes.
    Pillow's positive rotation is counter-clockwise, which is the inverse of
    the clockwise rotation used by this reference dial.
    """
    angle = degrees % 360.0
    # This face uses an intentional half-turn readability flip through the
    # lower arc: 5, 6, and 7 keep their familiar reading direction instead of
    # rotating fully with the radial marker.  Preserve that observed layout
    # convention without applying the former, incorrect global modulo fold.
    if 150.0 <= angle <= 210.0:
        return angle - 180.0
    return angle


def _bbox_from_alpha(alpha: Image.Image) -> tuple[int, int, int, int] | None:
    return alpha.getbbox()


def _ink_mask(image: Image.Image) -> Image.Image:
    rgb = image.convert("RGB")
    mask = Image.new("L", rgb.size, 0)
    source = rgb.load()
    target = mask.load()
    for y in range(rgb.height):
        for x in range(rgb.width):
            red, green, blue = source[x, y]
            high = max(red, green, blue)
            low = min(red, green, blue)
            # The seconds hand has antialiased dark-red edge pixels.  Treat
            # them as hand-only even when the red channel falls below the old
            # bright-red cutoff; warm cream numeral strokes remain balanced
            # across RGB and therefore survive.
            is_red = red >= 50 and red > green * 1.15 and red > blue * 1.05
            if high >= 105 and (high - low) <= 115 and not is_red:
                target[x, y] = min(255, max(0, high - 35) * 2)
    return mask


def _apply_exclusion(mask: Image.Image, exclusion: Image.Image | None, date_frame: dict[str, int] | None) -> Image.Image:
    result = mask.copy()
    if exclusion is not None:
        result = ImageChops.subtract(result, exclusion.convert("L"))
    if date_frame:
        draw = ImageDraw.Draw(result)
        draw.rectangle(
            (
                date_frame["x"],
                date_frame["y"],
                date_frame["x"] + date_frame["width"] - 1,
                date_frame["y"] + date_frame["height"] - 1,
            ),
            fill=0,
        )
    return result


def _crop_with_alpha(image: Image.Image, mask: Image.Image, box: tuple[int, int, int, int]) -> Image.Image:
    source = image.convert("RGBA").crop(box)
    alpha = mask.crop(box)
    source.putalpha(alpha)
    return source


def _tight(image: Image.Image, padding: int = 2) -> Image.Image:
    alpha = image.getchannel("A")
    box = alpha.getbbox()
    if box is None:
        return Image.new("RGBA", (1, 1), (0, 0, 0, 0))
    left = max(0, box[0] - padding)
    top = max(0, box[1] - padding)
    right = min(image.width, box[2] + padding)
    bottom = min(image.height, box[3] + padding)
    return image.crop((left, top, right, bottom))


def _drop_isolated_ink(image: Image.Image, minimum_area: int = 9) -> Image.Image:
    """Remove detached capture specks without altering a glyph's stroke pixels."""
    alpha = image.getchannel("A")
    pixels = alpha.load()
    visited: set[tuple[int, int]] = set()
    components: list[list[tuple[int, int]]] = []
    for y in range(alpha.height):
        for x in range(alpha.width):
            if (x, y) in visited or pixels[x, y] <= 20:
                continue
            stack = [(x, y)]
            visited.add((x, y))
            component: list[tuple[int, int]] = []
            while stack:
                current_x, current_y = stack.pop()
                component.append((current_x, current_y))
                for next_y in range(max(0, current_y - 1), min(alpha.height, current_y + 2)):
                    for next_x in range(max(0, current_x - 1), min(alpha.width, current_x + 2)):
                        if (next_x, next_y) not in visited and pixels[next_x, next_y] > 20:
                            visited.add((next_x, next_y))
                            stack.append((next_x, next_y))
            components.append(component)
    if not components:
        return image
    largest = max(len(component) for component in components)
    keep_threshold = max(minimum_area, round(largest * 0.08))
    cleaned = alpha.copy()
    cleaned_pixels = cleaned.load()
    for component in components:
        if len(component) >= keep_threshold:
            continue
        for x, y in component:
            cleaned_pixels[x, y] = 0
    result = image.copy()
    result.putalpha(cleaned)
    return result


def _normalize_cell(image: Image.Image, cell_size: tuple[int, int] = CELL_SIZE) -> Image.Image:
    image = _tight(image, padding=0)
    if image.width <= 0 or image.height <= 0:
        return Image.new("RGBA", cell_size, (0, 0, 0, 0))
    scale = min((cell_size[1] - 2) / image.height, (cell_size[0] - 1) / image.width)
    width = max(1, round(image.width * scale))
    height = max(1, round(image.height * scale))
    resized = image.resize((width, height), Image.Resampling.LANCZOS)
    cell = Image.new("RGBA", cell_size, (0, 0, 0, 0))
    cell.alpha_composite(resized, ((cell_size[0] - width) // 2, cell_size[1] - height - 1))
    return cell


def _split_columns(image: Image.Image, count: int) -> list[Image.Image]:
    alpha = image.getchannel("A")
    box = alpha.getbbox()
    if box is None or count <= 1:
        return [_tight(image, padding=1)]
    content = image.crop(box)
    content_alpha = content.getchannel("A")
    projection = [sum(1 for y in range(content.height) if content_alpha.getpixel((x, y)) > 20) for x in range(content.width)]
    boundaries = [0]
    for index in range(1, count):
        target = round(content.width * index / count)
        radius = max(2, round(content.width * 0.22))
        left = max(boundaries[-1] + 1, target - radius)
        right = min(content.width - (count - index), target + radius)
        split = min(range(left, right + 1), key=lambda column: (projection[column - 1] + projection[column], abs(column - target)))
        boundaries.append(split)
    boundaries.append(content.width)
    result = []
    for left, right in zip(boundaries, boundaries[1:]):
        result.append(_tight(content.crop((left, 0, right, content.height)), padding=1))
    return result


def _metrics(image: Image.Image) -> dict[str, float]:
    alpha = image.getchannel("A")
    box = alpha.getbbox() or (0, 0, 0, 0)
    ink = sum(1 for value in alpha.getdata() if value > 20)
    coverage = ink / max(1, image.width * image.height)
    active_rows = [sum(1 for x in range(image.width) if alpha.getpixel((x, y)) > 20) for y in range(image.height)]
    active_columns = [sum(1 for y in range(image.height) if alpha.getpixel((x, y)) > 20) for x in range(image.width)]
    stroke_samples = [value for value in active_rows + active_columns if value > 0]
    stroke = sum(stroke_samples) / max(1, len(stroke_samples)) / 2.0
    return {
        "width": image.width,
        "height": image.height,
        "inkCoverage": round(coverage, 5),
        "strokeWidthEstimate": round(stroke, 3),
        "baselineOffset": round(image.height - box[3], 3),
        "sideBearingLeft": box[0],
        "sideBearingRight": image.width - box[2],
        "inkBbox": {"x": box[0], "y": box[1], "width": max(0, box[2] - box[0]), "height": max(0, box[3] - box[1])},
    }


def _asset_from_path(path: Path) -> Image.Image:
    return Image.open(path).convert("RGBA")


def _date_style_relation(date_glyph: Image.Image | None, reference_glyph: Image.Image | None) -> dict[str, Any]:
    if date_glyph is None or reference_glyph is None or date_glyph.getchannel("A").getbbox() is None or reference_glyph.getchannel("A").getbbox() is None:
        return {"classification": "UNKNOWN", "confidence": 0.0, "similarity": 0.0}
    date = _normalize_cell(date_glyph, (32, 36)).getchannel("A").resize((32, 36), Image.Resampling.LANCZOS)
    reference = _normalize_cell(reference_glyph, (32, 36)).getchannel("A").resize((32, 36), Image.Resampling.LANCZOS)
    difference = ImageChops.difference(date, reference)
    similarity = max(0.0, 1.0 - sum(difference.getdata()) / (255.0 * difference.width * difference.height))
    date_box = date.getbbox() or (0, 0, 0, 0)
    ref_box = reference.getbbox() or (0, 0, 0, 0)
    aspect_date = (date_box[2] - date_box[0]) / max(1, date_box[3] - date_box[1])
    aspect_ref = (ref_box[2] - ref_box[0]) / max(1, ref_box[3] - ref_box[1])
    aspect_similarity = max(0.0, 1.0 - abs(aspect_date - aspect_ref))
    score = round(0.78 * similarity + 0.22 * aspect_similarity, 4)
    if score >= 0.72:
        classification = "SAME_STYLE_SYSTEM"
    elif score >= 0.5:
        classification = "RELATED_BUT_OPTICALLY_ADJUSTED"
    elif score >= 0.28:
        classification = "DIFFERENT_STYLE_SYSTEM"
    else:
        classification = "UNKNOWN"
    return {"classification": classification, "confidence": score, "similarity": round(similarity, 4)}


def _sector_crop(image: Image.Image, mask: Image.Image, center: tuple[float, float], angle: float) -> Image.Image:
    """Collect one numeral using an oriented local dial window.

    A wide angular wedge preserves terminals but also captures the neighboring
    hour positions (and turns the deliberately empty 3 o'clock slot into a
    false glyph).  A radial/tangential window remains geometry-driven while
    keeping each marker isolated.
    """
    result = Image.new("L", image.size, 0)
    source = mask.load()
    target = result.load()
    radians = math.radians(angle)
    radial_x, radial_y = math.sin(radians), -math.cos(radians)
    tangent_x, tangent_y = math.cos(radians), math.sin(radians)
    for y in range(image.height):
        for x in range(image.width):
            if source[x, y] <= 20:
                continue
            dx = x - center[0]
            dy = y - center[1]
            radial = dx * radial_x + dy * radial_y
            tangent = dx * tangent_x + dy * tangent_y
            if radial < 135 or radial > 218 or abs(tangent) > 42:
                continue
            target[x, y] = source[x, y]
    box = result.getbbox()
    if box is None:
        return Image.new("RGBA", (1, 1), (0, 0, 0, 0))
    return _crop_with_alpha(image, result, box)


def extract_themed_glyph_set(
    reference_path: Path,
    hand_occlusion_mask_path: Path,
    output_root: Path,
    clock_center: tuple[float, float],
    date_window_metadata_path: Path | None = None,
    synthesizer: GlyphSynthesizer | None = None,
) -> dict[str, Any]:
    """Extract, canonicalize, classify, and materialize a reusable themed digit set."""
    reference = Image.open(reference_path).convert("RGB")
    exclusion = Image.open(hand_occlusion_mask_path).convert("L") if hand_occlusion_mask_path.exists() else Image.new("L", reference.size, 0)
    date_metadata = json.loads(date_window_metadata_path.read_text(encoding="utf-8")) if date_window_metadata_path and date_window_metadata_path.exists() else {}
    frame = date_metadata.get("frameBbox")
    # The source hands are evidence of the photographed time, not part of a
    # numeral.  However, subtracting their broad A1 mask removes legitimate
    # dial strokes at 2/6/7/10.  Keep the original alpha here and record the
    # overlap as confidence/review metadata instead of destructively cutting a
    # glyph.  The date frame remains excluded because it replaces hour 3.
    mask = _apply_exclusion(_ink_mask(reference), None, frame)
    raw_dir = output_root / "assets/glyphs/observed/raw"
    canonical_dir = output_root / "assets/glyphs/observed/canonical"
    candidate_dir = output_root / "assets/glyphs/synthesized/candidates"
    themed_dir = output_root / "assets/glyphs/themed"
    for directory in (raw_dir, canonical_dir, candidate_dir, themed_dir):
        directory.mkdir(parents=True, exist_ok=True)

    observations: dict[str, list[dict[str, Any]]] = {digit: [] for digit in DIGITS}
    slots: list[dict[str, Any]] = []
    for hour in range(1, 13):
        label = str(hour) if hour != 12 else "12"
        angle = (hour % 12) * 30.0
        raw = _sector_crop(reference, mask, clock_center, angle)
        raw_box = raw.getchannel("A").getbbox()
        if raw_box:
            box = (raw_box[0], raw_box[1], raw_box[2], raw_box[3])
        else:
            box = (0, 0, raw.width, raw.height)
        ink_box = raw.getchannel("A").getbbox()
        quality = 0.0 if ink_box is None else min(0.99, 0.62 + sum(1 for value in raw.getchannel("A").getdata() if value > 20) / 2800.0)
        if ink_box is None or quality < 0.63:
            slots.append({"hour": hour, "label": label, "status": "MISSING", "angleDeg": angle, "bbox": {"x": box[0], "y": box[1], "width": box[2] - box[0], "height": box[3] - box[1]}, "confidence": 0.0})
            continue
        raw = _tight(raw, padding=2)
        raw_name = f"hour_{hour:02d}_label.png"
        raw_path = raw_dir / raw_name
        raw.save(raw_path)
        local_rotation = _dial_orientation(angle)
        canonical_label = raw.rotate(local_rotation, resample=Image.Resampling.BICUBIC, expand=True)
        canonical_label = _drop_isolated_ink(canonical_label)
        canonical_label = _tight(canonical_label, padding=1)
        canonical_label_path = canonical_dir / raw_name
        canonical_label.save(canonical_label_path)
        # Segment multi-digit labels only after their baseline is horizontal.
        # Splitting a tilted "10" by source X columns was the cause of the
        # corrupted 0 and several partial glyph observations.
        parts = _split_columns(canonical_label, len(label))
        if len(parts) != len(label):
            parts = [canonical_label]
        slot_observations = []
        for index, (character, part) in enumerate(zip(label, parts), start=1):
            glyph_rotation = local_rotation
            part = _tight(part, padding=1)
            observation_id = f"hour_{hour}" if len(label) == 1 else f"hour_{hour}_{'first' if index == 1 else 'second'}"
            canonical_name = f"{observation_id}_{character}.png"
            canonical_path = canonical_dir / canonical_name
            part.save(canonical_path)
            raw_observation = raw_dir / f"{observation_id}_{character}.png"
            # The raw label is retained as the lossless source observation.
            # Per-character raw crops are deliberately avoided for oblique
            # multi-digit labels because their source-space X split is invalid.
            raw.save(raw_observation)
            metric = _metrics(_normalize_cell(part))
            observation = {
                "id": observation_id,
                "character": character,
                "hour": hour,
                "angleDeg": angle,
                "localRotationDeg": round(glyph_rotation, 2),
                "raw": str(raw_observation.relative_to(output_root)).replace("\\", "/"),
                "canonical": str(canonical_path.relative_to(output_root)).replace("\\", "/"),
                "confidence": round(quality, 4),
                "metrics": metric,
            }
            observations[character].append(observation)
            slot_observations.append(observation)
        slots.append({"hour": hour, "label": label, "status": "OBSERVED", "angleDeg": angle, "localRotationDeg": local_rotation, "handOverlapMaskPresent": bool(exclusion.getbbox()), "observations": slot_observations, "confidence": round(quality, 4)})

    references: dict[str, dict[str, Any]] = {}
    coverage: dict[str, str] = {}
    themed_assets: dict[str, dict[str, Any]] = {}
    for character in DIGITS:
        if observations[character]:
            def preference(item: dict[str, Any]) -> tuple[int, float, float]:
                observation_id = item["id"]
                preferred = 1 if observation_id == f"hour_{character}" else 0
                if character == "1":
                    preferred = {"hour_12_first": 3, "hour_1": 2, "hour_10_first": 1}.get(observation_id, 0)
                elif character == "2":
                    # The minute hand points at the source's 2 marker.  The
                    # second character of the clean 12 marker is the reliable
                    # source observation for date rendering.
                    preferred = {"hour_12_second": 3, "hour_2": 1}.get(observation_id, 0)
                return preferred, item["confidence"], item["metrics"]["inkCoverage"]

            chosen = max(observations[character], key=preference)
            source_path = output_root / chosen["canonical"]
            themed_path = themed_dir / f"glyph_{character}.png"
            normalized = _normalize_cell(_asset_from_path(source_path))
            normalized.save(themed_path)
            references[character] = chosen
            coverage[character] = "OBSERVED"
            themed_assets[character] = {
                "character": character,
                "type": THEMED_GLYPH_TYPE,
                "source": "OBSERVED",
                "synthetic": False,
                "confidence": chosen["confidence"],
                "resource": str(themed_path.relative_to(output_root)).replace("\\", "/"),
                "observations": [item["id"] for item in observations[character]],
                "metrics": _metrics(normalized),
            }
        else:
            coverage[character] = "MISSING"

    synthesizer = synthesizer or DeterministicFallbackAdapter(output_root / "assets/fonts/pretendard.ttf")
    adapter_status = [ExternalModelAdapter().status(), LocalModelAdapter().status(), synthesizer.status()]
    candidates: dict[str, list[dict[str, Any]]] = {}
    for character in DIGITS:
        if coverage[character] != "MISSING":
            continue
        generated = synthesizer.synthesize(character, references, candidate_dir)
        candidates[character] = generated
        if generated:
            best = max(generated, key=lambda item: item["confidence"])
            source_path = Path(best["path"])
            themed_path = themed_dir / f"glyph_{character}.png"
            normalized = _normalize_cell(_asset_from_path(source_path))
            normalized.save(themed_path)
            coverage[character] = "SYNTHESIZED"
            themed_assets[character] = {
                "character": character,
                "type": THEMED_GLYPH_TYPE,
                "source": "SYNTHESIZED",
                "synthetic": True,
                "confidence": best["confidence"],
                "resource": str(themed_path.relative_to(output_root)).replace("\\", "/"),
                "candidate": best["candidate"],
                "requiresHumanReview": True,
                "metrics": _metrics(normalized),
            }

    date_glyph = None
    if date_metadata.get("innerBbox"):
        inner = date_metadata["innerBbox"]
        date_glyph = _crop_with_alpha(reference, _ink_mask(reference), (inner["x"], inner["y"], inner["x"] + inner["width"], inner["y"] + inner["height"]))
        date_glyph = _tight(date_glyph, padding=1)
        date_path = raw_dir / "date_window_9.png"
        date_glyph.save(date_path)
    relation = _date_style_relation(date_glyph, _asset_from_path(output_root / themed_assets["9"]["resource"]) if "9" in themed_assets else None)
    observed_count = sum(1 for value in coverage.values() if value == "OBSERVED")
    synthesized_count = sum(1 for value in coverage.values() if value == "SYNTHESIZED")
    report = {
        "status": "completed_with_review" if synthesized_count or relation["classification"] in {"UNKNOWN", "DIFFERENT_STYLE_SYSTEM"} else "completed",
        "type": THEMED_GLYPH_TYPE,
        "family": THEMED_FAMILY,
        "coverage": coverage,
        "coverageCounts": {"observed": observed_count, "synthesized": synthesized_count, "missing": sum(1 for value in coverage.values() if value == "MISSING")},
        "slots": slots,
        "observations": observations,
        "glyphs": themed_assets,
        "candidates": candidates,
        "dateStyleRelation": relation,
        "adapters": adapter_status,
        "externalModelStatus": "external synthesis unavailable",
        "canonicalization": {
            "orientation": "inverse full local dial-angle rotation; no 180-degree folding for decimal glyphs",
            "cellSize": {"width": CELL_SIZE[0], "height": CELL_SIZE[1]},
            "sourceFidelity": "observed glyph pixels preserved before normalization",
        },
        "requiresHumanReview": synthesized_count > 0 or relation["classification"] not in {"SAME_STYLE_SYSTEM", "RELATED_BUT_OPTICALLY_ADJUSTED"},
        "actualDialThreeOClockNumeral": "intentionally_absent_not_reconstructed",
    }
    report_path = output_root / "glyph-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return report
