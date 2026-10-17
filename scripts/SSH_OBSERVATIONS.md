# Daily mapped altimetry observations

The `ssh` publisher is separate from THCU forecasts. It preserves the original
UTC observation date, dataset version, metres and metres per second. Its one
THSS frame contains ADT above the geoid, SLA relative to the 1993–2012 mean, and
eastward/northward geostrophic velocity. These are mapped observations, not tidal
height, total surface current, depth or a clearance/routing input.

The original non-demo DUACS 0.125° daily map is reduced to a 0.5° display grid.
All sixteen contributors must be finite, in range and ice-free; unknown coastal
and ice cells remain unknown. The display bundle is exactly 4,406,464 bytes.
The source proof exercised the original 83,001,158-byte NetCDF with the actual
CF time encoding and cell-centre bounds. The 128 MiB native-file, 256 MiB output,
3 GiB virtual memory and ten-minute generation limits remain enforced.

`ssh-observations-publisher.yml` runs daily at 08:47 UTC on master. Its manual
provider and publication inputs remain false by default. An explicit provider input enables one generation job under the
existing hash-locked CMEMS dependencies. Its source log and resource evidence
are retained. A separate publish input enables the standard-library-only writer
to restore the same run's sealed bundle. The existing writer validates original
source freshness, producer commit/run, every local and remote byte, immutable
generation assets and deterministic ISO-week shards before changing the two
discovery slots last. The writer receives no provider credentials.

The daily schedule follows successful first publication run38999035362: both
public discovery slots and the immutable4,406,464-byte payload were read without
credentials and matched the same-run sealed artifact. The actual app decoder and
source proxy against live public upstream passed; Chrome and WebKit at300/375px
and130% text passed four real Mapbox phone replays without changing page extent.
The fixed receipt is in `publisher-tests/fixtures/ssh-first-public-readback.json`.
This daily data schedule does not enable the app flag or claim the new Vercel
route is deployed. The six existing CMEMS pipelines and MPA workflow are unchanged.

Source and credit: Copernicus Marine product
`SEALEVEL_GLO_PHY_L4_NRT_008_046`, dataset
`cmems_obs-sl_glo_phy-ssh_nrt_allsat-l4-duacs-0.125deg_P1D`, version `202506`.
Generated using E.U. Copernicus Marine Service Information;
DOI [10.48670/moi-00149](https://doi.org/10.48670/moi-00149);
copyright Mercator Ocean.
