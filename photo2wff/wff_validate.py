from __future__ import annotations

import os
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path


class WffValidationError(ValueError):
    pass


def lint_wff_xml(xml_path: Path) -> str:
    """Run Photo2WFF's fast structural lint without claiming WFF validity."""
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
    return "Photo2WFF structural lint passed"


def _default_validator_jar() -> Path:
    configured = os.environ.get("PHOTO2WFF_WFF_VALIDATOR_JAR")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parent.parent / "tools" / "wff-validator" / "wff-validator.jar"


def validate_wff_xml(xml_path: Path, format_version: int = 1, validator_jar: Path | None = None) -> str:
    """Require Google's official XSD validator and fail closed when unavailable."""
    lint_wff_xml(xml_path)
    validator_jar = validator_jar or _default_validator_jar()
    if not validator_jar.is_file():
        raise WffValidationError(
            "Google WFF validator is required but unavailable: "
            f"{validator_jar}. Run scripts/setup_wff_validator.ps1 or set "
            "PHOTO2WFF_WFF_VALIDATOR_JAR."
        )
    try:
        result = subprocess.run(
            ["java", "-jar", str(validator_jar), str(format_version), str(xml_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as error:
        raise WffValidationError(f"Google WFF validator could not execute: {error}") from error
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0 or "PASSED" not in output or "FAILED" in output:
        raise WffValidationError(output or f"validator exited with {result.returncode}")
    return output
