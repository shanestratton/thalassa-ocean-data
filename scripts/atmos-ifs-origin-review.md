# IFS original longitude-origin repair

The actual four-read IFS diagnostic `39011834081` stopped after its first two
requests: one HTTP 200 index and one exact HTTP 206 pressure range. The preserved
525,958-byte original has SHA256
`599ae5df95354ad5b2070bb853762ff7c8111d6dec0acb25d9af5c705e4e696e`.
Its original ecCodes metadata declares first longitude 180 degrees and last
longitude 179.75 degrees. The prior admission incorrectly required the GFS
0/359.75-degree endpoints for both models. That failed observation remains failed;
it never established decoded IFS availability, a four-read success or a cohort.

## Exact geography and unchanged gates

[ECMWF's geography keys](https://codes.ecmwf.int/grib/format/edition-independent/1/)
define the original first/last points and longitude increment. Its
[GRIB key documentation](https://confluence.ecmwf.int/spaces/ECC/pages/188042102/GRIB+Keys)
recommends degree-valued keys and identifies the computed scanning flags; the
[scanning table](https://codes.ecmwf.int/grib/format/grib2/ctables/3/4/) distinguishes
eastward rows, consecutive longitude points and row direction. These describe
how to interpret declared original metadata; the captured field supplies the
specific IFS endpoint evidence.

Admission now binds **GFS to exactly 0/359.75 degrees** and **IFS to exactly
180/179.75 degrees**. It does not admit arbitrary origins, alternate increments,
reversed/alternating rows, rotated/reduced grids or assumed endpoints. All original
centre, field/level, unit, 1440 by 721 cardinality, 0.25-degree increment, north-to-south
row, cycle/valid-time and bitmap gates remain before native value expansion.
Physical bounds, exact output cardinality and the original 95% mask gate remain.

For an admitted eastward row, the native column for target longitude `lon` is
`((lon - original_first_longitude) % 360) / 0.25`. Both the full sampler and the
separate indexed original-node probe use the declared admitted origin. GFS
sampling is mathematically unchanged. For IFS, target -180 degrees maps to native
column 0, Greenwich to 720 and 179 degrees to 1436; the prior GFS formula would
incorrectly shift those values by half the world. Sampling remains exact original
nodes, without interpolation, unit inference or geographic reprojection.

## Fixtures and held proof

Three new source cases use distinct hard-coded native positions across the
dateline, Greenwich and hemispheres, preserve bitmap unknowns, check the independent
indexed calls and refuse wrong endpoints before expansion. All original 117 source
cases remain; the complete source inventory is now **120**. The original five
native test bodies/assertions remain. Their IFS sample helper now encodes the
factual IFS endpoints instead of a fictional Greenwich-origin IFS grid.

Two additional native cases make **seven** mandatory ecCodes tests: a nonuniform
IFS round trip with explicit original positions and a replay of the unmodified
captured pressure field. The replay fixture records the original source URL,
Range, cycle, first failed observation and original refused metadata in its JSON
sidecar. It checks exact bytes/hash and metadata, indexed node identities, decoder
values/masks and one standalone THAS diagnostic frame. It has no provider read or
currentness claim. The ECMWF open-data attribution and CC-BY-4.0/ECMWF Terms of Use
remain explicit in that sidecar and the existing output metadata.

Local standard-library atmosphere tests pass 33 cases. Windows has no ecCodes
runtime or xarray installed; no packages or privileges were changed. The genuine
full 120-case source and seven-case native run must still execute in the two
existing separate hash-locked Linux dependency closures, under dead proxies, with
zero skips and actual interpreter exits. Preserve the historical mixed-native
exit 134 and the first metadata refusal. No provider read, complete-cohort
publication, workflow dispatch, schedule, flag, app activation or deployment is
performed by this repair packet. The held production generator and daily SSH
remain held/unchanged except for the same corrected original sampler.
