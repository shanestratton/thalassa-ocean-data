"""Instrumented, entirely fictional HTTP/native transport for one-shot bounds."""
from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from array import array
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from atmos_contract import CELLS, FIELDS, validate_frame
from atmos_pipeline import GFS_HOST, GFS_SELECTORS
from prove_atmos_first_source import (MAX_BODY_BYTES, MAX_DIRECTORY_BYTES, ProofBudget,
    SAMPLE_POINTS, DiagnosticMetadataRefused, native_source_probe, prove_first_source)
from publisher_contract import ContractError
from test_atmos import RUN, VALUES, original_metadata
from test_atmos_admission import RAW, native_api

IFS_RUN = RUN - timedelta(hours=12)


class FixtureResponse:
    def __init__(self, url, body, *, status=200, byte_range=None, truncate=False):
        self.url, self.status = url, status
        self.headers = {'Content-Length': str(len(body)), 'Content-Encoding': 'identity'}
        if byte_range:
            self.headers['Content-Range'] = f'bytes {byte_range[0]}-{byte_range[1]}/100000'
        self.body = io.BytesIO(body[:-1] if truncate else body)

    def geturl(self): return self.url
    def read(self, size): return self.body.read(size)
    def close(self): self.body.close()


class FixtureOpener:
    def __init__(self, *, bad_status=False, truncate=False):
        self.calls, self.bad_status, self.truncate = [], bad_status, truncate

    def open(self, request, timeout):
        self.calls.append((request.full_url, request.get_header('Range'), timeout))
        url, requested = request.full_url, request.get_header('Range')
        if requested:
            start, end = map(int, requested.removeprefix('bytes=').split('-'))
            return FixtureResponse(url, RAW, status=200 if self.bad_status else 206,
                                   byte_range=(start, end), truncate=self.truncate)
        if url.endswith('.idx'):
            offset = int(url.removesuffix('.idx')[-3:])
            valid = 'anl' if offset == 0 else f'{offset} hour fcst'
            rows = [f'{i+1}:{i*20}:d=2026101700:{variable}:{level}:{valid}:'
                    for i, (variable, level) in enumerate(GFS_SELECTORS.values())]
            rows.append(f'6:100:d=2026101700:HGT:surface:{valid}:')
            body = '\n'.join(rows).encode()
        else:
            offset = int(url.rsplit('/', 1)[-1].split('-')[1].removesuffix('h'))
            body = json.dumps({'param': 'msl', 'levtype': 'sfc', 'date': '20261016', 'time': '1200',
                               'step': str(offset), 'stream': 'oper', 'type': 'fc',
                               '_offset': 0, '_length': 20}).encode()
        return FixtureResponse(url, body)


def fixture_probe(_raw, _dataset, field, _run, _offset):
    return {'metadata': {'units': FIELDS[field][1], 'fixtureOnly': True},
            'samples': [{'longitude': lon, 'latitude': lat, 'sourceIndex': 0,
                         'displayIndex': (lat+90)*360+lon+180,
                         'originalValue': VALUES[field], 'unknown': False} for lon, lat in SAMPLE_POINTS]}


def fixture_decoder(_raw, _dataset, field, _run, _offset):
    return array('f', [VALUES[field]]) * CELLS, bytes([1]) * CELLS


class AtmosFirstSourceProofTests(unittest.TestCase):
    def test_complete_fixture_retains_exact_sixteen_originals_and_four_frames(self):
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary) / 'proof'
            opener = FixtureOpener()
            report = prove_first_source(RUN, IFS_RUN, out, allow_provider_downloads=True,
                now=RUN+timedelta(hours=5), budget_factory=lambda p: ProofBudget(p, opener=opener),
                decoder=fixture_decoder, probe=fixture_probe)
            self.assertEqual(report['state'], 'observed')
            self.assertEqual((report['requests'], len(report['originals']), len(report['frames'])), (16, 16, 4))
            self.assertFalse(report['completeCohort'])
            self.assertFalse(report['publication'])
            self.assertEqual(sum(1 for _, header, _ in opener.calls if header), 12)
            self.assertTrue(all(timeout == 20 for _, _, timeout in opener.calls))
            for original in report['originals']:
                from hashlib import sha256
                raw = (out / original['bodyPath']).read_bytes()
                self.assertEqual((len(raw), sha256(raw).hexdigest()), (original['bytes'], original['sha256']))
                self.assertTrue(original['readCompleted'])
                self.assertTrue((out / original['bodyPath'].replace('.body', '.headers.json')).is_file())
            for frame in report['frames']:
                validate_frame(out / frame['path'], frame['dataset'], RUN if frame['dataset']=='gfs-atmos' else IFS_RUN, frame['offset'])
            self.assertLess(sum(p.stat().st_size for p in out.rglob('*') if p.is_file()), MAX_DIRECTORY_BYTES)
            self.assertFalse((out / 'bundle').exists())
            self.assertEqual(len(list(out.glob('diagnostic-*.bin'))), 4)

    def test_consent_and_explicit_current_cycles_precede_any_output_or_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            for i, change in enumerate([{'allow_provider_downloads': False},
                                       {'now': RUN+timedelta(hours=13)},
                                       {'gfs_run': RUN+timedelta(hours=6)},
                                       {'ifs_run': RUN}]):
                out = Path(temporary) / str(i)
                options = dict(gfs_run=RUN, ifs_run=IFS_RUN, out=out,
                               allow_provider_downloads=True, now=RUN+timedelta(hours=5))
                options.update(change)
                with self.assertRaises(ContractError): prove_first_source(**options)
                self.assertFalse(out.exists())

    def test_request_body_deadline_and_directory_quotas_hold_before_another_open(self):
        with tempfile.TemporaryDirectory() as temporary:
            for label in ['requests', 'body', 'deadline', 'directory']:
                out = Path(temporary) / label
                out.mkdir()
                now = [0]
                opener = FixtureOpener()
                budget = ProofBudget(out, opener=opener, clock=lambda: now[0])
                if label == 'requests': budget.requests = 16
                if label == 'body': budget.body_bytes = MAX_BODY_BYTES
                if label == 'deadline': now[0] = 601
                if label == 'directory': budget.directory_bytes = MAX_DIRECTORY_BYTES
                with self.assertRaises(ContractError):
                    budget.read_original('fixture-index', f'https://{GFS_HOST}/fixture.idx', GFS_HOST)
                self.assertEqual(opener.calls, [])

    def test_native_probe_admits_before_indexed_expansion_and_releases(self):
        for changed in [True, False]:
            metadata = {**original_metadata(), 'paramId': 151}
            if changed: metadata['Ni'] = 1441
            api, handle = native_api(metadata)
            api.codes_get_double_elements = Mock(return_value=[101200] * len(SAMPLE_POINTS))
            with patch.dict(sys.modules, {'eccodes': api}):
                if changed:
                    with self.assertRaises(DiagnosticMetadataRefused) as error:
                        native_source_probe(RAW, 'gfs-atmos', 'pressure', RUN, 0)
                    self.assertEqual(error.exception.metadata['Ni'], 1441)
                    api.codes_get_double_elements.assert_not_called()
                else:
                    proof = native_source_probe(RAW, 'gfs-atmos', 'pressure', RUN, 0)
                    self.assertEqual(proof['samples'][0]['displayIndex'], 0)
                    api.codes_get_double_elements.assert_called_once()
            api.codes_get_values.assert_not_called()
            api.codes_release.assert_called_once_with(handle)

    def test_changed_original_sample_or_mask_refuses_a_diagnostic_frame(self):
        with tempfile.TemporaryDirectory() as temporary:
            for changed_mask in [False, True]:
                out = Path(temporary) / str(changed_mask)
                def changed_probe(*args):
                    value = fixture_probe(*args)
                    if changed_mask: value['samples'][0]['unknown'] = True
                    else: value['samples'][0]['originalValue'] += 1
                    return value
                with self.assertRaisesRegex(ContractError, 'original-node'):
                    prove_first_source(RUN, IFS_RUN, out, allow_provider_downloads=True,
                        now=RUN+timedelta(hours=5), budget_factory=lambda p: ProofBudget(p, opener=FixtureOpener()),
                        decoder=fixture_decoder, probe=changed_probe)
                report = json.loads((out / 'result.json').read_text())
                self.assertEqual(report['state'], 'failed')
                self.assertEqual(len(list(out.glob('diagnostic-*.bin'))), 0)
                self.assertEqual(len(report['originals']), 2)

    def test_truncated_original_preserves_admitted_prefix_and_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary) / 'truncated'
            decode = Mock(side_effect=AssertionError('truncated bytes reached decoder'))
            with self.assertRaisesRegex(ContractError, 'truncated'):
                prove_first_source(RUN, IFS_RUN, out, allow_provider_downloads=True,
                    now=RUN+timedelta(hours=5), budget_factory=lambda p: ProofBudget(p, opener=FixtureOpener(truncate=True)),
                    decoder=decode, probe=fixture_probe)
            decode.assert_not_called()
            report = json.loads((out / 'result.json').read_text())
            self.assertFalse(report['originals'][-1]['readCompleted'])
            self.assertEqual((out / report['originals'][-1]['bodyPath']).stat().st_size, len(RAW)-1)

    def test_http_failure_is_held_without_decoder_or_source_substitution(self):
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary) / 'wrong-status'
            decode = Mock(side_effect=AssertionError('wrong status reached decoder'))
            with self.assertRaisesRegex(ContractError, 'status'):
                prove_first_source(RUN, IFS_RUN, out, allow_provider_downloads=True,
                    now=RUN+timedelta(hours=5), budget_factory=lambda p: ProofBudget(p, opener=FixtureOpener(bad_status=True)),
                    decoder=decode, probe=fixture_probe)
            decode.assert_not_called()
            report = json.loads((out / 'result.json').read_text())
            self.assertEqual(report['requests'], 2)
            self.assertEqual(report['originals'][-1]['bytes'], 0)
            self.assertFalse(report['originals'][-1]['readCompleted'])
            self.assertEqual(report['frames'], [])

    def test_default_cli_refuses_provider_reads_without_consent(self):
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary) / 'refused'
            command = [sys.executable, str(Path(__file__).resolve().parents[1] / 'prove_atmos_first_source.py'),
                       '--gfs-run', '2026-10-17T00:00:00Z', '--ifs-run', '2026-10-16T12:00:00Z', '--out', str(out)]
            result = subprocess.run(command, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 2)
            self.assertIn('provider reads are held', result.stderr)
            self.assertFalse(out.exists())


if __name__ == '__main__':
    unittest.main()
