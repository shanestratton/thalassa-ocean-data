#!/usr/bin/env python3
"""Held, bounded public GFS/IFS scalar producer. No implicit latest run or publication.

An explicit run and --allow-provider-downloads are required. Only original field
byte ranges are downloaded; the original GRIB metadata is checked independently
of its index, then sampled at exact 1-degree nodes. No temperature/visibility
conversion, forecast extrapolation or model substitution occurs here.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import time
import urllib.request
from array import array
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from atmos_contract import ATMOS_DATASETS, CELLS, FIELDS, build_atmos_bundle, validate_run, write_frame
from publisher_contract import ContractError, _require, parse_utc, producer_provenance

MAX_FIELD_BYTES = 6 * 1024 * 1024
MAX_INDEX_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_REQUESTS = 210
TOTAL_SECONDS = 1200
GFS_HOST = 'noaa-gfs-bdp-pds.s3.amazonaws.com'
IFS_HOST = 'ecmwf-forecasts.s3.amazonaws.com'
IFS_SOURCES = {
    'aws': (IFS_HOST, ''),
    'google': ('storage.googleapis.com', '/ecmwf-open-data'),
}
GFS_SELECTORS = {
    'gust': ('GUST', 'surface'), 'visibility': ('VIS', 'surface'),
    'pressure': ('PRMSL', 'mean sea level'),
    'temperature': ('TMP', '2 m above ground'), 'dewpoint': ('DPT', '2 m above ground'),
}
# Original WMO GRIB2 discipline/category/number and first fixed surface.
GFS_PARAMETERS = {
    'gust': (0, 2, 22, 1, 0), 'visibility': (0, 19, 0, 1, 0),
    'pressure': (0, 3, 1, 101, 0), 'temperature': (0, 0, 0, 103, 2),
    'dewpoint': (0, 0, 6, 103, 2),
}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ContractError('provider redirect refused')


class DownloadBudget:
    def __init__(self, *, clock=time.monotonic, opener=None):
        self.clock = clock
        self.deadline = clock() + TOTAL_SECONDS
        self.bytes = 0
        self.requests = 0
        self.opener = opener or urllib.request.build_opener(NoRedirect())

    def check(self):
        _require(self.clock() <= self.deadline, 'provider generation deadline exceeded')
        _require(self.bytes <= MAX_TOTAL_BYTES, 'provider byte budget exceeded')

    def read(self, url: str, host: str, *, byte_range: tuple[int, int] | None = None) -> bytes:
        from urllib.parse import urlsplit
        parsed = urlsplit(url)
        _require(parsed.scheme == 'https' and parsed.netloc == host and not parsed.query
                 and not parsed.fragment and not parsed.username and not parsed.password,
                 'provider URL outside fixed source')
        self.check()
        self.requests += 1
        _require(self.requests <= MAX_REQUESTS, 'provider request budget exceeded')
        maximum = MAX_INDEX_BYTES
        headers = {'Accept-Encoding': 'identity', 'User-Agent': 'Thalassa-atmos-scalar/1'}
        if byte_range is not None:
            start, end = byte_range
            _require(type(start) is int and type(end) is int and 0 <= start <= end
                     and end - start + 1 <= MAX_FIELD_BYTES, 'provider field range too large')
            maximum = end - start + 1
            headers['Range'] = f'bytes={start}-{end}'
        request = urllib.request.Request(url, headers=headers)
        with self.opener.open(request, timeout=20) as response:
            _require(response.geturl() == url, 'provider response redirected')
            _require(response.headers.get('Content-Encoding', 'identity') == 'identity',
                     'provider encoded response refused')
            _require(response.status == (206 if byte_range else 200), 'provider range/status mismatch')
            if byte_range:
                match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.headers.get('Content-Range', ''))
                _require(match and (int(match[1]), int(match[2])) == byte_range
                         and int(match[3]) > byte_range[1], 'provider Content-Range mismatch')
            advertised = response.headers.get('Content-Length')
            _require(advertised is not None and advertised.isdigit()
                     and 0 < int(advertised) <= maximum, 'provider length absent/oversized')
            if byte_range:
                _require(int(advertised) == maximum, 'provider truncated field range')
            chunks = []
            length = 0
            while True:
                self.check()
                chunk = response.read(min(64 * 1024, maximum + 1 - length))
                if not chunk:
                    break
                length += len(chunk)
                self.bytes += len(chunk)
                _require(length <= maximum, 'provider response exceeded bound')
                self.check()
                chunks.append(chunk)
            _require(length == int(advertised), 'provider truncated response')
        self.check()
        return b''.join(chunks)


def gfs_ranges(text: str, run: datetime, offset: int) -> dict[str, tuple[int, int]]:
    _require(len(text.encode('utf-8')) <= MAX_INDEX_BYTES, 'GFS index too large')
    lines = [line.split(':') for line in text.splitlines() if line]
    _require(5 <= len(lines) <= 5000, 'GFS index row count')
    positions = []
    for row in lines:
        _require(len(row) >= 7 and row[0].isdigit() and row[1].isdigit(), 'malformed GFS index')
        positions.append(int(row[1]))
    _require(positions[0] == 0 and all(a < b for a, b in zip(positions, positions[1:])),
             'GFS index offsets not increasing')
    result = {}
    date = run.strftime('d=%Y%m%d%H')
    valid = 'anl' if offset == 0 else f'{offset} hour fcst'
    for field, (variable, level) in GFS_SELECTORS.items():
        matches = [i for i, row in enumerate(lines) if row[2:6] == [date, variable, level, valid]]
        _require(len(matches) == 1 and matches[0] + 1 < len(lines), f'GFS {field} missing/ambiguous')
        index = matches[0]
        start, end = positions[index], positions[index + 1] - 1
        _require(0 < end - start + 1 <= MAX_FIELD_BYTES, 'GFS field range bound')
        result[field] = (start, end)
    return result


def ifs_range(text: str, run: datetime, offset: int) -> tuple[int, int]:
    _require(len(text.encode('utf-8')) <= MAX_INDEX_BYTES, 'IFS index too large')
    lines = text.splitlines()
    _require(0 < len(lines) <= 5000, 'IFS index row count')
    matches = []
    for line in lines:
        try:
            row = json.loads(line)
        except (ValueError, TypeError) as error:
            raise ContractError('malformed IFS index') from error
        _require(isinstance(row, dict), 'malformed IFS index row')
        if row.get('param') == 'msl' and row.get('levtype') == 'sfc':
            _require(str(row.get('date')) == run.strftime('%Y%m%d')
                     and str(row.get('time')).zfill(4) == run.strftime('%H%M')
                     and str(row.get('step')) == str(offset) and row.get('stream') == 'oper'
                     and row.get('type') == 'fc', 'IFS index source/run/step mismatch')
            start, length = row.get('_offset'), row.get('_length')
            _require(type(start) is int and start >= 0 and type(length) is int
                     and 0 < length <= MAX_FIELD_BYTES, 'IFS field range bound')
            matches.append((start, start + length - 1))
    _require(len(matches) == 1, 'IFS msl missing/ambiguous')
    return matches[0]


def admit_original_metadata(metadata: Mapping[str, Any], dataset: str, field: str,
                            run: datetime, offset: int) -> None:
    """Original guards, before any native value-array expansion."""
    validate_run(dataset, run)
    spec = ATMOS_DATASETS[dataset]
    _require(field in spec['fields'], 'wrong model field')
    _require(type(offset) is int and 0 <= offset <= (spec['steps'] - 1) * 6
             and offset % 6 == 0, 'invalid original forecast offset')
    valid = run + timedelta(hours=offset)
    expected = {
        'edition': 2, 'centre': 7 if dataset == 'gfs-atmos' else 98,
        'gridType': 'regular_ll', 'Ni': 1440, 'Nj': 721, 'numberOfPoints': 1440 * 721,
        'iDirectionIncrementInDegrees': .25, 'jDirectionIncrementInDegrees': .25,
        'latitudeOfFirstGridPointInDegrees': 90, 'latitudeOfLastGridPointInDegrees': -90,
        'longitudeOfFirstGridPointInDegrees': 0 if dataset == 'gfs-atmos' else 180,
        'longitudeOfLastGridPointInDegrees': 359.75 if dataset == 'gfs-atmos' else 179.75,
        'iScansNegatively': 0, 'jScansPositively': 0, 'jPointsAreConsecutive': 0, 'alternativeRowScanning': 0,
        'dataDate': int(run.strftime('%Y%m%d')), 'dataTime': run.hour * 100,
        'validityDate': int(valid.strftime('%Y%m%d')), 'validityTime': valid.hour * 100,
        'stepType': 'instant', 'startStep': offset, 'endStep': offset, 'stepUnits': 1,
    }
    if dataset == 'gfs-atmos':
        discipline, category, number, surface, level = GFS_PARAMETERS[field]
        expected.update(discipline=discipline, parameterCategory=category, parameterNumber=number,
                        typeOfFirstFixedSurface=surface, level=level)
    else:
        expected.update(paramId=151, typeOfFirstFixedSurface=101, level=0)
    _require(all(metadata.get(key) == value for key, value in expected.items()),
             'original GRIB model/field/level/units/grid/run/valid-time mismatch')
    # Unit spellings are ecCodes' own units, never inferred from numerical magnitude.
    acceptable_units = {'m/s': ('m s**-1', 'm s-1', 'm/s'), 'm': ('m',), 'Pa': ('Pa',), 'K': ('K',)}
    _require(metadata.get('units') in acceptable_units[FIELDS[field][1]], 'original GRIB units mismatch')
    _require(metadata.get('bitmapPresent') in (0, 1), 'original GRIB bitmap flag missing')


def sample_original(values: Sequence[float], metadata: Mapping[str, Any], dataset: str,
                    field: str, run: datetime, offset: int) -> tuple[array, bytes]:
    admit_original_metadata(metadata, dataset, field, run, offset)
    _require(len(values) == 1440 * 721, 'original GRIB value cardinality')
    missing = metadata.get('missingValue')
    has_missing = metadata.get('bitmapPresent') == 1
    output = array('f')
    mask = bytearray()
    _, _, low, high = FIELDS[field]
    # Eastward native rows start at the declared original node: GFS 0E,
    # IFS 180E. Admission above binds the exact endpoints/scanning/increment.
    source_origin = metadata['longitudeOfFirstGridPointInDegrees']
    for row in range(181):
        source_row = (180 - row) * 4
        for col in range(360):
            source_col = int(((col - 180 - source_origin) % 360) * 4)
            value = float(values[source_row * 1440 + source_col])
            if (has_missing and value == missing) or not math.isfinite(value):
                output.append(0)
                mask.append(0)
            else:
                _require(low <= value <= high, f'original GRIB {field} outside physical range')
                output.append(value)
                mask.append(1)
    _require(sum(mask) >= CELLS * .95, 'original GRIB coverage incomplete')
    return output, bytes(mask)


def read_original_metadata(handle, codes) -> dict[str, Any]:
    """Fixed scalar metadata only; no values or native grid allocation."""
    integer_keys = ['edition', 'centre', 'Ni', 'Nj', 'numberOfPoints',
                    'iScansNegatively', 'jScansPositively', 'jPointsAreConsecutive', 'alternativeRowScanning',
                    'dataDate', 'dataTime', 'validityDate', 'validityTime', 'startStep', 'endStep',
                    'stepUnits', 'discipline', 'parameterCategory', 'parameterNumber',
                    'typeOfFirstFixedSurface', 'level', 'paramId', 'bitmapPresent']
    double_keys = ['iDirectionIncrementInDegrees', 'jDirectionIncrementInDegrees',
                   'latitudeOfFirstGridPointInDegrees', 'latitudeOfLastGridPointInDegrees',
                   'longitudeOfFirstGridPointInDegrees', 'longitudeOfLastGridPointInDegrees', 'missingValue']
    metadata = {key: codes.codes_get_long(handle, key) for key in integer_keys}
    metadata.update({key: codes.codes_get_double(handle, key) for key in double_keys})
    metadata.update({key: codes.codes_get_string(handle, key) for key in ['gridType', 'stepType', 'units']})
    return metadata


def decode_message(raw: bytes, dataset: str, field: str, run: datetime, offset: int):
    _require(16 <= len(raw) <= MAX_FIELD_BYTES and raw[:4] == b'GRIB' and raw[-4:] == b'7777'
             and int.from_bytes(raw[8:16], 'big') == len(raw), 'not exactly one bounded GRIB2 message')
    import eccodes
    handle = eccodes.codes_new_from_message(raw)
    try:
        metadata = read_original_metadata(handle, eccodes)
        admit_original_metadata(metadata, dataset, field, run, offset)
        return sample_original(eccodes.codes_get_values(handle), metadata, dataset, field, run, offset)
    finally:
        eccodes.codes_release(handle)


def ifs_source_location(source):
    """One explicitly selected official replica; no URL input or failover.

    ECMWF's own ecmwf-opendata/urls.py lists the Google bucket. The original
    AWS path remains the default so existing reviewed invocations do not change.
    """
    _require(type(source) is str and source in IFS_SOURCES, 'unknown IFS source')
    return IFS_SOURCES[source]


def ifs_original_url(run, offset, source='aws'):
    host, prefix = ifs_source_location(source)
    _require(type(offset) is int and 0 <= offset <= 240 and offset % 6 == 0,
             'invalid IFS original step')
    stamp, hour = run.strftime('%Y%m%d'), run.strftime('%H')
    return f'https://{host}{prefix}/{stamp}/{hour}z/ifs/0p25/oper/{stamp}{hour}0000-{offset}h-oper-fc.grib2', host


def generate(dataset: str, run: datetime, out: Path, *, budget=None, decoder=decode_message, ifs_source='aws') -> Path:
    ifs_source_location(ifs_source)
    _require(dataset == 'ifs-pressure' or ifs_source == 'aws', 'IFS source cannot alter a GFS generation')
    spec = ATMOS_DATASETS[dataset]
    validate_run(dataset, run)
    current = datetime.now(timezone.utc)
    _require(current - timedelta(hours=spec['max_age_hours']) <= run <= current,
             'requested provider run is stale or future')
    provenance = producer_provenance()
    _require(not out.exists(), 'output must be a fresh directory')
    out.mkdir(parents=True)
    budget = budget or DownloadBudget()
    paths = []
    for index in range(spec['steps']):
        budget.check()
        offset = index * 6
        stamp, hour = run.strftime('%Y%m%d'), run.strftime('%H')
        if dataset == 'gfs-atmos':
            base = f'https://{GFS_HOST}/gfs.{stamp}/{hour}/atmos/gfs.t{hour}z.pgrb2.0p25.f{offset:03d}'
            ranges = gfs_ranges(budget.read(base + '.idx', GFS_HOST).decode('utf-8'), run, offset)
            planes = {field: decoder(budget.read(base, GFS_HOST, byte_range=ranges[field]), dataset, field, run, offset)
                      for field in spec['fields']}
        else:
            base, host = ifs_original_url(run, offset, ifs_source)
            selected = ifs_range(budget.read(base.removesuffix('.grib2') + '.index', host).decode('utf-8'), run, offset)
            planes = {'pressure': decoder(budget.read(base, host, byte_range=selected), dataset, 'pressure', run, offset)}
        budget.check()
        path = out / f'step-{index:03d}.bin'
        write_frame(path, dataset, run, offset, planes)
        paths.append(path)
    budget.check()
    return build_atmos_bundle(dataset, run, paths, out / 'bundle', provenance)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', choices=sorted(ATMOS_DATASETS), required=True)
    parser.add_argument('--run', required=True, help='Exact eligible UTC cycle, e.g. 2026-10-17T00:00:00Z')
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--allow-provider-downloads', action='store_true', default=False)
    parser.add_argument('--ifs-source', choices=sorted(IFS_SOURCES), default='aws',
                        help='Explicit official replica; no automatic fallback')
    args = parser.parse_args()
    if not args.allow_provider_downloads:
        parser.error('provider downloads are held; explicit --allow-provider-downloads required')
    generate(args.dataset, parse_utc(args.run, 'run'), args.out, ifs_source=args.ifs_source)


if __name__ == '__main__':
    main()
