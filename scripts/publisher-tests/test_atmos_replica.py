"""Explicit official IFS replica selection; fixtures make no provider requests."""
import json
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from atmos_pipeline import IFS_HOST, generate, ifs_original_url, ifs_source_location
from atmos_contract import validate_frame
from prove_atmos_first_source import ProofBudget, prove_first_source
from publisher_contract import ContractError
from test_atmos import RUN
from test_atmos_first_source import (IFS_RUN, RAW, FixtureOpener, FixtureResponse,
                                     fixture_decoder, fixture_probe)

GOOGLE = 'https://storage.googleapis.com/ecmwf-open-data/'


class GoogleOpener(FixtureOpener):
    def open(self, request, timeout):
        url = request.full_url
        if not url.startswith(GOOGLE):
            raise AssertionError('Replica switched or bucket escaped')
        if request.get_header('Range'):
            return super().open(request, timeout)
        self.calls.append((url, None, timeout))
        offset = int(url.rsplit('/', 1)[-1].split('-')[1].removesuffix('h'))
        body = json.dumps({'param': 'msl', 'levtype': 'sfc', 'date': '20261016', 'time': '1200',
                           'step': str(offset), 'stream': 'oper', 'type': 'fc',
                           '_offset': 0, '_length': len(RAW)}).encode()
        return FixtureResponse(url, body)


class AtmosReplicaTests(unittest.TestCase):
    def test_default_aws_and_explicit_google_have_exact_original_paths(self):
        suffix = '20261016/12z/ifs/0p25/oper/20261016120000-240h-oper-fc.grib2'
        self.assertEqual(ifs_original_url(IFS_RUN, 240), (f'https://{IFS_HOST}/' + suffix, IFS_HOST))
        self.assertEqual(ifs_original_url(IFS_RUN, 240, 'google'), (GOOGLE + suffix, 'storage.googleapis.com'))
        for step in [True, -6, 1, 246, '240']:
            with self.subTest(step=step), self.assertRaises(ContractError):
                ifs_original_url(IFS_RUN, step, 'google')

    def test_arbitrary_hosts_buckets_and_non_string_profiles_are_refused_before_output(self):
        factory = Mock()
        with tempfile.TemporaryDirectory() as temporary:
            for i, source in enumerate(['https://example.com', '../other', 'google/other', True, None, [], {}]):
                out = Path(temporary) / str(i)
                with self.subTest(source=source), self.assertRaisesRegex(ContractError, 'unknown IFS source'):
                    prove_first_source(RUN, IFS_RUN, out, allow_provider_downloads=True,
                                       ifs_only=True, ifs_source=source, budget_factory=factory)
                self.assertFalse(out.exists())
        factory.assert_not_called()

    def test_explicit_google_retains_four_originals_two_native_checked_frames_and_fifth_read_hold(self):
        opener = GoogleOpener()
        budgets = []
        def factory(out):
            budget = ProofBudget(out, opener=opener)
            budgets.append(budget)
            return budget
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary) / 'proof'
            report = prove_first_source(RUN, IFS_RUN, out, allow_provider_downloads=True,
                ifs_only=True, ifs_source='google', now=RUN+timedelta(hours=5),
                budget_factory=factory, decoder=fixture_decoder, probe=fixture_probe)
            self.assertEqual(report['ifsSource'], 'google')
            self.assertEqual((report['requests'], len(report['originals']), len(report['frames']), len(report['fields'])),
                             (4, 4, 2, 2))
            self.assertEqual([r['offset'] for r in report['frames']], [0, 240])
            self.assertTrue(all(url.startswith(GOOGLE) and timeout == 20 for url, _, timeout in opener.calls))
            self.assertFalse(report['publication'])
            self.assertFalse(report['completeCohort'])
            for frame in report['frames']:
                validate_frame(out/frame['path'], 'ifs-pressure', IFS_RUN, frame['offset'])
            with self.assertRaisesRegex(ContractError, 'request budget'):
                budgets[0].read_original('fifth', GOOGLE+'not-read.index', 'storage.googleapis.com')
            self.assertEqual(len(opener.calls), 4)

    def test_selected_replica_failure_stops_without_trying_another_source(self):
        class RefusingGoogle(GoogleOpener):
            def open(self, request, timeout):
                self.calls.append((request.full_url, None, timeout))
                return FixtureResponse(request.full_url, b'fixture refusal', status=503)
        opener = RefusingGoogle()
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary) / 'refused'
            with self.assertRaisesRegex(ContractError, 'status mismatch'):
                prove_first_source(RUN, IFS_RUN, out, allow_provider_downloads=True,
                    ifs_only=True, ifs_source='google', now=RUN+timedelta(hours=5),
                    budget_factory=lambda p: ProofBudget(p, opener=opener),
                    decoder=fixture_decoder, probe=fixture_probe)
            result = json.loads((out/'result.json').read_text())
            self.assertEqual((result['requests'], result['bodyBytes'], result['frames'], result['fields']), (1, 0, [], []))
            self.assertEqual(result['originals'][0]['status'], 503)
            self.assertEqual(result['ifsSource'], 'google')
            self.assertEqual(len(opener.calls), 1)
            self.assertTrue(opener.calls[0][0].startswith(GOOGLE))

    def test_full_ifs_generator_uses_same_explicit_replica_before_first_field(self):
        class FirstReadObserved(Exception): pass
        calls = []
        class Budget:
            def check(self): pass
            def read(self, url, host, *, byte_range=None):
                calls.append((url, host, byte_range))
                raise FirstReadObserved()
        with tempfile.TemporaryDirectory() as temporary, patch('atmos_pipeline.datetime') as clock, \
                patch('atmos_pipeline.producer_provenance', return_value={}):
            clock.now.return_value = IFS_RUN+timedelta(hours=5)
            with self.assertRaises(FirstReadObserved):
                generate('ifs-pressure', IFS_RUN, Path(temporary)/'first', budget=Budget(), ifs_source='google')
        self.assertEqual(calls, [(GOOGLE+'20261016/12z/ifs/0p25/oper/20261016120000-0h-oper-fc.index',
                                 'storage.googleapis.com', None)])

    def test_ifs_selection_cannot_change_or_silently_apply_to_gfs_generation(self):
        self.assertEqual(ifs_source_location('aws'), (IFS_HOST, ''))
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)/'gfs'
            with self.assertRaisesRegex(ContractError, 'cannot alter a GFS'):
                generate('gfs-atmos', RUN, out, budget=Mock(), ifs_source='google')
            self.assertFalse(out.exists())


if __name__ == '__main__':
    unittest.main()
