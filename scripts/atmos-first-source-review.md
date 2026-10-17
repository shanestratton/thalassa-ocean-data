# Atmosphere 141 first-original diagnostic — source review

Provider reads are held. This is a preparatory source packet, not a forecast
publication or activation. The existing held full-cohort generator, native tests,
hash locks, old seven publishers and daily SSH remain in place. No provider reads,
pushes, schedules, app flags, secrets or publication were changed by this packet.

## Admission ordering

`atmos_pipeline.py` now extracts the original metadata guard into
`admit_original_metadata` and checks it **before** `codes_get_values`. This keeps
the same model/centre, 0.25° 1440×721 grid, orientation, field, level, units, cycle,
valid time and bitmap rules. `sample_original` independently rechecks those rules
and retains exact value-cardinality, physical bounds and 95% original mask gates.
`read_original_metadata` reads the same fixed scalar keys without expanding values.

The mocked-native ordering tests prove 18 bad metadata variants cause zero value
expansion and one handle release. They also prove positive sampling, cardinality,
physical-range refusal and cleanup on metadata-read failure. These are ordering
tests, not a claim that fictional GRIB bytes are real provider/native observations.
The original three actual ecCodes fixtures remain unchanged and mandatory in
their separate atmospheric Linux dependency closure. Two additional native cases
compare indexed original nodes with the full decoder at all four diagnostic model
endpoints and verify the exact bitmap-unknown node and remaining known nodes.

## Mandatory offline proof before any original read

The original source tripwire keeps its 72 baseline tests, 15 SSH tests and 15 original
atmosphere tests. Four new admission methods and eight one-shot methods are added
explicitly: 115 source cases total, with all original named assertions retained.
The native closure retains those three original actual ecCodes tests and adds the
two indexed-node cases: five native tests, zero skips, and a mandatory process exit 0.

Preserve the earlier separate-process pattern from
`.github/workflows/check-atmos141-offline.yml`: one Linux 3.11.15 process installs
only `scripts/cmems-requirements.lock` and runs the complete source suite; another
installs only `scripts/atmos-requirements.lock` and runs the native suite. Both
use reviewed hash-locked wheels, dead HTTP/HTTPS/ALL proxies, actual package and
loaded-library evidence, exact source hashes and real interpreter exit status.
The historical mixed native C double-free/exit 134 remains failed evidence; it
does not justify a skip, suppressed exit or deleting old tests.

Local Windows validation can run the focused standard-library tests. A full
attempt is retained at `work/validation141-atmos-preflight-all-first.log`: Windows
symlink privilege and absent xarray prevent the genuine whole source proof there.
No privilege change or global dependency installation was attempted. Root must
review and run the new isolated Linux proof before approving the first real read.

## Reviewed one-shot shape

`prove_atmos_first_source.py` refuses by default, requires a fresh output directory
and explicit cycles, and has no publisher/cohort-builder/latest-run lane. The
proposed cycles are GFS 2026-10-17T00:00Z and IFS 2026-10-16T12:00Z. They must be
rechecked for currentness immediately before actual execution; they are not a
standing permission to substitute a later cycle automatically. Existing model
max-age gates remain 12/24 hours. The diagnostic requires GFS 00 plus the preceding
IFS 12 cycle, each as its own original model/time identity.

| Source | Original offsets | Reads per offset | Total |
| --- | --- | --- | ---: |
| GFS 0.25° |0,168h| one index + gust/visibility/MSLP/2m temperature/2m dewpoint |12|
| IFS oper 0.25° |0,240h| one index + MSLP |4|

The exact original source selectors/range/status/content-length/content-encoding/
redirect guards are reused from `DownloadBudget`, `gfs_ranges` and `ifs_range`.
Only fixed HTTPS public NOAA/ECMWF S3 hosts are allowed; no query, authentication,
redirect, alternate model or fallback provider is accepted.

The narrower diagnostic limits are 16 reads, 80 MiB admitted bodies, 96 MiB entire
output, 600 s lifetime, 20 s reads and 6 MiB per original field. Linux `RLIMIT_AS` holds
the native process to 2 GiB; a SIGALRM holds 600 s and is cleared on exit. Native
dependencies must import successfully before any provider read. A process with
CMEMS/NetCDF/HDF5/rasterio already imported is refused rather than mixing the
earlier crashing dependency closures. Failure receipts may finish after the read
deadline, within the fixed directory limit; they never authorize another read.

On success, all 16 original response bodies are retained without modification in
`originals/`, together with original request/response URLs, headers, exact Range,
status, byte count and SHA256. Each of 12 GRIB fields gets original native scalar
metadata and four independent exact-node value/mask queries. The indexed Python
API is documented by [ECMWF ecCodes](https://sites.ecmwf.int/docs/eccodes/namespaceec_codes.html).
Only admitted metadata may precede indexed or full value expansion. Original
double samples are compared with the decoder's exact float32 representation and
mask at southern/northern, Greenwich/dateline and 153°E/27°S nodes. No unit guessing
or interpolation is used. Original masked/nonfinite values remain unknown.

Four standalone THAS diagnostic frames are written and validated through the
existing real binary writer/validator, preserving model, run, valid offset,
fields, units, masks, original attribution and licence. They are deliberately
incomplete 29/41-frame cohorts. No generation manifest or publishable bundle is
created, and `completeCohort`/`publication` remain false even on diagnostic success.

A refusal stops at the first error and preserves only what was actually admitted.
For example, a rejected HTTP status retains its headers and an empty/unread body
prefix; truncation retains the admitted raw prefix. It does not invent unobserved
bodies or retry another cycle. No HTTP error or partial result is reported as
the full 16-body success. Metadata refusals retain original scalar metadata when
available. All output files are created exclusively; no deletion/overwrite is
used to fit a cap or conceal an earlier failure.

After genuine Linux proofs, root may review a separately source-pinned one-shot
read of this shape. No actual command was run with provider consent in this packet.
Even a successful source diagnostic would establish neither a complete forecast
release nor world forecast availability, navigational accuracy or app activation.

## First real observation and corrected index path

Run 39007145855 preserved ten original GFS fields and two independently validated
diagnostic frames at offsets 0 and 168 hours. Its thirteenth request correctly
refused an ECMWF HTTP 404: the code appended `.index` to `.grib2`, rather than
replacing the extension. All response headers and admitted original bytes remain
in `work/atmos141-first-real-source-failed`; no complete result is claimed.

Both the diagnostic and held full generator now replace that extension, following
[ECMWF's original file naming documentation](https://confluence.ecmwf.int/pages/viewpage.action?pageId=327648995).
The fictional diagnostic transport now refuses the wrong suffix and asserts both
exact original index URLs. One new case independently stops the full generator
at its first index request and checks the exact provider path before any field
read. The source tripwire admits this deliberate addition: 115 source cases;
the five actual native cases and all scientific admission rules remain unchanged.
The URL repair requires a new isolated proof before a second bounded real read.
