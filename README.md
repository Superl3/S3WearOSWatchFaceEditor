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

The PNGs are copied at their original resolution. Supplied digits are used by
the date slot; absent digits use the Pretendard fallback. No automatic glyph
extraction, synthesis, style fitting, or topology reconstruction is performed
on production `main`.

A2.5 runtime validation is a separate gate and does not alter the production
renderer. It renders the fixed matrix of four times (`00:00:00`, `03:15:45`,
`06:30:00`, `10:08:30`), five dates (`1`, `8`, `11`, `20`, `31`), and manual
override on/off, then compares runtime captures by static dial, hand, date,
and global regions. Run it with:

```powershell
python -m photo2wff runtime-gate path\to\scene.json path\to\manual-off\watchface.xml --manual-xml path\to\manual-on\watchface.xml --out runtime-validation
```

The gate never labels a phone emulator as Wear OS and reports
`blocked_by_runtime_environment` when no Wear OS runtime is available.

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
