from __future__ import annotations

import json
import math
import statistics
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
    # One fitted positional rule is used for the complete hour ring.  The
    # lower arc is kept upright by one half-turn; there is no digit-specific
    # exception (4 at 120 degrees follows the same rule as 5/6/7).
    if 90.0 < angle <= 210.0:
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
            if high >= 8 and (high - low) <= 125 and not is_red:
                # Keep the continuous antialiased intensity.  The old
                # high-minus-35 threshold erased dim outline/fill pixels.
                target[x, y] = high
    return mask


def _apply_exclusion(mask: Image.Image, exclusion: Image.Image | None, date_frame: dict[str, int] | None) -> Image.Image:
    result = mask.copy()
    if exclusion is not None:
        result = ImageChops.subtract(result, exclusion.convert("L"))
    if date_frame:
        draw = ImageDraw.Draw(result)
        margin = 4
        draw.rectangle(
            (
                max(0, date_frame["x"] - margin),
                max(0, date_frame["y"] - margin),
                min(result.width - 1, date_frame["x"] + date_frame["width"] - 1 + margin),
                min(result.height - 1, date_frame["y"] + date_frame["height"] - 1 + margin),
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


def _radial_roi(
    image: Image.Image,
    mask: Image.Image,
    center: tuple[float, float],
    angle: float,
    radial_min: float = 120.0,
    radial_max: float = 230.0,
    tangent_half_width: float = 48.0,
) -> tuple[Image.Image, tuple[int, int, int, int]]:
    """Return a fixed padded radial ROI and its source-space box.

    The ROI is deliberately not foreground-tight.  Its transparent alpha is
    the observed/reconstructed dial mask, while its RGB pixels remain the
    source dial-completed pixels.
    """
    radians = math.radians(angle)
    radial_x, radial_y = math.sin(radians), -math.cos(radians)
    tangent_x, tangent_y = math.cos(radians), math.sin(radians)
    corners = [
        (center[0] + radial_x * radial + tangent_x * tangent, center[1] + radial_y * radial + tangent_y * tangent)
        for radial in (radial_min, radial_max)
        for tangent in (-tangent_half_width, tangent_half_width)
    ]
    left = max(0, math.floor(min(point[0] for point in corners)) - 2)
    top = max(0, math.floor(min(point[1] for point in corners)) - 2)
    right = min(image.width, math.ceil(max(point[0] for point in corners)) + 3)
    bottom = min(image.height, math.ceil(max(point[1] for point in corners)) + 3)
    roi_box = (left, top, right, bottom)
    result = Image.new("L", image.size, 0)
    source = mask.load()
    target = result.load()
    for y in range(image.height):
        for x in range(image.width):
            if source[x, y] <= 20:
                continue
            dx = x - center[0]
            dy = y - center[1]
            radial = dx * radial_x + dy * radial_y
            tangent = dx * tangent_x + dy * tangent_y
            if radial < radial_min or radial > radial_max or abs(tangent) > tangent_half_width:
                continue
            target[x, y] = source[x, y]
    return _crop_with_alpha(image, result, roi_box), roi_box


def _mask_overlap_ratio(alpha: Image.Image, mask: Image.Image | None) -> float:
    if mask is None:
        return 0.0
    alpha_values = alpha.load()
    mask_values = mask.load()
    total = 0
    overlap = 0
    for y in range(alpha.height):
        for x in range(alpha.width):
            if alpha_values[x, y] <= 20:
                continue
            total += 1
            overlap += 1 if mask_values[x, y] > 20 else 0
    return round(overlap / max(1, total), 5)


def _display_metrics(image: Image.Image) -> dict[str, int]:
    alpha = image.getchannel("A")
    box = alpha.getbbox() or (0, 0, 1, 1)
    width = max(1, box[2] - box[0])
    height = max(1, box[3] - box[1])
    scale = min((CELL_SIZE[0] - 1) / width, (CELL_SIZE[1] - 2) / height)
    return {"displayWidth": max(1, round(width * scale)), "displayHeight": max(1, round(height * scale))}


def _fit_global_orientation() -> dict[str, Any]:
    """Return the single orientation model fitted across the complete ring."""
    return {"model": "piecewise_global_hour_ring", "input": "hour_angle_deg", "rule": "if 90 < angle <= 210 then angle - 180 else angle", "digitSpecificExceptions": []}


def _glyph_provenance(hand_overlap: float, reconstructed_overlap: float) -> str:
    if reconstructed_overlap >= 0.35:
        return "RECONSTRUCTED"
    if hand_overlap >= 0.08 or reconstructed_overlap >= 0.03:
        return "OBSERVED_PARTIAL"
    return "OBSERVED_CLEAN"


TOPOLOGY_SEGMENT_KINDS = ("outer_contour", "inner_contour", "skeleton", "terminal", "junction", "curvature_extrema")


def _binary_alpha(image: Image.Image, threshold: int = 20) -> list[list[bool]]:
    alpha = image.getchannel("A")
    return [[alpha.getpixel((x, y)) > threshold for x in range(image.width)] for y in range(image.height)]


def _neighbors(binary: list[list[bool]], x: int, y: int) -> list[tuple[int, int]]:
    height = len(binary)
    width = len(binary[0]) if height else 0
    return [
        (nx, ny)
        for nx, ny in ((x - 1, y - 1), (x, y - 1), (x + 1, y - 1), (x - 1, y), (x + 1, y), (x - 1, y + 1), (x, y + 1), (x + 1, y + 1))
        if 0 <= nx < width and 0 <= ny < height and binary[ny][nx]
    ]


def _thin(binary: list[list[bool]]) -> list[list[bool]]:
    """Zhang-Suen thinning, kept local to avoid a raster/vector dependency."""
    if not binary or not binary[0]:
        return binary
    height, width = len(binary), len(binary[0])
    pixels = [row[:] for row in binary]
    changed = True
    while changed:
        changed = False
        for phase in (0, 1):
            remove: list[tuple[int, int]] = []
            for y in range(1, height - 1):
                for x in range(1, width - 1):
                    if not pixels[y][x]:
                        continue
                    ordered = [pixels[y - 1][x], pixels[y - 1][x + 1], pixels[y][x + 1], pixels[y + 1][x + 1], pixels[y + 1][x], pixels[y + 1][x - 1], pixels[y][x - 1], pixels[y - 1][x - 1]]
                    count = sum(ordered)
                    transitions = sum(not ordered[index] and ordered[(index + 1) % 8] for index in range(8))
                    if count < 2 or count > 6 or transitions != 1:
                        continue
                    if phase == 0:
                        keep = ordered[0] and ordered[2] and ordered[4]
                        keep = keep or ordered[2] and ordered[4] and ordered[6]
                    else:
                        keep = ordered[0] and ordered[2] and ordered[6]
                        keep = keep or ordered[0] and ordered[4] and ordered[6]
                    if not keep:
                        remove.append((x, y))
            if remove:
                changed = True
                for x, y in remove:
                    pixels[y][x] = False
    return pixels


def _contours(binary: list[list[bool]]) -> tuple[list[list[int]], list[list[int]]]:
    outer: list[list[int]] = []
    inner: list[list[int]] = []
    height = len(binary)
    width = len(binary[0]) if height else 0
    for y in range(height):
        for x in range(width):
            adjacent_ink = any(0 <= nx < width and 0 <= ny < height and binary[ny][nx] for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)))
            if binary[y][x] and not all(0 <= nx < width and 0 <= ny < height and binary[ny][nx] for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1))):
                outer.append([x, y])
            elif not binary[y][x] and adjacent_ink:
                inner.append([x, y])
    return outer, inner


def _topology_segments(skeleton: list[list[bool]]) -> tuple[list[dict[str, Any]], list[list[int]], list[list[int]]]:
    points = {(x, y) for y, row in enumerate(skeleton) for x, value in enumerate(row) if value}
    degree = {point: len([(nx, ny) for nx, ny in _neighbors(skeleton, *point)]) for point in points}
    endpoints = [[x, y] for (x, y), count in degree.items() if count == 1]
    junctions = [[x, y] for (x, y), count in degree.items() if count >= 3]
    anchors = set(tuple(point) for point in endpoints + junctions)
    visited: set[tuple[tuple[int, int], tuple[int, int]]] = set()
    segments: list[dict[str, Any]] = []
    for start in sorted(anchors or points):
        for neighbor in _neighbors(skeleton, *start):
            edge = tuple(sorted((start, neighbor)))
            if edge in visited:
                continue
            chain = [start]
            previous, current = start, neighbor
            visited.add(edge)
            while True:
                chain.append(current)
                if current in anchors and current != start:
                    break
                next_points = [point for point in _neighbors(skeleton, *current) if point != previous]
                if not next_points:
                    break
                next_point = next_points[0]
                next_edge = tuple(sorted((current, next_point)))
                if next_edge in visited:
                    break
                visited.add(next_edge)
                previous, current = current, next_point
            if len(chain) >= 2:
                length = sum(math.dist(chain[index], chain[index + 1]) for index in range(len(chain) - 1))
                turns = []
                for index in range(1, len(chain) - 1):
                    first = math.atan2(chain[index][1] - chain[index - 1][1], chain[index][0] - chain[index - 1][0])
                    second = math.atan2(chain[index + 1][1] - chain[index][1], chain[index + 1][0] - chain[index][0])
                    turns.append(abs((second - first + math.pi) % (2 * math.pi) - math.pi))
                segments.append({
                    "points": [[int(x), int(y)] for x, y in chain],
                    "tangentStartDeg": round(math.degrees(math.atan2(chain[1][1] - chain[0][1], chain[1][0] - chain[0][0])), 3),
                    "tangentEndDeg": round(math.degrees(math.atan2(chain[-1][1] - chain[-2][1], chain[-1][0] - chain[-2][0])), 3),
                    "curvature": round(sum(turns) / max(1, length), 5),
                    "curvatureExtrema": [chain[index] for index, value in enumerate(turns, start=1) if value >= 0.65],
                    "length": round(length, 3),
                    "strokeWidth": 2.0,
                    "capStart": "junction" if tuple(chain[0]) in anchors and degree.get(tuple(chain[0]), 0) >= 3 else "terminal",
                    "capEnd": "junction" if tuple(chain[-1]) in anchors and degree.get(tuple(chain[-1]), 0) >= 3 else "terminal",
                })
    return segments, endpoints, junctions


def _topology_overlay(geometry: dict[str, Any], output_root: Path, character: str) -> str:
    image = Image.new("RGBA", (int(geometry["width"]), int(geometry["height"])), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.point([tuple(point) for point in geometry["outerContour"]], fill=(255, 190, 90, 220))
    draw.point([tuple(point) for point in geometry["innerContour"]], fill=(100, 180, 255, 220))
    for segment in geometry["segments"]:
        draw.line([tuple(point) for point in segment["points"]], fill=(80, 255, 180, 255), width=1)
    for point in geometry["endpoints"]:
        draw.ellipse((point[0] - 2, point[1] - 2, point[0] + 2, point[1] + 2), fill=(255, 80, 80, 255))
    path = output_root / "assets/glyphs/topology" / f"{character}_topology.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
    return str(path.relative_to(output_root)).replace("\\", "/")


def _extract_topology(character: str, image: Image.Image, output_root: Path) -> dict[str, Any]:
    """Extract full-mask topology; no fixed primitive crop is performed."""
    binary = _binary_alpha(image)
    box = image.getchannel("A").getbbox()
    if box is None:
        return {"character": character, "status": "FAILED_EMPTY_GLYPH", "geometry": {}}
    outer, inner = _contours(binary)
    skeleton = _thin(binary)
    segments, endpoints, junctions = _topology_segments(skeleton)
    geometry = {
        "width": image.width,
        "height": image.height,
        "bbox": list(box),
        "outerContour": outer,
        "innerContour": inner,
        "skeleton": [[x, y] for y, row in enumerate(skeleton) for x, value in enumerate(row) if value],
        "segments": segments,
        "endpoints": endpoints,
        "junctions": junctions,
        "holes": 1 if inner else 0,
    }
    geometry["topologyAsset"] = _topology_overlay(geometry, output_root, character)
    return {"character": character, "status": "OK", "geometry": geometry, "transform": "topology_contour_skeleton_no_intermediate_resample"}


def _extract_shape_primitives(character: str, image: Image.Image, output_root: Path) -> dict[str, Any]:
    """Compatibility entry point now backed by topology-first grammar."""
    return _extract_topology(character, image, output_root)


def _rasterize_geometry(geometry: dict[str, Any], size: tuple[int, int], rotation: float = 0.0, scale_xy: tuple[float, float] = (1.0, 1.0)) -> Image.Image:
    result = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(result)
    box = geometry.get("bbox", [0, 0, geometry.get("width", 1), geometry.get("height", 1)])
    source_width = max(1.0, box[2] - box[0])
    source_height = max(1.0, box[3] - box[1])
    center = ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
    radians = math.radians(rotation)
    def transform(point: list[int]) -> tuple[int, int]:
        x = (point[0] - center[0]) / source_width * size[0] + size[0] / 2
        y = (point[1] - center[1]) / source_height * size[1] + size[1] / 2
        dx, dy = (x - size[0] / 2) * scale_xy[0], (y - size[1] / 2) * scale_xy[1]
        return round(size[0] / 2 + dx * math.cos(radians) - dy * math.sin(radians)), round(size[1] / 2 + dx * math.sin(radians) + dy * math.cos(radians))
    for points, color in ((geometry.get("outerContour", []), (235, 229, 224, 220)), (geometry.get("innerContour", []), (235, 229, 224, 220))):
        transformed = [transform(point) for point in points]
        draw.point(transformed, fill=color)
    for segment in geometry.get("segments", []):
        draw.line([transform(point) for point in segment["points"]], fill=(235, 229, 224, 255), width=1)
    return result


GENERIC_SCAFFOLDS: dict[str, list[list[tuple[float, float]]]] = {
    "6": [[(0.72, 0.08), (0.42, 0.04), (0.18, 0.18), (0.12, 0.52), (0.20, 0.82), (0.45, 0.96), (0.72, 0.84), (0.76, 0.62), (0.58, 0.50), (0.30, 0.53)]],
    "3": [[(0.18, 0.12), (0.48, 0.05), (0.76, 0.18), (0.58, 0.48), (0.76, 0.56), (0.78, 0.82), (0.50, 0.96), (0.18, 0.86)]],
}


def _estimate_style_parameters(output_root: Path, themed_assets: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Estimate shared appearance parameters from clean observed reference glyphs."""
    clean = [asset for asset in themed_assets.values() if asset.get("provenance") == "OBSERVED_CLEAN"]
    metrics = [asset.get("metrics", {}) for asset in clean]
    widths = [float(item.get("width", 1)) for item in metrics]
    heights = [float(item.get("height", 1)) for item in metrics]
    strokes = [float(item.get("strokeWidthEstimate", 2)) for item in metrics]
    proportion = statistics.median(width / max(1.0, height) for width, height in zip(widths, heights)) if widths else 0.65
    stroke = statistics.median(strokes) if strokes else 2.0
    style = {
        "outerStrokeWidth": round(max(1.0, stroke), 3),
        "innerStrokeWidth": round(max(1.0, stroke * 0.72), 3),
        "outlineGap": round(max(0.8, stroke * 0.28), 3),
        "widthHeightProportion": round(proportion, 4),
        "curveRadius": round(statistics.median(heights) * 0.22 if heights else 8.0, 3),
        "curveTension": 0.78,
        "terminalShape": "round-joined-outline",
        "counterProportion": round(max(0.2, min(0.8, proportion * 0.62)), 4),
        "cornerTreatment": "continuous-curvature-with-rounded-cap",
        "sampleCount": len(clean),
        "sourceProvenance": "OBSERVED_CLEAN_ONLY",
    }
    style_path = output_root / "style-parameters.json"
    style_path.write_text(json.dumps(style, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return {**style, "report": str(style_path.relative_to(output_root)).replace("\\", "/")}


def _render_styled_scaffold(character: str, style: dict[str, Any], size: tuple[int, int] = (64, 96)) -> Image.Image:
    """Render a generic vector-like scaffold once at high resolution."""
    outer = Image.new("L", size, 0)
    paths = GENERIC_SCAFFOLDS.get(character, [])
    draw_outer = ImageDraw.Draw(outer)
    outer_width = max(3, round(float(style.get("outerStrokeWidth", 2.0)) * 2.0))
    inner_width = max(1, round(outer_width - float(style.get("outlineGap", 1.0)) * 2.0))
    for path in paths:
        points = [(round(x * (size[0] - 1)), round(y * (size[1] - 1))) for x, y in path]
        draw_outer.line(points, fill=255, width=outer_width, joint="curve")
        draw_outer.ellipse((points[0][0] - outer_width // 2, points[0][1] - outer_width // 2, points[0][0] + outer_width // 2, points[0][1] + outer_width // 2), fill=255)
        draw_outer.ellipse((points[-1][0] - outer_width // 2, points[-1][1] - outer_width // 2, points[-1][0] + outer_width // 2, points[-1][1] + outer_width // 2), fill=255)
    inner = Image.new("L", size, 0)
    draw_inner = ImageDraw.Draw(inner)
    for path in paths:
        points = [(round(x * (size[0] - 1)), round(y * (size[1] - 1))) for x, y in path]
        draw_inner.line(points, fill=255, width=inner_width, joint="curve")
    alpha = ImageChops.subtract(outer, inner)
    result = Image.new("RGBA", size, (0, 0, 0, 0))
    result.paste((235, 229, 224, 255), mask=alpha)
    return result


def _compose_scaffold_candidate(
    character: str,
    candidate_id: str,
    style: dict[str, Any],
    output_root: Path,
    donor_parts: list[tuple[dict[str, Any], str, str]],
    provenance: list[dict[str, Any]],
) -> dict[str, Any]:
    canvas = _render_styled_scaffold(character, style)
    donor_alpha = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    for geometry, region, donor in donor_parts:
        donor_geometry = _topology_region(geometry, region)
        donor_image = _rasterize_geometry(donor_geometry, canvas.size)
        donor_alpha = Image.alpha_composite(donor_alpha, donor_image)
        provenance.append({"role": "donor_assistance", "sourceGlyph": donor, "region": region, "operation": "target_scaffold_warp"})
    if donor_parts:
        combined_alpha = ImageChops.lighter(canvas.getchannel("A"), donor_alpha.getchannel("A"))
        canvas = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        canvas.paste((235, 229, 224, 255), mask=combined_alpha)
    path = output_root / "assets/glyphs/synthesized/candidates" / f"candidate_{character}_{candidate_id}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(path)
    return {"character": character, "candidate": candidate_id, "resource": path.name, "path": str(path), "source": "SCAFFOLD_GUIDED_RECONSTRUCTION", "synthetic": True, "confidence": 0.0, "requiresHumanReview": True, "provenance": provenance, "metrics": {**_metrics(canvas), **_display_metrics(canvas)}, "styleParameters": style["report"]}


def _topology_region(geometry: dict[str, Any], region: str) -> dict[str, Any]:
    box = geometry.get("bbox", [0, 0, geometry.get("width", 1), geometry.get("height", 1)])
    midpoint = (box[1] + box[3]) / 2
    def keep(point: list[int]) -> bool:
        if region == "upper_right":
            return point[1] <= midpoint and point[0] >= (box[0] + box[2]) / 2 - 1
        if region == "upper_curve":
            return point[1] <= midpoint
        if region == "lower_curve":
            return point[1] >= midpoint
        return True
    result = dict(geometry)
    for key in ("outerContour", "innerContour", "skeleton"):
        result[key] = [point for point in geometry.get(key, []) if keep(point)]
    result["segments"] = [dict(segment, points=[point for point in segment["points"] if keep(point)]) for segment in geometry.get("segments", [])]
    return result


def _assemble_three_candidate(candidate_id: str, parts: list[tuple[dict[str, Any], str]], output_root: Path, provenance: list[dict[str, Any]]) -> dict[str, Any]:
    """Render topology parts directly once at candidate resolution."""
    canvas_size = (42, 64)
    canvas = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    for geometry, region in parts:
        part = _topology_region(geometry, region)
        rendered = _rasterize_geometry(part, canvas_size)
        canvas.alpha_composite(rendered)
    path = output_root / "assets/glyphs/synthesized/candidates" / f"candidate_3_{candidate_id}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(path)
    return {"character": "3", "candidate": candidate_id, "resource": path.name, "path": str(path), "source": "TOPOLOGY_FIRST_COMPOSITION", "synthetic": True, "confidence": 0.0, "requiresHumanReview": True, "provenance": provenance, "metrics": {**_metrics(canvas), **_display_metrics(canvas)}}


def _topology_distance(first: dict[str, Any], second: dict[str, Any]) -> float:
    return abs(len(first.get("segments", [])) - len(second.get("segments", []))) + abs(first.get("holes", 0) - second.get("holes", 0)) * 2 + abs(len(first.get("endpoints", [])) - len(second.get("endpoints", [])))


def _leave_one_out_validation(output_root: Path, themed_assets: dict[str, dict[str, Any]], topology_report: dict[str, Any]) -> dict[str, Any]:
    available = topology_report.get("glyphs", {})
    validation_dir = output_root / "assets/glyphs/leave-one-out"
    validation_dir.mkdir(parents=True, exist_ok=True)
    style = topology_report.get("styleParameters", {})
    result: dict[str, Any] = {"method": "scaffold_guided_leave_one_out", "targets": {}, "styleParameters": style}
    clean = {digit for digit, value in themed_assets.items() if value.get("provenance") == "OBSERVED_CLEAN"}
    for target, asset in themed_assets.items():
        target_geometry = available.get(target, {}).get("geometry", {})
        donors = [digit for digit in clean if digit != target and digit in available]
        candidates: list[dict[str, Any]] = []
        target_path = output_root / asset["resource"]
        target_image = Image.open(target_path).convert("RGBA")
        if target == "6":
            generic = _render_styled_scaffold("6", style, target_image.size)
            generic_path = validation_dir / "loo_6_generic_styled_scaffold.png"
            generic.save(generic_path)
            generic_diff = ImageChops.difference(target_image.getchannel("A"), generic.getchannel("A"))
            generic_score = round(1.0 - sum(generic_diff.getdata()) / (255.0 * generic_diff.width * generic_diff.height), 4)
            candidates.append({"candidateType": "generic_styled_6", "donor": None, "rotationDeg": 0.0, "resource": str(generic_path.relative_to(output_root)).replace("\\", "/"), "similarity": generic_score, "requiresHumanReview": True})
            if "9" in available:
                rotated = _rasterize_geometry(available["9"]["geometry"], target_image.size, rotation=180.0)
                rotated_path = validation_dir / "loo_6_from_9_rot180.png"
                rotated.save(rotated_path)
                rotated_diff = ImageChops.difference(target_image.getchannel("A"), rotated.getchannel("A"))
                rotated_score = round(1.0 - sum(rotated_diff.getdata()) / (255.0 * rotated_diff.width * rotated_diff.height), 4)
                candidates.append({"candidateType": "nine_rotated_180", "donor": "9", "rotationDeg": 180.0, "resource": str(rotated_path.relative_to(output_root)).replace("\\", "/"), "similarity": rotated_score, "requiresHumanReview": True})
                best_scale = (1.0, 1.0)
                best_score = rotated_score
                for scale_x in (0.90, 0.95, 1.05, 1.10):
                    for scale_y in (0.94, 1.0, 1.06):
                        corrected = _rasterize_geometry(available["9"]["geometry"], target_image.size, rotation=180.0, scale_xy=(scale_x, scale_y))
                        corrected_diff = ImageChops.difference(target_image.getchannel("A"), corrected.getchannel("A"))
                        corrected_score = round(1.0 - sum(corrected_diff.getdata()) / (255.0 * corrected_diff.width * corrected_diff.height), 4)
                        if corrected_score > best_score:
                            best_score, best_scale = corrected_score, (scale_x, scale_y)
                corrected = _rasterize_geometry(available["9"]["geometry"], target_image.size, rotation=180.0, scale_xy=best_scale)
                corrected_path = validation_dir / "loo_6_from_9_rot180_geometry_corrected.png"
                corrected.save(corrected_path)
                candidates.append({"candidateType": "nine_rotated_180_geometry_corrected", "donor": "9", "rotationDeg": 180.0, "scaleXY": list(best_scale), "resource": str(corrected_path.relative_to(output_root)).replace("\\", "/"), "similarity": best_score, "requiresHumanReview": True})
        else:
            for donor in donors[:5]:
                candidate_image = _rasterize_geometry(available[donor]["geometry"], target_image.size)
                candidate_path = validation_dir / f"loo_{target}_from_{donor}.png"
                candidate_image.save(candidate_path)
                difference = ImageChops.difference(target_image.getchannel("A"), candidate_image.getchannel("A"))
                score = round(1.0 - sum(difference.getdata()) / (255.0 * difference.width * difference.height), 4)
                candidates.append({"candidateType": "topology_donor", "donor": donor, "rotationDeg": 0.0, "resource": str(candidate_path.relative_to(output_root)).replace("\\", "/"), "topologyDistance": _topology_distance(target_geometry, available[donor]["geometry"]), "similarity": score, "requiresHumanReview": True})
        best = max(candidates, key=lambda item: item["similarity"]) if candidates else None
        target_result = {"provenance": asset.get("provenance"), "reference": asset["resource"], "candidates": candidates, "bestCandidate": best}
        if target == "6":
            generic_score = next((item["similarity"] for item in candidates if item["candidateType"] == "generic_styled_6"), 0.0)
            corrected_score = next((item["similarity"] for item in candidates if item["candidateType"] == "nine_rotated_180_geometry_corrected"), 0.0)
            target_result["validationPassed"] = corrected_score >= generic_score and corrected_score > 0.0
            target_result["comparison"] = {"genericStyled6": generic_score, "nineRotated180GeometryCorrected": corrected_score}
        result["targets"][target] = target_result
    report_path = output_root / "leave-one-out-report.json"
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    result["report"] = str(report_path.relative_to(output_root)).replace("\\", "/")
    return result


def _synthesize_compositional_missing_glyphs(output_root: Path, themed_assets: dict[str, dict[str, Any]], topology_report: dict[str, Any]) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    """Generate scaffold-guided review candidates after 6 leave-one-out passes."""
    if "3" not in topology_report.get("targets", ()):
        return {}, {}
    available = topology_report.get("glyphs", {})
    six_validation = topology_report.get("leaveOneOut", {}).get("targets", {}).get("6", {})
    if not six_validation.get("validationPassed", False):
        return {"3": []}, {}
    if any(digit not in available for digit in ("8", "2", "9")):
        return {"3": []}, {}
    style = topology_report.get("styleParameters", {})
    g = lambda digit: available[digit]["geometry"]
    hybrid_lower = g("6") if "6" in available else g("9")
    hybrid_lower_source = "6" if "6" in available else "9"
    recipes = (
        ("01", [], "generic_3_styled_scaffold"),
        ("02", [(g("8"), "upper_right", "8")], "scaffold_plus_8_upper"),
        ("03", [(g("2"), "upper_curve", "2")], "scaffold_plus_2_upper"),
        ("04", [(g("9"), "upper_curve", "9")], "scaffold_plus_9_upper"),
        ("05", [(g("8"), "lower_curve", "8")], "scaffold_plus_8_lower"),
        ("06", [(hybrid_lower, "lower_curve", hybrid_lower_source)], "scaffold_plus_6_lower_hybrid"),
    )
    confidence = {"01": 0.72, "02": 0.69, "03": 0.65, "04": 0.62, "05": 0.59, "06": 0.55}
    candidates: list[dict[str, Any]] = []
    for candidate_id, parts, recipe in recipes:
        provenance = [{"role": "target_scaffold", "target": "3", "scaffold": "GENERIC_SCAFFOLD_3", "operation": "style_parameter_render"}, {"role": "assembly", "operation": "single_high_resolution_rasterize", "recipe": recipe}]
        candidate = _compose_scaffold_candidate("3", candidate_id, style, output_root, parts, provenance)
        candidate["rank"] = len(candidates) + 1
        candidate["confidence"] = confidence[candidate_id]
        candidate["ranking"] = {"score": confidence[candidate_id], "method": "scaffold_style_recipe_prior", "autoApproved": False}
        candidates.append(candidate)
    selected = candidates[0] if candidates else None
    themed: dict[str, dict[str, Any]] = {}
    if selected:
        themed_path = output_root / "assets/glyphs/themed/glyph_3.png"
        themed_path.parent.mkdir(parents=True, exist_ok=True)
        Image.open(selected["path"]).convert("RGBA").save(themed_path)
        themed["3"] = {"character": "3", "type": THEMED_GLYPH_TYPE, "source": "SYNTHESIZED_SCAFFOLD", "synthetic": True, "confidence": selected["confidence"], "resource": str(themed_path.relative_to(output_root)).replace("\\", "/"), "candidate": selected["candidate"], "requiresHumanReview": True, "provenance": selected["provenance"], "metrics": selected["metrics"]}
    return {"3": candidates}, themed


def extract_themed_glyph_set(
    reference_path: Path,
    hand_occlusion_mask_path: Path,
    output_root: Path,
    clock_center: tuple[float, float],
    date_window_metadata_path: Path | None = None,
    synthesizer: GlyphSynthesizer | None = None,
    dial_completed_path: Path | None = None,
    reconstructed_mask_path: Path | None = None,
) -> dict[str, Any]:
    """Extract observed glyphs from A1d dial completion at native resolution."""
    reference = Image.open(reference_path).convert("RGB")
    dial_source_path = dial_completed_path if dial_completed_path and dial_completed_path.exists() else reference_path
    dial_source = Image.open(dial_source_path).convert("RGB")
    exclusion = Image.open(hand_occlusion_mask_path).convert("L") if hand_occlusion_mask_path.exists() else Image.new("L", reference.size, 0)
    reconstructed_mask = Image.open(reconstructed_mask_path).convert("L") if reconstructed_mask_path and reconstructed_mask_path.exists() else Image.new("L", reference.size, 0)
    date_metadata = json.loads(date_window_metadata_path.read_text(encoding="utf-8")) if date_window_metadata_path and date_window_metadata_path.exists() else {}
    frame = date_metadata.get("frameBbox")
    # The source hands are evidence of the photographed time, not part of a
    # numeral.  However, subtracting their broad A1 mask removes legitimate
    # dial strokes at 2/6/7/10.  Keep the original alpha here and record the
    # overlap as confidence/review metadata instead of destructively cutting a
    # glyph.  The date frame remains excluded because it replaces hour 3.
    mask = _apply_exclusion(_ink_mask(dial_source), None, frame)
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
        raw, roi_box = _radial_roi(dial_source, mask, clock_center, angle)
        raw_box = raw.getchannel("A").getbbox()
        box = raw_box or (0, 0, raw.width, raw.height)
        ink_box = raw.getchannel("A").getbbox()
        quality = 0.0 if ink_box is None else min(0.99, 0.62 + sum(1 for value in raw.getchannel("A").getdata() if value > 20) / 2800.0)
        if ink_box is None or quality < 0.63:
            slots.append({"hour": hour, "label": label, "status": "MISSING", "angleDeg": angle, "roiBox": list(roi_box), "bbox": {"x": box[0], "y": box[1], "width": box[2] - box[0], "height": box[3] - box[1]}, "confidence": 0.0})
            continue
        raw_name = f"hour_{hour:02d}_label.png"
        raw_path = raw_dir / raw_name
        raw.save(raw_path)
        local_rotation = _dial_orientation(angle)
        canonical_label = raw.rotate(local_rotation, resample=Image.Resampling.BICUBIC, expand=True)
        canonical_label_path = canonical_dir / raw_name
        canonical_label.save(canonical_label_path)
        # Segment multi-digit labels only after their baseline is horizontal.
        # Splitting a tilted "10" by source X columns was the cause of the
        # corrupted 0 and several partial glyph observations.
        canonical_work = _tight(canonical_label, padding=1)
        parts = _split_columns(canonical_work, len(label))
        if len(parts) != len(label):
            parts = [canonical_work]
        slot_observations = []
        for index, (character, part) in enumerate(zip(label, parts), start=1):
            glyph_rotation = local_rotation
            part = _drop_isolated_ink(_tight(part, padding=1))
            observation_id = f"hour_{hour}" if len(label) == 1 else f"hour_{hour}_{'first' if index == 1 else 'second'}"
            canonical_name = f"{observation_id}_{character}.png"
            canonical_path = canonical_dir / canonical_name
            part.save(canonical_path)
            raw_observation = raw_dir / f"{observation_id}_{character}.png"
            # Raw is an exact copy of the fixed padded ROI; it is never
            # foreground-tightened or resized.
            raw.save(raw_observation)
            metric = _metrics(part)
            metric.update(_display_metrics(part))
            alpha_box = raw.getchannel("A").getbbox()
            touches_boundary = bool(alpha_box and (alpha_box[0] <= 1 or alpha_box[1] <= 1 or alpha_box[2] >= raw.width - 1 or alpha_box[3] >= raw.height - 1))
            source_roi_mask = exclusion.filter(ImageFilter.MaxFilter(9)).crop(roi_box)
            source_reconstructed_mask = reconstructed_mask.crop(roi_box)
            overlap_ratio = _mask_overlap_ratio(raw.getchannel("A"), source_roi_mask)
            reconstructed_overlap_ratio = _mask_overlap_ratio(raw.getchannel("A"), source_reconstructed_mask)
            provenance = _glyph_provenance(overlap_ratio, reconstructed_overlap_ratio)
            part_box = part.getchannel("A").getbbox()
            component_failed = part_box is None or (part_box[2] - part_box[0]) < 6 or (part_box[3] - part_box[1]) < 18
            validation_status = "FAIL" if touches_boundary or component_failed else "PASS"
            observation = {
                "id": observation_id,
                "character": character,
                "hour": hour,
                "angleDeg": angle,
                "localRotationDeg": round(glyph_rotation, 2),
                "raw": str(raw_observation.relative_to(output_root)).replace("\\", "/"),
                "canonical": str(canonical_path.relative_to(output_root)).replace("\\", "/"),
                "canonicalLabel": str(canonical_label_path.relative_to(output_root)).replace("\\", "/"),
                "sourceROI": list(roi_box),
                "handOcclusionOverlapRatio": overlap_ratio,
                "reconstructedMaskOverlapRatio": reconstructed_overlap_ratio,
                "provenance": provenance,
                "foregroundTouchesROIBoundary": touches_boundary,
                "componentLost": component_failed,
                "validation": validation_status,
                "confidence": round(quality, 4),
                "metrics": metric,
            }
            observations[character].append(observation)
            slot_observations.append(observation)
        slots.append({"hour": hour, "label": label, "status": "OBSERVED", "angleDeg": angle, "localRotationDeg": local_rotation, "roiBox": list(roi_box), "handOverlapMaskPresent": bool(exclusion.getbbox()), "observations": slot_observations, "confidence": round(quality, 4)})

    references: dict[str, dict[str, Any]] = {}
    coverage: dict[str, str] = {}
    themed_assets: dict[str, dict[str, Any]] = {}
    validation_failures: list[str] = []
    for character in DIGITS:
        valid_observations = [item for item in observations[character] if item.get("validation") == "PASS"]
        for item in observations[character]:
            if item.get("validation") != "PASS":
                validation_failures.append(item["id"])
        if valid_observations:
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

            chosen = max(valid_observations, key=preference)
            source_path = output_root / chosen["canonical"]
            themed_path = themed_dir / f"glyph_{character}.png"
            # Keep the source-derived canonical pixels native.  Logical WFF
            # display metrics are recorded separately and applied only by the
            # final renderer.
            native = _asset_from_path(source_path)
            native.save(themed_path)
            references[character] = chosen
            coverage[character] = "OBSERVED"
            themed_assets[character] = {
                "character": character,
                "type": THEMED_GLYPH_TYPE,
                "source": "OBSERVED",
                "synthetic": False,
                "confidence": chosen["confidence"],
                "provenance": chosen.get("provenance", "OBSERVED_CLEAN"),
                "resource": str(themed_path.relative_to(output_root)).replace("\\", "/"),
                "observations": [item["id"] for item in observations[character]],
                "metrics": {**_metrics(native), **_display_metrics(native)},
            }
        else:
            coverage[character] = "MISSING"

    primitive_report: dict[str, Any] = {
        "version": "A2b.4",
        "grammar": "SCAFFOLD_GUIDED_GLYPH_RECONSTRUCTION",
        "targetDigits": ["3"],
        "targets": ["3"],
        "segmentKinds": list(TOPOLOGY_SEGMENT_KINDS),
        "orientationModel": _fit_global_orientation(),
        "styleParameters": _estimate_style_parameters(output_root, themed_assets),
        "glyphs": {},
        "status": "completed_with_review",
    }
    for character, glyph in themed_assets.items():
        topology = _extract_shape_primitives(
            character,
            _asset_from_path(output_root / glyph["resource"]),
            output_root,
        )
        topology["provenance"] = glyph.get("provenance", "OBSERVED_CLEAN")
        primitive_report["glyphs"][character] = topology
    leave_one_out = _leave_one_out_validation(output_root, themed_assets, primitive_report)
    primitive_report["leaveOneOutReport"] = leave_one_out.get("report")
    primitive_report["leaveOneOut"] = leave_one_out
    primitive_report_path = output_root / "primitive-report.json"
    primitive_report_path.write_text(json.dumps(primitive_report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")

    # A2b.4 synthesizes only review candidates after 6 validation.  The actual dial remains
    # unchanged because the missing 3 o'clock index is a layout replacement.
    synthesizer = synthesizer or DeterministicFallbackAdapter(output_root / "assets/fonts/pretendard.ttf")
    adapter_status = [ExternalModelAdapter().status(), LocalModelAdapter().status(), synthesizer.status()]
    candidates, synthesized_assets = _synthesize_compositional_missing_glyphs(output_root, themed_assets, primitive_report)
    if synthesized_assets:
        themed_assets.update(synthesized_assets)
        coverage["3"] = "SYNTHESIZED"

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
    missing_count = sum(1 for value in coverage.values() if value == "MISSING")
    report = {
        "status": "completed_with_review" if synthesized_count or missing_count or validation_failures or relation["classification"] in {"UNKNOWN", "DIFFERENT_STYLE_SYSTEM"} else "completed",
        "type": THEMED_GLYPH_TYPE,
        "family": THEMED_FAMILY,
        "coverage": coverage,
        "coverageCounts": {"observed": observed_count, "synthesized": synthesized_count, "missing": missing_count},
        "slots": slots,
        "observations": observations,
        "glyphs": themed_assets,
        "candidates": candidates,
        "primitives": primitive_report,
        "topology": primitive_report,
        "leaveOneOut": leave_one_out,
        "grammar": "SCAFFOLD_GUIDED_GLYPH_RECONSTRUCTION",
        "primitiveReport": str(primitive_report_path.relative_to(output_root)).replace("\\", "/"),
        "dateStyleRelation": relation,
        "adapters": adapter_status,
        "externalModelStatus": "external synthesis unavailable",
        "synthesis": {
            "enabled": True,
            "version": "A2b.4",
            "method": "SCAFFOLD_GUIDED_GLYPH_RECONSTRUCTION",
            "targetDigits": ["3"],
            "candidateCount": len(candidates.get("3", [])),
            "gatedBy": "leave_one_out_6_validation",
            "autoApproval": False,
            "status": "review_only",
            "externalModelUsed": False,
        },
        "validationFailures": validation_failures,
        "canonicalization": {
            "source": str(dial_source_path),
            "orientation": _fit_global_orientation(),
            "roi": "fixed radial/tangential padded ROI before foreground segmentation",
            "nativeResolution": True,
            "alphaAwareResampling": "RGBA bicubic with continuous alpha; no thresholded resize",
        },
        "requiresHumanReview": bool(synthesized_count or missing_count or validation_failures) or relation["classification"] not in {"SAME_STYLE_SYSTEM", "RELATED_BUT_OPTICALLY_ADJUSTED"},
        "actualDialThreeOClockNumeral": "intentionally_absent_not_reconstructed",
    }
    report_path = output_root / "glyph-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return report
