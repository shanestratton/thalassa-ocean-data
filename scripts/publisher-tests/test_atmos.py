"""Offline atmospheric ownership, source science, range and publication tests."""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from array import array
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from atmos_contract import (ATMOS_DATASETS, BOUNDS, CELLS, FIELDS, HEADER_BYTES,
                            build_atmos_bundle, file_bytes, metadata_for, validate_frame,
                            validate_run, write_frame)
from atmos_pipeline import (DownloadBudget, GFS_HOST, MAX_TOTAL_BYTES, gfs_ranges,
                            ifs_range, sample_original)
from publisher_contract import (ContractError, _generation, validate_manifest,
                                validate_legacy_v1_bootstrap, validate_publication_freshness,
                                validate_shard_inventory, asset_shard_tag)

RUN = datetime(2026, 10, 17, tzinfo=timezone.utc)
HEX = 'a' * 64
VALUES = {'gust': 12, 'visibility': 5000, 'pressure': 101200, 'temperature': 290, 'dewpoint': 289}


def manifest(dataset='gfs-atmos'):
    spec = ATMOS_DATASETS[dataset]
    generation = _generation('2026-10-17T00:00:00Z', [HEX] * spec['steps'], dataset)
    return {
        'schema_version': 2, 'dataset': {'key': dataset, 'id': spec['id']}, 'generation': generation,
        'generated_at': '2026-10-17T04:00:00Z', 'published_at': '2026-10-17T04:05:00Z',
        'data_start': '2026-10-17T00:00:00Z',
        'data_end': (RUN + timedelta(hours=(spec['steps'] - 1) * 6)).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'cadence_hours': 6, 'dimensions': {'width': 360, 'height': 181}, 'bounds': dict(BOUNDS),
        'producer': {'commit': 'b' * 40, 'run_id': 1, 'run_attempt': 1}, 'metadata': metadata_for(dataset),
        'files': [{'step': i, 'offset_hours': i * 6,
                   'data_time': (RUN + timedelta(hours=i * 6)).strftime('%Y-%m-%dT%H:%M:%SZ'),
                   'filename': f'{generation}-h{i:03d}.bin', 'bytes': file_bytes(dataset),
                   'sha256': HEX, 'content_type': 'application/octet-stream'} for i in range(spec['steps'])],
    }


def original_metadata(field='pressure', dataset='gfs-atmos', offset=0):
    from atmos_pipeline import GFS_PARAMETERS
    valid = RUN + timedelta(hours=offset)
    result = dict(
        edition=2, centre=7 if dataset == 'gfs-atmos' else 98,
        gridType='regular_ll', Ni=1440, Nj=721, numberOfPoints=1440 * 721,
        iDirectionIncrementInDegrees=.25, jDirectionIncrementInDegrees=.25,
        latitudeOfFirstGridPointInDegrees=90, latitudeOfLastGridPointInDegrees=-90,
        longitudeOfFirstGridPointInDegrees=0 if dataset == 'gfs-atmos' else 180,
        longitudeOfLastGridPointInDegrees=359.75 if dataset == 'gfs-atmos' else 179.75,
        iScansNegatively=0, jScansPositively=0, jPointsAreConsecutive=0, alternativeRowScanning=0,
        dataDate=20261017, dataTime=0, validityDate=int(valid.strftime('%Y%m%d')),
        validityTime=valid.hour * 100, stepType='instant', startStep=offset, endStep=offset,
        stepUnits=1, units=FIELDS[field][1], missingValue=9999, bitmapPresent=0,
    )
    if dataset == 'gfs-atmos':
        keys = ('discipline', 'parameterCategory', 'parameterNumber', 'typeOfFirstFixedSurface', 'level')
        result.update(zip(keys, GFS_PARAMETERS[field]))
    else:
        result.update(paramId=151, typeOfFirstFixedSurface=101, level=0)
    return result


class AtmosContractTests(unittest.TestCase):
    def test_complete_models_have_independent_identity_and_full_horizon(self):
        for dataset, horizon in [('gfs-atmos', 168), ('ifs-pressure', 240)]:
            source = manifest(dataset)
            validate_manifest(source, expected_dataset=dataset, allow_draft=False)
            self.assertEqual(source['files'][-1]['offset_hours'], horizon)
            self.assertEqual(file_bytes(dataset), 1629064 if dataset == 'gfs-atmos' else 325864)
            validate_publication_freshness(source, now=RUN + timedelta(hours=5))
        self.assertNotEqual(manifest()['generation'], manifest('ifs-pressure')['generation'])

    def test_legacy_scalar_bootstrap_is_not_admitted(self):
        with self.assertRaisesRegex(ContractError, 'no legacy'):
            validate_legacy_v1_bootstrap({'version': 1}, 'gfs-atmos')

    def test_source_clock_does_not_turn_a_06_ifs_cycle_into_a_10_day_forecast(self):
        for hour in [6, 18]:
            with self.assertRaisesRegex(ContractError, 'cycle'):
                validate_run('ifs-pressure', RUN + timedelta(hours=hour))
        for dataset, age in [('gfs-atmos', 13), ('ifs-pressure', 25)]:
            source = manifest(dataset)
            source['generated_at'] = source['published_at'] = (RUN + timedelta(hours=age - 1)).strftime('%Y-%m-%dT%H:%M:%SZ')
            with self.assertRaisesRegex(ContractError, 'stale'):
                validate_publication_freshness(source, now=RUN + timedelta(hours=age))

    def test_missing_frames_source_aliases_and_wrong_attribution_refuse(self):
        changes = [lambda m: m['files'].pop(), lambda m: m['files'][1].update(offset_hours=7),
                   lambda m: m['files'][1].update(sha256='c' * 64),
                   lambda m: m['metadata'].update(model='aifs'),
                   lambda m: m['metadata'].update(source_resolution_degrees='0.09'),
                   lambda m: m['metadata'].update(licence='unreviewed'),
                   lambda m: m['bounds'].update(east=180)]
        for change in changes:
            with self.subTest(change=change):
                source = manifest(); change(source)
                with self.assertRaises(ContractError):
                    validate_manifest(source, expected_dataset='gfs-atmos', allow_draft=False)

    def test_binary_owns_exact_model_clocks_fields_masks_and_units(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'frame.bin'
            planes = {field: (array('f', [VALUES[field]]) * CELLS, b'\1' * CELLS)
                      for field in ATMOS_DATASETS['gfs-atmos']['fields']}
            write_frame(path, 'gfs-atmos', RUN, 6, planes)
            self.assertEqual(path.stat().st_size, file_bytes('gfs-atmos'))
            validate_frame(path, 'gfs-atmos', RUN, 6)
            with self.assertRaises(ContractError): validate_frame(path, 'gfs-atmos', RUN, 0)
            raw = bytearray(path.read_bytes()); raw[56] = 3; path.write_bytes(raw)
            with self.assertRaisesRegex(ContractError, 'header'): validate_frame(path, 'gfs-atmos', RUN, 6)

    def test_binary_never_grants_unknown_cells_depth_or_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'frame.bin'
            values = array('f', [101200]) * CELLS
            mask = bytearray(b'\1' * CELLS); mask[0] = 0; values[0] = 0
            write_frame(path, 'ifs-pressure', RUN, 0, {'pressure': (values, bytes(mask))})
            raw = bytearray(path.read_bytes()); raw[HEADER_BYTES + CELLS * 4] = 2; path.write_bytes(raw)
            with self.assertRaisesRegex(ContractError, 'mask'): validate_frame(path, 'ifs-pressure', RUN, 0)

    def test_complete_real_binary_bundle_is_sealed_before_manifest_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); paths = []
            planes = {'pressure': (array('f', [101200]) * CELLS, b'\1' * CELLS)}
            for index in range(41):
                path = root / f'{index}.bin'; write_frame(path, 'ifs-pressure', RUN, index * 6, planes); paths.append(path)
            draft = build_atmos_bundle('ifs-pressure', RUN, paths, root / 'bundle',
                                       {'commit': 'b' * 40, 'run_id': 1, 'run_attempt': 1},
                                       generated_at=RUN + timedelta(hours=5))
            source = json.loads(draft.read_text(encoding='utf-8'))
            validate_manifest(source, expected_dataset='ifs-pressure', allow_draft=True, bundle_dir=draft.parent)
            self.assertEqual(len(list(draft.parent.glob('*.bin'))), 41)
            with self.assertRaises(FileExistsError): write_frame(paths[0], 'ifs-pressure', RUN, 0, planes)
            original = (draft.parent / source['files'][0]['filename']).read_bytes()
            raw = bytearray(original); raw[0] = 0; (draft.parent / source['files'][0]['filename']).write_bytes(raw)
            with self.assertRaises(ContractError): validate_manifest(source, expected_dataset='ifs-pressure', allow_draft=True, bundle_dir=draft.parent)

    def test_gfs_weekly_capacity_has_headroom_and_does_not_delete_immutable_assets(self):
        source = manifest(); generation = source['generation']; tag = asset_shard_tag('gfs-atmos', generation)
        self.assertEqual(29 * 4 * 7, 812)
        self.assertEqual(41 * 2 * 7, 574)
        inventory = [{'name': f'{generation}-h{i:03d}.bin', 'size': file_bytes('gfs-atmos')} for i in range(29)]
        validate_shard_inventory('gfs-atmos', tag, inventory, source['files'])
        with self.assertRaisesRegex(ContractError, 'capacity'):
            validate_shard_inventory('gfs-atmos', tag, inventory, source['files'], maximum_assets=28)


class AtmosOriginalScienceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.values = array('f', [101200]) * (1440 * 721)

    def test_samples_exact_nodes_and_keeps_hemispheres_and_dateline(self):
        values = self.values[:]
        values[720 * 1440 + 720] = 99000  # -90,-180
        values[0] = 102000  # +90,0
        output, mask = sample_original(values, original_metadata(), 'gfs-atmos', 'pressure', RUN, 0)
        self.assertEqual(output[0], 99000)
        self.assertEqual(output[180 * 360 + 180], 102000)
        self.assertEqual(sum(mask), CELLS)

    def test_original_model_level_units_run_and_scanning_must_all_match(self):
        edits = {'centre': 98, 'Ni': 360, 'gridType': 'reduced_gg', 'jScansPositively': 1,
                 'units': 'hPa', 'parameterNumber': 0, 'typeOfFirstFixedSurface': 1,
                 'dataTime': 600, 'endStep': 6, 'validityTime': 600, 'stepType': 'avg'}
        for key, value in edits.items():
            with self.subTest(key=key):
                metadata = original_metadata(); metadata[key] = value
                with self.assertRaisesRegex(ContractError, 'mismatch'):
                    sample_original(self.values, metadata, 'gfs-atmos', 'pressure', RUN, 0)

    def test_missing_bitmap_is_explicit_and_not_a_legitimate_9999_visibility(self):
        values = array('f', [9999]) * (1440 * 721)
        output, mask = sample_original(values, original_metadata('visibility'), 'gfs-atmos', 'visibility', RUN, 0)
        self.assertEqual(output[0], 9999); self.assertEqual(mask[0], 1)
        metadata = original_metadata(); metadata['bitmapPresent'] = 1
        values = self.values[:]; values[720 * 1440 + 720] = 9999
        output, mask = sample_original(values, metadata, 'gfs-atmos', 'pressure', RUN, 0)
        self.assertEqual(output[0], 0); self.assertEqual(mask[0], 0)


class AtmosRangeTests(unittest.TestCase):
    def test_gfs_ranges_are_original_fields_not_accumulations_or_wrong_cycles(self):
        source = '\n'.join([
            '1:0:d=2026101700:PRMSL:mean sea level:anl:',
            '2:100:d=2026101700:VIS:surface:anl:',
            '3:200:d=2026101700:GUST:surface:anl:',
            '4:300:d=2026101700:TMP:2 m above ground:anl:',
            '5:400:d=2026101700:DPT:2 m above ground:anl:',
            '6:500:d=2026101700:TMP:surface:anl:',
        ])
        self.assertEqual(gfs_ranges(source, RUN, 0)['gust'], (200, 299))
        for bad in [source.replace('anl', '0-6 hour ave fcst'), source.replace('2026101700', '2026101706'), source.replace('2:100:', '2:0:')]:
            with self.assertRaises(ContractError): gfs_ranges(bad, RUN, 0)

    def test_ifs_range_does_not_accept_ensemble_aifs_or_wrong_valid_time(self):
        source = dict(param='msl', levtype='sfc', date='20261017', time='0000', step='6', stream='oper', type='fc', _offset=100, _length=200)
        self.assertEqual(ifs_range(json.dumps(source), RUN, 6), (100, 299))
        for key, value in [('type', 'pf'), ('stream', 'enfo'), ('step', '12'), ('date', '20261016')]:
            bad = dict(source); bad[key] = value
            with self.assertRaises(ContractError): ifs_range(json.dumps(bad), RUN, 6)
        with self.assertRaises(ContractError): ifs_range(json.dumps(source) + '\n' + json.dumps(source), RUN, 6)

    def test_bounded_http_refuses_ignored_ranges_redirects_truncation_and_encoded_bytes(self):
        url = f'https://{GFS_HOST}/fixture.grib2'
        class Response(io.BytesIO):
            status = 206
            headers = {'Content-Length': '10', 'Content-Range': 'bytes 20-29/100'}
            def geturl(self): return url
        class Opener:
            def __init__(self, response): self.response = response
            def open(self, request, timeout): return self.response
        self.assertEqual(DownloadBudget(opener=Opener(Response(b'0123456789'))).read(url, GFS_HOST, byte_range=(20, 29)), b'0123456789')
        for change in [lambda r: setattr(r, 'status', 200),
                       lambda r: setattr(r, 'headers', {'Content-Length': '10', 'Content-Range': 'bytes 0-9/100'}),
                       lambda r: setattr(r, 'headers', {'Content-Length': '10', 'Content-Range': 'bytes 20-29/100', 'Content-Encoding': 'gzip'}),
                       lambda r: setattr(r, 'geturl', lambda: 'https://lookalike.example/file')]:
            response = Response(b'0123456789'); change(response)
            with self.assertRaises(ContractError): DownloadBudget(opener=Opener(response)).read(url, GFS_HOST, byte_range=(20, 29))
        with self.assertRaises(ContractError): DownloadBudget(opener=Opener(Response(b'0123'))).read(url, GFS_HOST, byte_range=(20, 29))
        with self.assertRaises(ContractError): DownloadBudget().read('https://' + GFS_HOST + '.lookalike.example/a', GFS_HOST)

    def test_generation_deadline_total_bytes_and_requests_do_not_reset(self):
        now = [0]
        budget = DownloadBudget(clock=lambda: now[0]); now[0] = 1201
        with self.assertRaisesRegex(ContractError, 'deadline'): budget.check()
        budget = DownloadBudget(); budget.bytes = MAX_TOTAL_BYTES + 1
        with self.assertRaisesRegex(ContractError, 'byte budget'): budget.check()
        budget = DownloadBudget(); budget.requests = 210
        with self.assertRaisesRegex(ContractError, 'request budget'): budget.read(f'https://{GFS_HOST}/fixture.idx', GFS_HOST)


if __name__ == '__main__':
    unittest.main()
