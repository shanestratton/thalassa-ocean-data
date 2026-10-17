#!/usr/bin/env python3
"""Held one-shot original GFS/IFS diagnostic, never a publishable cohort.

Provider access requires explicit cycles and --read-reviewed-first-source. The
actual Linux contract/native proofs must pass before an operator reviews a read.
This command has no publisher, credentials, latest-run selection or timer lane.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import signal
import sys
import time
import urllib.error
from array import array
from datetime import datetime, timedelta, timezone
from pathlib import Path

from atmos_contract import (ATMOS_DATASETS, CELLS, FIELDS, file_bytes, metadata_for,
                            validate_run, write_frame)
from atmos_pipeline import (DownloadBudget, GFS_HOST, IFS_HOST, MAX_FIELD_BYTES,
                            admit_original_metadata, decode_message, gfs_ranges,
                            ifs_range, read_original_metadata)
from publisher_contract import ContractError, _require, parse_utc

MAX_READS = 16
MAX_BODY_BYTES = 80 * 1024 * 1024
MAX_DIRECTORY_BYTES = 96 * 1024 * 1024
MAX_HEADER_BYTES = 64 * 1024
MAX_SECONDS = 600
MAX_PROCESS_BYTES = 2 * 1024 * 1024 * 1024
SAMPLE_POINTS = ((-180, -90), (0, 0), (179, 90), (153, -27))


def digest(body):
    return hashlib.sha256(body).hexdigest()


class ProofBudget(DownloadBudget):
    """Existing strict range reader, with narrower one-shot lifetime/receipts."""
    def __init__(self, out, *, clock=time.monotonic, opener=None):
        super().__init__(clock=clock, opener=opener)
        self.out = out
        self.deadline = clock() + MAX_SECONDS
        self.directory_bytes = 0
        self.body_bytes = 0
        self.records = []
        self.label = ''
        self.max_reads = MAX_READS
        self.opener = RecordingOpener(self, self.opener)

    def check(self):
        _require(self.clock() <= self.deadline, 'first-source deadline exceeded')
        _require(self.bytes <= MAX_BODY_BYTES and self.body_bytes <= MAX_BODY_BYTES,
                 'first-source body budget exceeded')
        _require(self.requests <= self.max_reads, 'first-source request budget exceeded')
        _require(self.directory_bytes <= MAX_DIRECTORY_BYTES, 'first-source directory budget exceeded')

    def write(self, relative, body):
        # Bounded failure receipts remain writable after the read deadline.
        # This never authorizes another provider read or a decoded frame.
        _require(self.directory_bytes + len(body) <= MAX_DIRECTORY_BYTES,
                 'first-source directory budget exceeded before write')
        path = self.out / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('xb') as target:
            target.write(body)
        self.directory_bytes += len(body)
        return {'path': relative, 'bytes': len(body), 'sha256': digest(body)}

    def write_json(self, relative, value):
        return self.write(relative, (json.dumps(value, indent=2, allow_nan=False) + '\n').encode())

    def read_original(self, label, url, host, *, byte_range=None):
        self.check()
        _require(self.requests < self.max_reads, 'first-source request budget exceeded before read')
        _require(self.body_bytes < MAX_BODY_BYTES, 'first-source body budget exhausted before open')
        _require(self.directory_bytes < MAX_DIRECTORY_BYTES - 1024 * 1024,
                 'first-source directory reserve exhausted before open')
        _require(label and all(c.isalnum() or c in '-.' for c in label), 'invalid internal receipt label')
        self.label = label
        return super().read(url, host, byte_range=byte_range)


class RecordingOpener:
    def __init__(self, budget, opener):
        self.budget, self.opener = budget, opener

    def open(self, request, timeout):
        self.budget.check()
        try:
            response = self.opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            response = error
        try:
            return RecordedResponse(self.budget, request, response)
        except BaseException:
            response.close()
            raise


class RecordedResponse:
    def __init__(self, budget, request, response):
        self.budget, self.response = budget, response
        self.headers, self.status = response.headers, response.status
        headers = list(self.headers.items())
        _require(len(json.dumps(headers).encode()) <= MAX_HEADER_BYTES, 'source headers exceed evidence bound')
        self.number = budget.requests
        self.base = f'originals/{self.number:02d}-{budget.label}'
        self.path = budget.out / (self.base + '.body')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.target = self.path.open('xb')
        self.length, self.hash = 0, hashlib.sha256()
        self.record = {'number': self.number, 'url': request.full_url,
                       'requestHeaders': list(request.header_items()), 'responseUrl': response.geturl(),
                       'status': self.status, 'responseHeaders': headers,
                       'requestedRange': request.get_header('Range'), 'bodyPath': self.base + '.body'}

    def geturl(self):
        return self.response.geturl()

    def __enter__(self):
        return self

    def read(self, size):
        self.budget.check()
        remaining = MAX_BODY_BYTES - self.budget.body_bytes
        _require(remaining > 0, 'first-source body budget exhausted before read')
        chunk = self.response.read(min(size, remaining))
        _require(isinstance(chunk, bytes) and len(chunk) <= min(size, remaining), 'source read exceeds request')
        _require(self.budget.directory_bytes + len(chunk) <= MAX_DIRECTORY_BYTES - 1024 * 1024,
                 'source evidence would consume directory reserve')
        self.target.write(chunk)
        self.hash.update(chunk)
        self.length += len(chunk)
        self.budget.body_bytes += len(chunk)
        self.budget.directory_bytes += len(chunk)
        return chunk

    def __exit__(self, kind, error, traceback):
        try:
            self.target.close()
            self.record.update(bytes=self.length, sha256=self.hash.hexdigest(),
                               readCompleted=error is None, error=None if error is None else str(error))
            self.budget.records.append(dict(self.record))
            self.budget.write_json(self.base + '.headers.json', self.record)
        finally:
            self.response.close()
        return False


def native_source_probe(raw, dataset, field, run, offset):
    """Independent original-node queries, only after the same metadata gate.

    The actual full decoder is called separately. ecCodes documents its indexed
    values accessor at https://sites.ecmwf.int/docs/eccodes/namespaceec_codes.html.
    """
    _require(16 <= len(raw) <= MAX_FIELD_BYTES and raw[:4] == b'GRIB' and raw[-4:] == b'7777'
             and int.from_bytes(raw[8:16], 'big') == len(raw), 'unbounded diagnostic GRIB message')
    import eccodes
    handle = eccodes.codes_new_from_message(raw)
    try:
        metadata = read_original_metadata(handle, eccodes)
        try:
            admit_original_metadata(metadata, dataset, field, run, offset)
        except ContractError as error:
            raise DiagnosticMetadataRefused(metadata, str(error)) from error
        indexes = [int((90 - lat) * 4) * 1440 + int((lon % 360) * 4) for lon, lat in SAMPLE_POINTS]
        selected = eccodes.codes_get_double_elements(handle, 'values', indexes)
        _require(len(selected) == len(SAMPLE_POINTS), 'native sample cardinality')
        samples = []
        for (lon, lat), index, value in zip(SAMPLE_POINTS, indexes, selected):
            value = float(value)
            unknown = (metadata['bitmapPresent'] == 1 and value == metadata['missingValue']) or not math.isfinite(value)
            samples.append({'longitude': lon, 'latitude': lat, 'sourceIndex': index,
                            'displayIndex': (lat + 90) * 360 + lon + 180,
                            'originalValue': value if math.isfinite(value) else None,
                            'unknown': unknown})
        return {'metadata': metadata, 'samples': samples}
    finally:
        eccodes.codes_release(handle)


class DiagnosticMetadataRefused(ContractError):
    def __init__(self, metadata, message):
        super().__init__(message)
        self.metadata = metadata


def verify_samples(plane, probe):
    values, mask = plane
    _require(len(values) == len(mask) == CELLS, 'diagnostic plane cardinality')
    for sample in probe['samples']:
        i = sample['displayIndex']
        expected = 0 if sample['unknown'] else array('f', [sample['originalValue']])[0]
        _require(values[i] == expected and mask[i] == (0 if sample['unknown'] else 1),
                 'original-node value or mask disagrees with actual decoder')


def prove_first_source(gfs_run, ifs_run, out, *, allow_provider_downloads=False,
                       budget_factory=ProofBudget, decoder=decode_message,
                       probe=native_source_probe, now=None, runtime=None, ifs_only=False):
    _require(allow_provider_downloads is True, 'reviewed first-source reads are held')
    _require(type(ifs_only) is bool, 'invalid diagnostic scope')
    now = now or datetime.now(timezone.utc)
    validate_run('gfs-atmos', gfs_run)
    validate_run('ifs-pressure', ifs_run)
    _require(gfs_run.hour == 0 and ifs_run.hour == 12 and ifs_run == gfs_run - timedelta(hours=12),
             'diagnostic requires explicit GFS00 and preceding IFS12 cycles')
    for dataset, run in [('gfs-atmos', gfs_run), ('ifs-pressure', ifs_run)]:
        _require(now - timedelta(hours=ATMOS_DATASETS[dataset]['max_age_hours']) <= run <= now,
                 'diagnostic requested cycle is stale or future')
    _require(not out.exists() and not out.is_symlink(), 'diagnostic output must be fresh')
    out.mkdir(parents=True)
    budget = budget_factory(out)
    budget.max_reads = 4 if ifs_only else MAX_READS
    frames, fields = [], []
    report = {'purpose': 'original-source diagnostic only', 'completeCohort': False, 'publication': False,
              'gfsRun': gfs_run.isoformat(), 'ifsRun': ifs_run.isoformat(),
              'runtime': runtime, 'scope': 'ifs-pressure-only' if ifs_only else 'gfs-and-ifs',
              'requestedAt': now.isoformat(), 'limits': {'reads': budget.max_reads, 'bodyBytes': MAX_BODY_BYTES,
              'directoryBytes': MAX_DIRECTORY_BYTES, 'seconds': MAX_SECONDS, 'readSeconds': 20,
              'fieldBytes': MAX_FIELD_BYTES, 'processBytes': MAX_PROCESS_BYTES}}
    try:
        selections = [('ifs-pressure', ifs_run, (0, 240))] if ifs_only else [
            ('gfs-atmos', gfs_run, (0, 168)), ('ifs-pressure', ifs_run, (0, 240))]
        for dataset, run, offsets in selections:
            stamp, hour = run.strftime('%Y%m%d'), run.strftime('%H')
            for offset in offsets:
                budget.check()
                prefix = f'{dataset}-{offset:03d}'
                if dataset == 'gfs-atmos':
                    url = f'https://{GFS_HOST}/gfs.{stamp}/{hour}/atmos/gfs.t{hour}z.pgrb2.0p25.f{offset:03d}'
                    index = budget.read_original(prefix + '-index', url + '.idx', GFS_HOST)
                    ranges, host = gfs_ranges(index.decode('utf-8'), run, offset), GFS_HOST
                else:
                    url = f'https://{IFS_HOST}/{stamp}/{hour}z/ifs/0p25/oper/{stamp}{hour}0000-{offset}h-oper-fc.grib2'
                    index = budget.read_original(prefix + '-index', url.removesuffix('.grib2') + '.index', IFS_HOST)
                    ranges, host = {'pressure': ifs_range(index.decode('utf-8'), run, offset)}, IFS_HOST
                planes = {}
                for field in ATMOS_DATASETS[dataset]['fields']:
                    raw = budget.read_original(prefix + '-' + field, url, host, byte_range=ranges[field])
                    try:
                        original = probe(raw, dataset, field, run, offset)
                    except DiagnosticMetadataRefused as error:
                        budget.write_json(f'native/{prefix}-{field}-refused.json',
                                          {'metadata': error.metadata, 'admitted': False, 'error': str(error)})
                        raise
                    budget.write_json(f'native/{prefix}-{field}.json', original)
                    budget.check()
                    planes[field] = decoder(raw, dataset, field, run, offset)
                    verify_samples(planes[field], original)
                    fields.append({'dataset': dataset, 'offset': offset, 'field': field,
                                   'originalSha256': digest(raw), 'originalRange': list(ranges[field]),
                                   'validCells': sum(planes[field][1]), 'cells': CELLS,
                                   'samples': original['samples'], 'units': original['metadata']['units']})
                    budget.check()
                path = out / f'diagnostic-{prefix}.bin'
                budget.check()
                _require(budget.directory_bytes + file_bytes(dataset) <= MAX_DIRECTORY_BYTES,
                         'diagnostic frame exceeds directory budget')
                write_frame(path, dataset, run, offset, planes)
                budget.directory_bytes += path.stat().st_size
                frames.append({'dataset': dataset, 'offset': offset, 'path': path.name,
                               'bytes': path.stat().st_size, 'sha256': digest(path.read_bytes()),
                               'source': metadata_for(dataset)})
        expected = (4, 2, 2) if ifs_only else (MAX_READS, 4, 12)
        _require((budget.requests, len(frames), len(fields)) == expected,
                 'diagnostic original inventory incomplete')
        report['state'] = 'observed'
    except BaseException as error:
        report.update(state='failed', error=type(error).__name__ + ': ' + str(error))
        raise
    finally:
        # Includes any partially written frame; never delete evidence to fit.
        budget.directory_bytes = sum(p.stat().st_size for p in out.rglob('*') if p.is_file())
        report.update(requests=budget.requests, bodyBytes=budget.body_bytes,
                      directoryBytesBeforeReport=budget.directory_bytes,
                      originals=budget.records, frames=frames, fields=fields)
        budget.write_json('result.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gfs-run', required=True)
    parser.add_argument('--ifs-run', required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--read-reviewed-first-source', action='store_true', default=False)
    parser.add_argument('--ifs-only', action='store_true', default=False,
                        help='Four-read IFS diagnostic only; never repeats GFS or forms a complete cohort')
    args = parser.parse_args()
    if not args.read_reviewed_first_source:
        parser.error('provider reads are held; explicit --read-reviewed-first-source required')
    _require(sys.platform == 'linux', 'native source diagnostic requires the reviewed isolated Linux process')
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (MAX_PROCESS_BYTES, MAX_PROCESS_BYTES))
    signal.signal(signal.SIGALRM, deadline_reached)
    signal.alarm(MAX_SECONDS)
    try:
        # Fail before any provider request if the reviewed native closure is
        # unavailable. Never reproduce the historical mixed NetCDF/ecCodes job.
        import eccodes
        forbidden = {'xarray', 'netCDF4', 'h5py', 'rasterio', 'copernicusmarine'}
        _require(not forbidden.intersection(sys.modules), 'mixed native dependency closure refused')
        runtime = {'python': sys.version, 'ecCodesApi': eccodes.codes_get_api_version(),
                   'forbiddenImportedModules': []}
        prove_first_source(parse_utc(args.gfs_run, 'gfs run'), parse_utc(args.ifs_run, 'ifs run'), args.out,
                           allow_provider_downloads=True, runtime=runtime, ifs_only=args.ifs_only)
    finally:
        signal.alarm(0)


def deadline_reached(_signal, _frame):
    raise TimeoutError('first-source 600 s process deadline')


if __name__ == '__main__':
    main()
