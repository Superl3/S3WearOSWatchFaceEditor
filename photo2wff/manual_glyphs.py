from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from PIL import Image

MANUAL_GLYPH_FAMILY = "Photo2WFFManualGlyphs"


def import_manual_glyphs(source_dir: Path | None, output_root: Path) -> dict[str, Any] | None:
    """Copy user-supplied digit PNGs without synthesis or preprocessing."""
    if source_dir is None:
        return None
    if not source_dir.is_dir():
        raise ValueError(f"manual glyph directory does not exist: {source_dir}")
    destination_dir = output_root / "assets" / "manual-glyphs"
    destination_dir.mkdir(parents=True, exist_ok=True)
    resources: dict[str, str] = {}
    metrics: dict[str, dict[str, int]] = {}
    for character in "0123456789":
        source = source_dir / f"{character}.png"
        if not source.exists():
            continue
        with Image.open(source) as image:
            if image.width <= 0 or image.height <= 0:
                raise ValueError(f"manual glyph is empty: {source}")
            width, height = image.size
        destination = destination_dir / f"manual_glyph_{character}.png"
        shutil.copy2(source, destination)
        resource = str(destination.relative_to(output_root)).replace("\\", "/")
        resources[character] = resource
        metrics[character] = {"width": width, "height": height, "displayWidth": width, "displayHeight": height}
    if not resources:
        raise ValueError(f"manual glyph directory contains no 0.png-9.png files: {source_dir}")
    override = {
        "type": "MANUAL_GLYPH_OVERRIDE",
        "family": MANUAL_GLYPH_FAMILY,
        "fallbackFamily": "Pretendard",
        "resources": resources,
        "metrics": metrics,
        "providedDigits": sorted(resources),
        "sourceDirectory": str(source_dir),
        "automaticSynthesis": False,
    }
    (output_root / "manual-glyph-overrides.json").write_text(json.dumps(override, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return override
