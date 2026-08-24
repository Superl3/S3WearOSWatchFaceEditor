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

The A1a.1 validation renders `10:08:30`, `03:15:45`, `06:30:00`, and `09:45:15`. It checks pairwise changes inside dynamic hand ROIs and scans the original source-angle corridors for persistent hand ghosts. Global MAE is not sufficient for this gate. The compiler and renderer are frozen at the A1a implementation; A1b is limited to reference-to-`dial_clean`/hand-assets/pivot-angle extraction and delegates WFF generation to the frozen pipeline.

For A1c display geometry review, the same product-photo command also emits:

```powershell
python -m photo2wff product-photo path\to\product-photo.webp --out hermes-a1c-output
python -m photo2wff human-review hermes-a1c-output
```

`scene.json` records a source `RoundedRect(width, height, radius)` and a target circle represented by the same primitive (`width == height`, `radius == width / 2`). The review artifacts are written under `human-review/`:

```text
human-review/
  atlas.png                    # 9 fixed WFF render times
  geometry-overlay.png         # source/target boundaries, centers, rays
  asset-sheet.png              # dial_clean and extracted transparent assets
  mapping-comparison.png       # source, naive XY, adaptive boundary-normalized
  manifest.json                # structured mappings and review metadata
```

Structured elements map anchors, pivots, and geometry while preserving local appearance. Only static raster artwork uses inverse raster mapping. The A1c comparison keeps the legacy XY mapper beside the center-preserving boundary-normalized mapper; device/emulator runtime capture remains a separate deferred verification tier.

For A1d occlusion reconstruction and dial completion, the same command produces a reusable `Occlusion Reconstruction Engine` artifact set:

```powershell
python -m photo2wff product-photo path\to\product-photo.webp --out hermes-a1d-output
python -m photo2wff product-photo path\to\product-photo.webp --out hermes-a1d-fallback-output --generative-fallback path\to\inpaint-candidate.png
```

The default path restores deterministic background/stroke candidates and leaves uncertain pixels unresolved. The optional fallback is applied only inside the hand occlusion mask and is always marked as generated rather than observed. A1d writes `observed-mask.png`, `hand-occlusion-mask.png`, `reconstructed-mask.png`, `dial-before-reconstruction.png`, `dial-completed.png`, `unresolved-mask.png`, and `occlusion-metadata.json`. Its human-review bundle additionally contains `hands-off.png`, `occlusion-zoom-sheet.png`, `before-mask-reconstructed-final.png`, `reconstructed-highlight.png`, the normal 9-time atlas, and an adversarial `occlusion-reveal-atlas.png`. Uncertain regions keep confidence and `requiresHumanReview` metadata.

For A2 Dynamic Date Window, the analyzer treats a rounded window replacing the omitted 3 o'clock index as a high-confidence `DATE_DAY_OF_MONTH` candidate. The missing hour numeral is intentionally not reconstructed. The static frame, border, and background are preserved while only the observed glyph is removed into `assets/dial_empty_date.png`; the compiler emits a minimal `DYNAMIC_SLOT` abstraction:

```powershell
python -m photo2wff product-photo path\\to\\product-photo.webp --out hermes-a2-output
python -m photo2wff render-wff hermes-a2-output\\project\\watchface\\src\\main\\res\\raw\\watchface.xml --out hermes-a2-output\\wff-rendered.png --date 2024-08-20
```

This milestone supports only `slotType: DATE_DAY_OF_MONTH` (`d` or `dd`). The review bundle contains renders for days 1, 8, 11, 20, and 31 plus centered geometry guides and padding/clipping metrics under `human-review/date-window/`. Complications, weather, battery, weekday, and other dynamic semantics remain out of scope.

Production stops automatic glyph synthesis after A2. To use a supplied glyph,
place only the desired files (`0.png` through `9.png`) in a directory and pass
it to the product-photo command:

```powershell
python -m photo2wff product-photo path\to\product-photo.webp --out manual-glyph-output --manual-glyph-dir path\to\manual-glyphs
```

The PNGs are copied at their original resolution. WFF has no character-level
BitmapFont fallback: the date uses BitmapFont only when every digit in the
current value is supplied, and otherwise renders the entire value with
Pretendard. No automatic glyph
extraction, synthesis, style fitting, or topology reconstruction is performed
on production `main`.

To freeze a verified A2 bundle into the production analog structure, build it,
activate it on Wear OS, and create the review bundle in one command:

```powershell
python -m photo2wff production-port hermes-a2-output `
  --out hermes-production-port `
  --adb C:\Users\bug95\Android\Sdk\platform-tools\adb.exe `
  --serial emulator-5556
```

The output contains only `dial_complete.png`, three canonical hand assets,
`center_cap.png`, and a native dynamic date. Static artwork stays rasterized;
the compiler emits the verified `AnalogClock` z-order. Optional manual digits
can be supplied with `--manual-glyph-dir`; partial sets keep the existing
whole-value Pretendard fallback. The command requires official WFF validation,
runs `assembleDebug`, captures 9 active-runtime times and days
`1 / 8 / 11 / 20 / 31`, and writes static-detail, runtime, date, and final
preview review sheets.

A2.5 runtime validation is a separate gate and does not alter the production
renderer. It renders the fixed matrix of four times (`00:00:00`, `03:15:45`,
`06:30:00`, `10:08:30`), five dates (`1`, `8`, `11`, `20`, `31`), and manual
override on/off, then compares runtime captures by static dial, hand, date,
and global regions. Run it with:

```powershell
python -m photo2wff runtime-gate path\to\scene.json path\to\manual-off\watchface.xml --manual-xml path\to\manual-on\watchface.xml --out runtime-validation
```

To capture the active Wear OS runtime (not the picker preview), provide both
debug APKs and the Wear OS device serial:

```powershell
python -m photo2wff runtime-gate path\to\scene.json path\to\manual-off\watchface.xml --manual-xml path\to\manual-on\watchface.xml --out runtime-validation --capture --apk-off path\to\manual-off.apk --apk-on path\to\manual-on.apk --serial emulator-5556
```

The capture path clean-installs each APK, activates it through
`DEBUG_SURFACE`, sets each fixed time/date, and records a screenshot only when
the active runtime is confirmed. The report also saves aligned, hand-ROI,
date-window, static-dial, side-by-side, heatmap, and runtime-only atlas
artifacts.

The gate never labels a phone emulator as Wear OS and reports
`blocked_by_runtime_environment` when no Wear OS runtime is available.

A2.5c is retained as `partial_with_invalid_metrics` and tagged
`experimental/a25c-invalid-metrics`. Its production-screen MAE, constrained
hand detector, hardcoded pivot error, and framebuffer-derived transform must
not drive production geometry changes.

A2.5c.1 uses separate diagnostic WFFs for fiducial geometry, static hand asset
geometry, dynamic hand angles, and isolated text. It does not use global MAE as
an acceptance gate:

```powershell
python -m photo2wff measurement-correctness hermes-a2-output\scene.json `
  --manual-scene path\to\manual-output\scene.json `
  --out a25c1-measurement-correctness --capture --serial emulator-5556
```

`1ee56a8` is preserved as `baseline/a25c1-measurement-correctness`. A2.5c.2
isolates BitmapFont layout, solid-raster resampling, and analog pivot behavior
without changing production scene geometry:

```powershell
python -m photo2wff runtime-rendering-calibration hermes-a2-output\scene.json `
  --manual-scene C:\path\to\manual-output\scene.json `
  --out a25c2-runtime-rendering-calibration --capture --serial emulator-5556
```

The generated `device-runtime-calibration.json` contains only values measured
by dedicated runtime diagnostics. The geometry-only predicted preview applies
the verified logical-to-runtime affine transform; it does not claim an image
filter kernel while that kernel remains unmeasured. BitmapFont preview glyphs
are scaled from `BitmapFont size` and `Character` metrics before centering.

The runtime capture records `t_before -> screencap -> t_after` in one remote
device command and evaluates moving hands against the resulting angle interval.
Pivot error remains `unmeasured` unless it can be independently detected.

Google's official WFF validator is mandatory for compilation and tests. Build
the pinned validator once before development or CI validation:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup_wff_validator.ps1
```

If the validator is absent or cannot execute, WFF validation fails closed.
`lint_wff_xml` is only a Photo2WFF structural lint and is never reported as
official WFF validation.

## Generic rounded-rectangle perimeter analog support

Run the target-independent fixture gate before benchmarking a product image:

```powershell
photo2wff perimeter-fixtures --out perimeter-generic-fixtures
photo2wff perimeter-benchmark reference.webp --out perimeter-benchmark --serial emulator-5556
```

The benchmark command refuses to analyze the reference until the synthetic
gate passes. It emits both `RASTER_WARP` and `ELEMENT_PRESERVING` projects,
keeps unknown perimeter content as `STATIC_ARTWORK`, extracts central weekday
and day-of-month candidates, and reuses the existing analog-hand pipeline.
Structural/runtime success and visual benchmark acceptance are reported
separately; visual acceptance remains human-reviewed.

The legacy A2.5c command can still reproduce the invalidated research artifact:

```powershell
python -m photo2wff runtime-calibration path\to\scene.json path\to\manual-off\watchface.xml runtime-validation --manual-xml path\to\manual-on\watchface.xml --out runtime-calibration
```

It records the 454×454-to-438×438 transform, inverse-normalized screenshots,
timestamp-aware hand expectations, resampling candidates, and separate date
window/font measurements.

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

## Wear OS emulator setup

`scripts/wear_runtime_setup.ps1` boots the Wear OS AVD, verifies the `watch` device characteristic, clean-installs a WFF APK, activates it through the official `DEBUG_SURFACE` broadcast, opens the real watch-face picker, taps the face, captures its UI, and writes `a25-runtime-validation/wear-runtime-setup.json`. It treats `WatchFaceId[..., null]` as the normal resource-only WFF form and records picker/runtime activation separately.

```powershell
powershell -ExecutionPolicy Bypass -File scripts/wear_runtime_setup.ps1 -ApkPath C:\path\to\watchface-debug.apk -NoWindow -CapturePicker -CleanInstall -Activate
```
