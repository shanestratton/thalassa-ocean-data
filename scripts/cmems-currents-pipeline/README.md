# CMEMS Ocean-Currents Pipeline

This producer downloads the Copernicus Marine hourly merged surface-current
forecast and builds a validated schema-v2 artifact. It cannot publish releases.
The separate isolated publish job receives the artifact, revalidates it, and
updates the inactive dual v2 discovery slot only after every immutable asset
verifies.

## Source and output contract

- Product: `GLOBAL_ANALYSISFORECAST_PHY_001_024`
- Dataset: `cmems_mod_glo_phy_anfc_merged-uv_PT1H-i`
- Variables: `uo` / `vo`, exactly `m s-1` with their expected CF standard names
- Native grid: global 1/12 degree; area-averaged to approximately 0.25 degree
- Window: exactly 13 hourly snapshots (`T+0` through `T+12`)
- Payload: v2 `THCU`, little-endian float32 `u`, float32 `v`, then a 0/1 land mask

The producer validates source identity, CF axes and units, cadence, coverage,
finite masks, physical ranges, encoded dimensions, payload length, and mask
invariance before creating `manifest.draft.json`. Every binary filename embeds
its generation, step, and a digest-derived generation suffix.

## CI and trust separation

`.github/workflows/cmems-currents-pipeline.yml` runs every six hours. A 12-hour
forecast cannot safely be refreshed only once per day; the six-hour interval
also leaves room for a delayed run. The generation job has Copernicus credentials
and read-only repository permission. Dependencies install from the shared
Python 3.11.15 hash lock. Contract fixtures run first behind a dead-proxy
network fence; the later credential-bearing producer step has network access
because it must obtain source data from Copernicus.

The publish job has repository write permission but no Copernicus credentials or
producer dependencies. It binds the sealed draft's commit and run ID to the exact
workflow context and accepts an earlier producer attempt only when a failed
publish job is rerun in that same workflow run. It uploads generation-named
assets without clobbering to the deterministic UTC ISO-week shard, verifies
remote sizes and SHA-256 hashes, and updates only the inactive dual v2 manifest
slot. A first publication seeds both slots. Run-stable artifacts are retained for
one day: rerun only the failed publisher inside that window, or rerun all jobs
after it. Weekly capacity is bounded at 364 current assets, below the conservative
900-asset shard guard.

## Running the producer

Use Python 3.11.15 and install the repository lock with hashes:

```bash
python -m pip install --require-hashes --only-binary=:all: -r ../cmems-requirements.lock
export COPERNICUS_MARINE_USERNAME=...
export COPERNICUS_MARINE_PASSWORD=...
export GITHUB_SHA=0123456789abcdef0123456789abcdef01234567
export GITHUB_RUN_ID=1
export GITHUB_RUN_ATTEMPT=1
python pipeline.py
```

Local output defaults to `/tmp/cmems-currents/bundle`. Publishing is intentionally
not a local producer responsibility.

## Attribution

The visible layer must retain Copernicus Marine Service attribution and the
product DOI `10.48670/moi-00016`.
