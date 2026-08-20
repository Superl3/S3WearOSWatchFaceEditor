from __future__ import annotations

import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path


class WffValidationError(ValueError):
    pass


def validate_wff_xml(xml_path: Path, format_version: int = 1, validator_jar: Path | None = None) -> str:
    """Run deterministic structural checks and optionally Google's XSD validator."""
    try:
        root = ET.parse(xml_path).getroot()
    except ET.ParseError as error:
        raise WffValidationError(f"XML parse failed: {error}") from error
    if root.tag != "WatchFace":
        raise WffValidationError(f"root must be WatchFace, got {root.tag}")
    if root.attrib.get("width") != "438" or root.attrib.get("height") != "438":
        raise WffValidationError("MVP WFF canvas must be width=438 height=438")
    scene = root.find("Scene")
    if scene is None:
        raise WffValidationError("WatchFace must contain Scene")
    for child in scene:
        if child.tag in {"DigitalClock", "PartText", "PartImage", "Group"}:
            for key in ("x", "y", "width", "height"):
                if key not in child.attrib:
                    raise WffValidationError(f"{child.tag} is missing required geometry attribute '{key}'")
    if validator_jar is None:
        return "structural validation passed"
    result = subprocess.run(
        ["java", "-jar", str(validator_jar), str(format_version), str(xml_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0 or "PASSED" not in output:
        raise WffValidationError(output or f"validator exited with {result.returncode}")
    return output

