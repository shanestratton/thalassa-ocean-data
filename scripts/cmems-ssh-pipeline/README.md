Daily DUACS mapped altimetry, build 141-08
=======================================

This producer is source-staged. No workflow installs a timer or runs it against
Copernicus, and no SSH assets or public manifest have been published yet.

The exact non-demo dataset is
`cmems_obs-sl_glo_phy-ssh_nrt_allsat-l4-duacs-0.125deg_P1D`, version `202506`,
product `SEALEVEL_GLO_PHY_L4_NRT_008_046`, DOI `10.48670/moi-00149`.
The source date is the most recent available daily observation no later than
yesterday UTC, with a four-day age ceiling. It is never a forecast or retimed
to the app's forecast slider.

ADT is height above the geoid. SLA is height above the 1993–2012 mean sea
surface. The two velocity channels are absolute geostrophic current in m/s.
These fields do not provide tidal height, soundings, under-keel clearance,
tidal/total currents or collision/navigation authority.

One global native 1440×2880 map is downloaded with five variables and a
128 MiB regular-NetCDF ceiling. CF coordinates, original date, packing/units,
variable identities and ice flags are checked before averaging. Every native
cell in a 4×4 block must be finite, in range and ice-free. Otherwise the whole
display cell remains unknown; no coastal or ice-gap interpolation is allowed.
One 720×360 display grid is exactly 4,406,464 bytes, using the separate THSS1
protocol. The six original THCU products and MPA contracts are unchanged.

THSS1 is little-endian. Its 64-byte header is `<4sBBBBHHddddq12x`: magic THSS,
version1, channels4, flags0, reserved0, width720, height360, bounds of cell
centres west−179.75/south−89.75/east179.75/north89.75, and original UTC-midnight
epoch milliseconds. Twelve reserved bytes are zero. Four channel-major
Float32 planes (adt,sla,ugos,vgos) follow, then one byte per cell (1=valid,
0=unknown). Unknown values serialize as zero only alongside their zero mask.

The captured 20,285-byte original consolidated Zarr metadata fixture was
retrieved on 2026-10-17 from the STAC-advertised timeChunked asset's
`.zmetadata`; SHA256
`9a95c91b46f890ae091631544ee80b4c9071fc49f353354372f76c4dad0cc1cf`.
It documents the real optional `nv=2` bounds dimension and Gregorian time
encoding without a `standard_name` attribute. The isolated Linux tests encode
and decode a packed NetCDF with these attributes; fixtures contain synthetic
measurements and are never published as observations.

Source and attribution:
- https://data.marine.copernicus.eu/product/SEALEVEL_GLO_PHY_L4_NRT_008_046/description
- https://documentation.marine.copernicus.eu/PUM/CMEMS-SL-PUM-008-046-047-060-068.pdf
- https://stac.marine.copernicus.eu/metadata/SEALEVEL_GLO_PHY_L4_NRT_008_046/cmems_obs-sl_glo_phy-ssh_nrt_allsat-l4-duacs-0.125deg_P1D_202506/dataset.stac.json
- https://help.marine.copernicus.eu/en/articles/4444611-citing-copernicus-marine-products-and-services

Display credit: Generated using E.U. Copernicus Marine Service Information;
DOI 10.48670/moi-00149; copyright Mercator Ocean.
