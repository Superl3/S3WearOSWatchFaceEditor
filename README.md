# Photo2WFF

Photo2WFF is a deterministic first-pass reconstruction pipeline for minimalist 438×438 Wear OS watch faces.

```text
reference.png → analyzer → scene.json → WFF XML → preview.png → compare → patches.json
```

The canonical editable representation is `scene.json`. Generated Android resources under `project/` are compilation output and can be regenerated at any time.

The Vision Analyzer contract emits a strict scene with `normalization`, `background`, `elements`, and `analysis` sections. Elements use the fixed taxonomy from the analyzer role, including `TIME`, `HOUR`, `MINUTE`, `SECOND`, `DATE`, `WEEKDAY`, dynamic data fields, and `STATIC_IMAGE` for conservative raster fallback. Each inferred element carries numeric geometry, confidence, and uncertainty metadata where needed.

## Quick start

Requirements:

- Python 3.11+
- Pillow (`python -m pip install -e .`)
- Android SDK with `platforms;android-34` and build-tools
- Java 17+ for the generated WFF project

From this directory:

```powershell
python -m pip install -e .
python -m photo2wff quick path\to\reference.png
python -m photo2wff refine output
python -m photo2wff render output
```

The implementation order is explicit:

1. `schemas/scene.schema.json` defines the versioned scene contract.
2. `templates/wff-minimal/` contains the smallest resource-only WFF project shape.
3. `photo2wff build samples/minimal.scene.json --out sample-output` validates and compiles a fixed scene without Vision Analyzer.
4. The generated project is built with Gradle and its WFF XML is checked with the official validator.
5. `photo2wff quick reference.png` is the later analyzer-connected path.

For a frontal product photograph, use the conservative analog fallback:

```powershell
python -m photo2wff product-photo path\to\product-photo.webp --out product-photo-output
```

This crops the dark display region, removes remaining bezel fragments, normalizes it to 438×438, and emits a `MINIMAL_ANALOG` scene. The A1a path keeps the cleaned dial static and emits native `AnalogClock` hand elements with adjustable geometry metadata. Hand detection is conservative and explicitly marked for A1b refinement.

For the A1a functional analog vertical slice:

```powershell
python -m photo2wff product-photo path\to\product-photo.webp --out hermes-a1-output
python -m photo2wff render-wff hermes-a1-output\project\watchface\src\main\res\raw\watchface.xml --out hermes-a1-output\wff-rendered.png --time 10:08:30
```

`render-wff` reads the compiled WFF XML and its drawable resources, then renders the fixed `PREVIEW_TIME` independently of the scene preview. It is a deterministic format-level renderer for CI; a Wear OS device/emulator screenshot is a separate verification tier.

`quick` creates:

```text
output/
  reference.png
  scene.json
  scene.initial.json
  editable.yaml
  preview_initial.png
  preview.png
  comparison.json
  patches.json
  assets/
  project/
```

For the fixed-scene milestone:

```powershell
python -m photo2wff build samples\minimal.scene.json --out sample-output
```

This command intentionally does not inspect an image. It proves the schema → parser → compiler → WFF template → preview → APK path before Vision Analyzer integration.

The initial milestone is intentionally narrow: a circular 438×438 canvas, simple backgrounds, large digital time, secondary text, and static image fallback. Low-confidence regions are preserved as raster assets instead of being guessed into invalid WFF.

## Canonical fixture

The repository includes a deterministic fixture generator and a generated first output under `fixtures/` and `demo-output/`. The fixture represents a black face with a Pretendard-like large time and secondary date/weekday labels. It is a pipeline test asset, not a replacement for the user's reference image.

```powershell
python scripts\make_canonical.py
python -m photo2wff quick fixtures\canonical.png --out demo-output
```

## WFF compatibility

The generated project uses Watch Face Format version 1, minSdk 33, and compile/target SDK 34. That keeps the first slice compatible with Wear OS 4+ while using only version-1 data sources. Galaxy Watch9's 40mm target is 438×438; the attached product specification deliberately fixes the compiler canvas to that size. A 44mm target must be added as a separate 480×480 scene variant rather than silently scaling the design.

The project is resource-only (`android:hasCode="false"`). Dynamic time, date, weekday, battery, steps, and heart-rate fields are emitted as native WFF expressions when present. Static or unsupported visual content is emitted as image resources.
