# CMEMS Ocean-Waves Pipeline

This producer downloads the Copernicus Marine global wave analysis and
forecast, validates its scientific metadata, and builds a schema-v2 immutable
bundle. It cannot publish releases. A separate isolated publish job revalidates
the sealed bundle before exposing its manifest.

## Timing and freshness contract

- Source dataset: `cmems_mod_glo_wav_anfc_0.083deg_PT3H-i`
- Variables: `VHM0` in metres and `VMDR` in degrees with their exact expected
  CF standard names
- Window: 17 snapshots at three-hour cadence (`T+0` through `T+48`)
- Schedule: `07:00` and `19:00` UTC, exactly 12 hours apart
- Maximum source age: 15 hours, comprising the 12-hour publish interval plus
  one exact three-hour native-cadence margin

The publisher and client both fail closed after that 15-hour boundary. The
margin prevents a routine outage while the next twice-daily run is processing;
it does not extend forecast coverage or permit a stale dataset whose 48-hour
window no longer covers the current time.

## Encoding and trust separation

The producer performs a height-weighted circular mean for direction and retains
the arithmetic mean `VHM0` magnitude. Near-cancelling directions use the
deterministic direction of the tallest native system, with the lowest normalised
bearing breaking exact ties. It validates finite masks, global coverage,
dimensions, cadence, CF axes, physical units, payload length, and mask invariance
before creating `manifest.draft.json`.

### Finite-mask contract for the height/direction pair

`VHM0`'s finite mask is the ocean/land truth: it must be identical for every
forecast step, it becomes the encoded land mask, and any cell losing its value
fails the run. `VMDR` is validated as a _bounded superset_ of that mask: the
upstream product defines mean direction on a one-cell fringe outside `VHM0`'s
wet mask (coastlines plus the seasonal sea-ice edge) which the encoder never
reads — the height-weighted projection multiplies each direction by `VHM0`
(NaN there) and the land mask zeroes those cells. Direction must never be
missing where height is finite, and the fringe is capped at 2% of the grid so
an upstream regrid or genuine mask change still fails closed.

Measured against the live product (2026-06-01 through 2026-08-26, ARCO
mirror): 53,093-58,099 fringe cells (0.60-0.66% of the grid) with mid-latitude
counts identical across months and polar counts tracking the ice edge; zero
cells where height was finite but direction missing, on every date sampled.
This mismatch predates the 2026-08-06 contract introduction — the original
all-variables-identical invariant had never held for this product, which is
why every run from 2026-08-06 to 2026-08-26 failed at `waves:VMDR:step0`.

The generation job has Copernicus credentials and read-only repository access.
The publish job has repository write permission but no source credentials or
producer dependencies. It binds the sealed draft's commit and run ID to the
workflow context and accepts an earlier producer attempt only when a failed
publisher is rerun in that same workflow run. Immutable assets are written to
the deterministic UTC ISO-week release shard. After remote size and SHA-256
verification, the publisher updates only the inactive dual v2 manifest slot; a
first publication seeds both slots. Run-stable artifacts are retained for one
day: rerun only the failed publisher inside that window, or rerun all jobs after
it.

## Attribution

The visible layer must retain Copernicus Marine Service attribution and product
DOI `10.48670/moi-00017`.
