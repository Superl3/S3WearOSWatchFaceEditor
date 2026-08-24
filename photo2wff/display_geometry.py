from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Iterable

from PIL import Image


Point = tuple[float, float]


@dataclass(frozen=True)
class RoundedRect:
    """A centered display boundary with one radius shared by all four corners.

    A circle is deliberately represented by the same primitive: width == height
    and radius == width / 2.  This keeps the mapping code independent of the
    source/target display family.
    """

    width: float
    height: float
    radius: float
    center_x: float = 0.0
    center_y: float = 0.0

    def __post_init__(self) -> None:
        values = (self.width, self.height, self.radius, self.center_x, self.center_y)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("display geometry values must be finite")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("display width and height must be positive")
        if self.radius < 0 or self.radius > min(self.width, self.height) / 2:
            raise ValueError("corner radius must be between zero and half the shortest side")

    @property
    def is_circle(self) -> bool:
        return math.isclose(self.width, self.height, abs_tol=1e-6) and math.isclose(self.radius, self.width / 2, abs_tol=1e-6)

    @property
    def shape(self) -> str:
        return "CIRCLE" if self.is_circle else "ROUNDED_RECT"

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RoundedRect":
        return cls(
            width=float(value["width"]),
            height=float(value["height"]),
            radius=float(value["radius"]),
            center_x=float(value.get("centerX", value.get("center_x", 0.0))),
            center_y=float(value.get("centerY", value.get("center_y", 0.0))),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "shape": self.shape,
            "width": round(self.width, 6),
            "height": round(self.height, 6),
            "radius": round(self.radius, 6),
            "centerX": round(self.center_x, 6),
            "centerY": round(self.center_y, 6),
            "isCircleSpecialCase": self.is_circle,
        }

    def bounds(self) -> tuple[float, float, float, float]:
        half_width = self.width / 2
        half_height = self.height / 2
        return (
            self.center_x - half_width,
            self.center_y - half_height,
            self.center_x + half_width,
            self.center_y + half_height,
        )

    def contains(self, point: Point) -> bool:
        x = abs(point[0] - self.center_x)
        y = abs(point[1] - self.center_y)
        half_width = self.width / 2
        half_height = self.height / 2
        if x > half_width or y > half_height:
            return False
        if self.radius == 0 or x <= half_width - self.radius or y <= half_height - self.radius:
            return True
        corner_x = half_width - self.radius
        corner_y = half_height - self.radius
        return (x - corner_x) ** 2 + (y - corner_y) ** 2 <= self.radius ** 2 + 1e-7

    def boundary_distance(self, direction: Point) -> float:
        """Return center-to-boundary distance for a direction vector.

        Binary search is intentional here.  It handles the rectangle, rounded
        corners, and the circle special case with one deterministic predicate,
        avoiding separate corner-intersection formulas that drift at tangents.
        """

        dx, dy = direction
        length = math.hypot(dx, dy)
        if length == 0:
            raise ValueError("direction must be non-zero")
        dx /= length
        dy /= length
        if self.is_circle:
            return self.width / 2
        low = 0.0
        high = max(self.width, self.height)
        while self.contains((self.center_x + dx * high, self.center_y + dy * high)):
            high *= 2
        for _ in range(52):
            middle = (low + high) / 2
            point = (self.center_x + dx * middle, self.center_y + dy * middle)
            if self.contains(point):
                low = middle
            else:
                high = middle
        return (low + high) / 2


def direction_from_angle(angle_degrees: float) -> Point:
    """Convert the A1 clock convention (0 degrees is 12 o'clock) to XY."""

    radians = math.radians(angle_degrees)
    return math.sin(radians), -math.cos(radians)


def angle_from_direction(direction: Point) -> float:
    return math.degrees(math.atan2(direction[0], -direction[1])) % 360


def boundary_normalized_map(point: Point, source: RoundedRect, target: RoundedRect) -> Point:
    """Map a point by preserving its normalized radial position.

    For p = center + unit * rho, the normalized radial position is
    rho / boundaryDistance(source, unit).  That scalar is then applied to the
    target boundary in the same direction.
    """

    source_vector = (point[0] - source.center_x, point[1] - source.center_y)
    rho = math.hypot(*source_vector)
    if rho == 0:
        return target.center_x, target.center_y
    direction = (source_vector[0] / rho, source_vector[1] / rho)
    source_boundary = source.boundary_distance(direction)
    normalized = rho / source_boundary
    target_boundary = target.boundary_distance(direction)
    return (
        target.center_x + direction[0] * normalized * target_boundary,
        target.center_y + direction[1] * normalized * target_boundary,
    )


def naive_xy_map(point: Point, source: RoundedRect, target: RoundedRect) -> Point:
    """Legacy center-preserving XY stretch used as the human-review baseline."""

    return (
        target.center_x + (point[0] - source.center_x) * target.width / source.width,
        target.center_y + (point[1] - source.center_y) * target.height / source.height,
    )


def map_bbox(bbox: dict[str, float], source: RoundedRect, target: RoundedRect, mapper=boundary_normalized_map) -> dict[str, float]:
    corners = (
        (float(bbox["x"]), float(bbox["y"])),
        (float(bbox["x"]) + float(bbox["width"]), float(bbox["y"])),
        (float(bbox["x"]), float(bbox["y"]) + float(bbox["height"])),
        (float(bbox["x"]) + float(bbox["width"]), float(bbox["y"]) + float(bbox["height"])),
    )
    mapped = [mapper(corner, source, target) for corner in corners]
    left = min(point[0] for point in mapped)
    top = min(point[1] for point in mapped)
    right = max(point[0] for point in mapped)
    bottom = max(point[1] for point in mapped)
    return {"x": left, "y": top, "width": right - left, "height": bottom - top}


def map_analog_hand(element: dict[str, Any], source: RoundedRect, target: RoundedRect, source_clock_center: Point | None = None, target_clock_center: Point | None = None) -> dict[str, Any]:
    """Map hand geometry while keeping the transparent asset in local space."""

    source_pivot = source_clock_center or (source.center_x, source.center_y)
    target_pivot = target_clock_center or (target.center_x, target.center_y)
    direction = direction_from_angle(float(element["observedAngleDeg"]))
    source_boundary = source.boundary_distance(direction)
    normalized_length = float(element["length"]) / source_boundary
    target_length = normalized_length * target.boundary_distance(direction)
    thickness = float(element.get("thickness", 1.0))
    endpoint = (target_pivot[0] + direction[0] * target_length, target_pivot[1] + direction[1] * target_length)
    normal = (-direction[1], direction[0])
    points = [
        target_pivot,
        endpoint,
        (target_pivot[0] + normal[0] * thickness / 2, target_pivot[1] + normal[1] * thickness / 2),
        (target_pivot[0] - normal[0] * thickness / 2, target_pivot[1] - normal[1] * thickness / 2),
    ]
    bbox = {
        "x": min(point[0] for point in points),
        "y": min(point[1] for point in points),
        "width": max(point[0] for point in points) - min(point[0] for point in points),
        "height": max(point[1] for point in points) - min(point[1] for point in points),
    }
    return {
        "id": element.get("id"),
        "role": element.get("role"),
        "sourcePivot": {"x": source_pivot[0], "y": source_pivot[1]},
        "targetPivot": {"x": target_pivot[0], "y": target_pivot[1]},
        "observedAngleDeg": float(element["observedAngleDeg"]),
        "normalizedLength": normalized_length,
        "sourceBoundaryDistance": source_boundary,
        "targetBoundaryDistance": target.boundary_distance(direction),
        "sourceLength": float(element["length"]),
        "targetLength": target_length,
        "thicknessPreserved": thickness,
        "targetBbox": bbox,
    }


def map_structured_element(element: dict[str, Any], source: RoundedRect, target: RoundedRect, source_clock_center: Point | None = None, target_clock_center: Point | None = None) -> dict[str, Any]:
    if element.get("type") == "ANALOG_HAND":
        return map_analog_hand(element, source, target, source_clock_center, target_clock_center)
    bbox = element.get("bbox", {})
    mapped_bbox = map_bbox(bbox, source, target)
    anchor = (
        float(bbox.get("x", 0)) + float(bbox.get("width", 0)) / 2,
        float(bbox.get("y", 0)) + float(bbox.get("height", 0)) / 2,
    )
    mapped_anchor = boundary_normalized_map(anchor, source, target)
    return {
        "id": element.get("id"),
        "type": element.get("type"),
        "sourceAnchor": {"x": anchor[0], "y": anchor[1]},
        "targetAnchor": {"x": mapped_anchor[0], "y": mapped_anchor[1]},
        "targetBbox": mapped_bbox,
        "localAppearance": "preserved",
    }


def map_element_preserving(element: dict[str, Any], source: RoundedRect, target: RoundedRect) -> dict[str, Any]:
    """Map an artwork anchor while applying only a uniform local transform."""

    bbox = element["bbox"]
    anchor_value = element.get("anchor") or {
        "x": float(bbox["x"]) + float(bbox["width"]) / 2,
        "y": float(bbox["y"]) + float(bbox["height"]) / 2,
    }
    source_anchor = (float(anchor_value["x"]), float(anchor_value["y"]))
    mapped_anchor = boundary_normalized_map(source_anchor, source, target)
    vector = (source_anchor[0] - source.center_x, source_anchor[1] - source.center_y)
    vector_length = math.hypot(*vector)
    direction = (0.0, -1.0) if vector_length == 0 else (vector[0] / vector_length, vector[1] / vector_length)
    boundary_scale = target.boundary_distance(direction) / source.boundary_distance(direction)
    uniform_scale = boundary_scale * float(element.get("scale", 1.0)) * float(element.get("opticalScale", 1.0))
    target_anchor = (
        mapped_anchor[0] + float(element.get("opticalOffsetX", 0.0)),
        mapped_anchor[1] + float(element.get("opticalOffsetY", 0.0)),
    )
    width = float(bbox["width"]) * uniform_scale
    height = float(bbox["height"]) * uniform_scale
    source_rotation = float(element.get("rotation", 0.0))
    rotation = float(element.get("opticalRotation", 0.0))
    return {
        "id": element.get("id"),
        "type": element.get("type"),
        "mappingMode": "ELEMENT_PRESERVING",
        "sourceAnchor": {"x": source_anchor[0], "y": source_anchor[1]},
        "targetAnchor": {"x": target_anchor[0], "y": target_anchor[1]},
        "sourceAngleDeg": angle_from_direction(direction),
        "uniformScale": uniform_scale,
        "rotation": rotation,
        "sourceRotation": source_rotation,
        "targetBbox": {
            "x": target_anchor[0] - width / 2,
            "y": target_anchor[1] - height / 2,
            "width": width,
            "height": height,
        },
        "opticalCorrection": {
            "x": float(element.get("opticalOffsetX", 0.0)),
            "y": float(element.get("opticalOffsetY", 0.0)),
            "scale": float(element.get("opticalScale", 1.0)),
            "rotation": float(element.get("opticalRotation", 0.0)),
        },
        "localAppearance": "uniform_scale_and_rotation_only",
    }


def _bilinear_sample(image: Image.Image, point: Point) -> tuple[int, int, int, int]:
    x, y = point
    if x < 0 or y < 0 or x >= image.width or y >= image.height:
        return 0, 0, 0, 0
    left = min(image.width - 1, max(0, math.floor(x)))
    top = min(image.height - 1, max(0, math.floor(y)))
    right = min(image.width - 1, left + 1)
    bottom = min(image.height - 1, top + 1)
    fx = x - left
    fy = y - top
    values = []
    for channel in range(4):
        top_value = image.getpixel((left, top))[channel] * (1 - fx) + image.getpixel((right, top))[channel] * fx
        bottom_value = image.getpixel((left, bottom))[channel] * (1 - fx) + image.getpixel((right, bottom))[channel] * fx
        values.append(round(top_value * (1 - fy) + bottom_value * fy))
    return tuple(values)  # type: ignore[return-value]


def inverse_raster_map(image: Image.Image, source: RoundedRect, target: RoundedRect, output_size: tuple[int, int] | None = None) -> Image.Image:
    """Inverse-map a static raster; structured layers use geometry instead."""

    width, height = output_size or image.size
    source_rgba = image.convert("RGBA")
    result = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    pixels = result.load()
    scale_x = width / image.width
    scale_y = height / image.height
    for y in range(height):
        target_y = (y + 0.5) / scale_y - 0.5
        for x in range(width):
            target_x = (x + 0.5) / scale_x - 0.5
            target_point = (target_x, target_y)
            if not target.contains(target_point):
                continue
            source_point = _inverse_map_point(target_point, source, target)
            pixels[x, y] = _bilinear_sample(source_rgba, source_point)
    return result


def _inverse_map_point(point: Point, source: RoundedRect, target: RoundedRect) -> Point:
    vector = (point[0] - target.center_x, point[1] - target.center_y)
    rho = math.hypot(*vector)
    if rho == 0:
        return source.center_x, source.center_y
    direction = (vector[0] / rho, vector[1] / rho)
    normalized = rho / target.boundary_distance(direction)
    return (
        source.center_x + direction[0] * normalized * source.boundary_distance(direction),
        source.center_y + direction[1] * normalized * source.boundary_distance(direction),
    )


def shape_from_scene(value: dict[str, Any]) -> RoundedRect:
    return RoundedRect.from_dict(value)


def points_on_boundary(shape: RoundedRect, angles: Iterable[float]) -> list[Point]:
    points: list[Point] = []
    for angle in angles:
        direction = direction_from_angle(angle)
        distance = shape.boundary_distance(direction)
        points.append((shape.center_x + direction[0] * distance, shape.center_y + direction[1] * distance))
    return points
