#!/usr/bin/env python3
"""One genuine daily DUACS map; no forecasts and no extrapolation into gaps."""
from __future__ import annotations

import json
import hashlib
import logging
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from publisher_contract import build_cmems_bundle, producer_provenance
from ssh_grid import DATASET_ID, DATASET_VERSION, PRODUCT_ID, SSH_METADATA, VARIABLES, coarsen_duacs, write_ssh_grid

CATALOG_URL = ('https://stac.marine.copernicus.eu/metadata/' + PRODUCT_ID + '/' +
               DATASET_ID + '_' + DATASET_VERSION + '/dataset.stac.json')
MAX_NATIVE_BYTES = 128 * 1024 * 1024
UNITS = {'adt': 'm', 'sla': 'm', 'ugos': 'm/s', 'vgos': 'm/s'}
STANDARDS = {
    'adt': 'sea_surface_height_above_geoid',
    'sla': 'sea_surface_height_above_sea_level',
    'ugos': 'surface_geostrophic_eastward_sea_water_velocity',
    'vgos': 'surface_geostrophic_northward_sea_water_velocity',
}


def source_date(catalog: dict, now: datetime) -> datetime:
    if catalog.get('id') != DATASET_ID + '_' + DATASET_VERSION:
        raise ValueError('Exact non-demo DUACS dataset/version required')
    props = catalog.get('properties', {})
    dimensions = props.get('cube:dimensions', {})
    for key, axis, extent in [('latitude', 'y', [-89.9375, 89.9375]),
                              ('longitude', 'x', [-179.9375, 179.9375])]:
        item = dimensions.get(key, {})
        if (item.get('type'), item.get('axis'), item.get('extent'), item.get('step'), item.get('reference_system')) != (
                'spatial', axis, extent, .125, 4326):
            raise ValueError('Catalog native grid changed; download budget no longer reviewed')
    if dimensions.get('time', {}).get('step') != 'P1D':
        raise ValueError('Original daily cadence required')
    variables = props.get('cube:variables', {})
    for key in VARIABLES:
        if (variables.get(key, {}).get('unit') != UNITS[key] or variables[key].get('standardName') != STANDARDS[key]
                or variables[key].get('dimensions') != ['time', 'latitude', 'longitude']):
            raise ValueError('Catalog variables/units changed')
    if variables.get('flag_ice', {}).get('flagValues') != [0, 1]:
        raise ValueError('Original ice flags required')
    end = datetime.fromisoformat(props['end_datetime'].replace('Z', '+00:00'))
    if end.tzinfo is None or end.utcoffset() != timedelta(0) or end.hour or end.minute or end.second or end.microsecond:
        raise ValueError('Catalog daily source time invalid')
    utc_day = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    chosen = min(utc_day - timedelta(days=1), end)
    if not timedelta(0) <= now - chosen <= timedelta(days=4):
        raise ValueError('No recent original daily observation')
    return chosen


def decode_native(dataset, expected_time: datetime):
    """Validate CF identity/units/axes before loading at most five native planes."""
    import numpy as np
    sizes = dict(dataset.sizes)
    # STAC advertises an optional two-element coordinate-bounds dimension.
    # It may survive the SDK subset; it is never a data plane or averaged.
    if sizes not in ({'time': 1, 'latitude': 1440, 'longitude': 2880},
                     {'time': 1, 'latitude': 1440, 'longitude': 2880, 'nv': 2}):
        raise ValueError('One exact 0.125 degree native observation required')
    for key, units, standard, axis in [('latitude', 'degrees_north', 'latitude', 'Y'),
                                       ('longitude', 'degrees_east', 'longitude', 'X')]:
        value = dataset[key]
        if value.dims != (key,) or value.attrs.get('units') != units or value.attrs.get('standard_name') != standard:
            raise ValueError('CF coordinate units/identity changed')
        if value.attrs.get('axis', axis) != axis:
            raise ValueError('CF coordinate axis changed')
    # The original DUACS time coordinate has no standard_name. Preserve its
    # actual Gregorian/1950 CF encoding rather than fabricating that attribute.
    time_units = dataset.time.encoding.get('units')
    if (dataset.time.dims != ('time',) or dataset.time.attrs.get('standard_name') not in (None, 'time')
            or time_units not in ('days since 1950-01-01', 'days since 1950-01-01 00:00:00',
                                  'days since 1950-01-01T00:00:00')
            or dataset.time.encoding.get('calendar') != 'gregorian'):
        raise ValueError('CF time identity changed')
    lat = dataset.latitude.values
    lon = dataset.longitude.values
    if not (np.allclose(lat, -89.9375 + np.arange(1440) * .125, rtol=0, atol=1e-6) and
            np.allclose(lon, -179.9375 + np.arange(2880) * .125, rtol=0, atol=1e-6)):
        raise ValueError('Native source axes/spacing/orientation changed')
    actual_time = int(np.asarray(dataset.time.values[0], dtype='datetime64[ms]').astype('int64'))
    if actual_time != int(expected_time.timestamp()) * 1000:
        raise ValueError('Original source time differs from requested daily map')
    for key in VARIABLES:
        value = dataset[key]
        if value.dims != ('time', 'latitude', 'longitude') or value.attrs.get('units') != UNITS[key] or value.attrs.get('standard_name') != STANDARDS[key]:
            raise ValueError('CF variable dimensions/units/identity changed')
    ice = dataset['flag_ice']
    if ice.dims != ('time', 'latitude', 'longitude') or list(ice.attrs.get('flag_values', [])) != [0, 1]:
        raise ValueError('Original CF ice flags required')
    return coarsen_duacs([dataset[key].values[0] for key in VARIABLES], ice.values[0]), actual_time


def main() -> None:
    import copernicusmarine
    import xarray as xr
    username, password = os.environ.get('COPERNICUS_MARINE_USERNAME'), os.environ.get('COPERNICUS_MARINE_PASSWORD')
    if not username or not password:
        raise ValueError('Existing Copernicus credentials required')
    output = Path(os.environ.get('CMEMS_OUT_DIR', '/tmp/cmems-ssh')).resolve()
    if output != Path('/tmp/cmems-ssh') or output.is_symlink():
        raise ValueError('Dedicated isolated output directory required')
    output.mkdir(parents=True, exist_ok=False)
    request = urllib.request.Request(CATALOG_URL, headers={'User-Agent': 'Thalassa/141 SSH metadata'})
    with urllib.request.urlopen(request, timeout=30) as response:
        encoded = response.read(128 * 1024 + 1)
        if len(encoded) > 128 * 1024:
            raise ValueError('Catalog byte bound exceeded')
    catalog = json.loads(encoded)
    chosen = source_date(catalog, datetime.now(timezone.utc))
    native = output / 'duacs-one-day.nc'
    copernicusmarine.subset(
        dataset_id=DATASET_ID, dataset_version=DATASET_VERSION,
        variables=[*VARIABLES, 'flag_ice'], username=username, password=password,
        minimum_longitude=-180, maximum_longitude=180, minimum_latitude=-90, maximum_latitude=90,
        start_datetime=chosen, end_datetime=chosen, coordinates_selection_method='strict-inside',
        output_directory=str(output), output_filename=native.name, overwrite=False,
        disable_progress_bar=True, raise_if_updating=True,
    )
    if not native.is_file() or native.is_symlink() or not 0 < native.stat().st_size <= MAX_NATIVE_BYTES:
        raise ValueError('One bounded regular native NetCDF required')
    with xr.open_dataset(native, decode_cf=True) as dataset:
        (fields, valid), epoch = decode_native(dataset, chosen)
    binary = output / 'source.bin'
    summary = write_ssh_grid(binary, fields, valid, epoch)
    stamp = chosen.isoformat().replace('+00:00', 'Z')
    draft = build_cmems_bundle(dataset_key='ssh', source_paths=[binary], offsets_hours=[0],
                               data_times=[stamp], bundle_dir=output / 'bundle',
                               provenance=producer_provenance(), metadata=SSH_METADATA)
    (output / 'SOURCE_METADATA.json').write_bytes(encoded)
    with native.open('rb') as native_file:
        native_sha256 = hashlib.file_digest(native_file, 'sha256').hexdigest()
    print(json.dumps({'source_date': stamp, 'dataset': DATASET_ID, 'dataset_version': DATASET_VERSION,
                      'native_bytes': native.stat().st_size,
                      'native_sha256': native_sha256,
                      'metadata_sha256': hashlib.sha256(encoded).hexdigest(),
                      'binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
                      'grid': summary, 'draft': str(draft)}))


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    main()
