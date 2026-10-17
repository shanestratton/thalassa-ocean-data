# Held atmospheric scalar publisher

This source adds GFS gust, model visibility, pressure, temperature and dewpoint,
and ECMWF IFS pressure. It does not activate a publisher or replace the existing
seven CMEMS/MPA contracts. There is no new schedule. Provider reads and release
publication independently default **false** in `atmos-held-pipeline.yml`.

## Fixed source and display contracts

Both sources use original 0.25° fields, sampled at exact 1° grid nodes for display.
This is explicitly labelled coarse display data, not a new native forecast or
the paid ECMWF 9 km service. The original GRIB run, valid time, centre, parameter,
fixed level, units, regular grid and scan directions must match the fixed source
path and index. Wrong units do not trigger a guessed conversion. The selected
provider hosts cannot redirect, and range reads require exact HTTP 206,
Content-Range and Content-Length. No full-file download fallback exists.

- GFS: 29 six-hour snapshots from h000 to h168. Original GUST/VIS at surface,
  PRMSL at mean sea level, TMP/DPT at 2 m above ground. Source path explicitly
  uses NOAA's public 0.25° bucket. Four eligible UTC cycles, maximum run age 12 h.
- IFS: 41 six-hour snapshots h000 to h240. Original `msl`, `oper`, `fc`, surface.
  Only 00/12 UTC are eligible. Maximum run age 24 h. An AIFS/ensemble or 06/18 UTC
  source cannot silently supply this contract.

Each independent THAS v1 frame has a 64-byte little-endian header and one float32
plane plus byte mask per fixed field. Coordinates are 360 × 181 cell centres,
west −180°, south −90°, dx/dy 1°, rows south to north. Periodic longitude does not
repeat the eastern endpoint. Missing cells remain zero/masked, never interpolated
across by the shared app renderer. At least 95% of each field must be valid;
remaining gaps stay explicitly unknown. A complete frame cohort is required.
The output units are m/s, m, Pa and K. Visibility is **model visibility, low skill**;
T−Td is only an indicative saturation proxy, not a fog probability or observed
visibility. No routing, tide, watch, clearance or safety authority is created.

## Bounds and publication ordering

Generation requires an explicit eligible UTC run, source provenance and fresh
output directory. The CLI refuses provider access without
`--allow-provider-downloads`. The manual workflow additionally requires exact
master and its separate download input before generation; publication needs
another explicit input. Untrusted input is passed through environment variables,
never inserted into shell command text.

Per generation: 20-minute monotonic deadline, at most 210 requests, 1 MiB per
index, 6 MiB per original GRIB field, 512 MiB total provider bytes, sequential
bounded field decode. A GFS output frame is 1,629,064 bytes and IFS 325,864 bytes.
The raw output plus sealed bundle is approximately 94.5 MB GFS / 26.7 MB IFS.
No previous partial or immutable file is overwritten. The existing release writer
revalidates the sealed complete bundle, hashes, source clocks, producer identity
and remote bytes before updating an inactive discovery slot last.

The unchanged 900-assets-per-ISO-week guard leaves room below GitHub's 1,000:
GFS 29 × 4 × 7 = 812; IFS 41 × 2 × 7 = 574. Additional generations/reruns still
consume that same budget and must refuse once full. Nothing deletes old assets.
The actual long-term storage/network/Actions budget needs review before enabling.

## Evidence and held checks

Local offline proof: 15 new atmospheric tests and nine release source gates pass.
This includes actual complete synthetic THAS bundle sealing, original-unit/level/
clock/coordinate checks, exact HTTP range and budget refusals, model separation,
and unchanged legacy admission. The original full suite was also attempted:
66/68 original checks passed locally, with the raw two environment failures kept
(`xarray` absent and Windows symlink privilege), not waived. The parent separately
proved all original 68 on Linux in its SSH packet.

`scripts/atmos-native-tests` adds three **actual ecCodes** tests using locally
generated GRIB samples and no provider I/O. These have not yet run locally because
that native dependency closure is absent. The held manual proof job installs eight
exact Python 3.11.15/Linux x86_64 wheel hashes from versioned PyPI metadata, then
runs these tests with a dead proxy. Hash pins have been recorded; successful Linux
wheel installation and native decoding are still required evidence.

Before any enablement: review/merge the source, run the genuine isolated Linux
proof, make one reviewed bounded real provider range/CLI generation, compare
original metadata/known values, inspect immutable release bytes/manifests and
hosted API readbacks, then make a separate activation decision. No provider GRIB
or image data has been downloaded by the source work.

## Primary references checked 17 October 2026

- [NOAA field inventory](https://www.nco.ncep.noaa.gov/pmb/products/gfs/gfs.t00z.pgrb2.0p25.f000.shtml)
  gives original parameters, levels and units.
- [NOAA GFS public bucket](https://registry.opendata.aws/noaa-gfs-bdp-pds/) is the
  fixed public source, rather than a third-party weather API.
- [NOAA PNS26-30](https://www.weather.gov/media/notification/pdf_2026/pns26-30_CommentsGFSv17_ProductRemoval.pdf)
  proposes retirement of coarse 0.5°/1° files/filters, motivating the retained
  0.25° source path.
- [ECMWF current open-data catalogue](https://www.ecmwf.int/en/forecasts/datasets/open-data)
  supplies the 0.25° licence/cycle/parameter contract. Its current catalogue takes
  precedence over stale stream/horizon tables in older client README text.
- [Official ECMWF client](https://github.com/ecmwf/ecmwf-opendata) documents the
  source selection and indexed subset access. This producer independently checks
  the bytes and does not depend on an implicit-latest request.
