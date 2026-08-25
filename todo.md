# Photo2WFF TODO

## Current state

- Production analog pipeline A0~A2 and runtime measurement infrastructure are preserved.
- Display ROI Confirmation Gate is implemented. Normalization and perimeter benchmark execution are blocked until a confirmed ROI metadata file is supplied.
- Generic RoundedRect perimeter fixtures pass, including disconnected multi-component markers, `(s,d)` geometry mapping, local-similarity placement, anchor/clipping checks, and fold-over checks.
- Production perimeter mapping defaults to `HYBRID_PERIMETER_MAPPING`:
  - `PERIMETER_SD_WARP` for continuous perimeter artwork
  - `LOCAL_SIMILARITY` for compact markers and numeral-like artwork
- Existing hand, date, WFF compiler, and runtime code paths must remain regression-free.

## Next tasks

### 1. Confirm the real display ROI

- Open the ROI editor for the actual source image.
- Verify that the entire display artwork is inside the ROI and that the device frame is outside it.
- Pay special attention to the 3 o'clock date window and the top/bottom markers.
- Approve the edited values and save `display-roi.json`.

```powershell
python -m photo2wff display-roi-edit <roi-review> --open
```

### 2. Re-run the perimeter benchmark with the confirmed ROI

```powershell
python -m photo2wff perimeter-benchmark <reference-image> `
  --out <benchmark-output> `
  --display-roi <roi-review>\display-roi.json
```

Acceptance checks:

- 12 perimeter marker slots detected
- anchor residual <= 0.5 px
- pixel retention >= 99%
- unintended clipping = 0
- no fold-over or wave-like curvature artifacts
- `10`, `11`, `12` remain intact as composite markers
- top and bottom markers are not cropped

### 3. Human review of mapping candidates

Review these artifacts side by side:

- confirmed source ROI
- radial baseline
- perimeter `(s,d)` warp
- local-similarity result
- hybrid result
- perimeter unwrap and grouping overlay
- hands-off cleaned dial
- runtime 9-time atlas, when a Wear OS runtime is available

Do not add target-reference coordinates or sample-specific mapper logic. If a marker needs adjustment, record it as reversible metadata or a human boundary review.

### 4. Runtime verification

- Run official WFF validation and `assembleDebug` for the final benchmark output.
- Install and activate the APK on the Wear OS emulator/device.
- Capture the active watch-face surface, not the picker preview.
- Keep runtime-specific differences separate from logical scene geometry.
- Report unavailable runtime verification as `blocked_by_runtime_environment`.

### 5. Regression gate before the next milestone

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
python -m compileall -q photo2wff tests
git diff --check
```

Do not proceed to detail refinement until the confirmed-ROI benchmark, official validator, build, and runtime review are complete or explicitly deferred.

## Scope guard

- Do not resume automatic glyph synthesis or font reconstruction.
- Do not change the validated hand/date/WFF/runtime pipeline for a perimeter-only visual issue.
- Do not bake 454 px runtime calibration into the logical 438 px scene.
- Do not treat a benchmark with an unconfirmed or clipped ROI as a production pass.
