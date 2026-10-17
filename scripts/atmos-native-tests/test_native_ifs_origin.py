"""Native IFS origin and captured original replay; all data is already local."""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import eccodes
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from atmos_contract import CELLS, validate_frame, write_frame
from atmos_pipeline import decode_message
from prove_atmos_first_source import native_source_probe, verify_samples
from test_native_atmos import RUN, message

INDEXES = [1036800, 519120, 1436, 675252]


class NativeIfsOriginTests(unittest.TestCase):
    def test_actual_ifs_origin_preserves_distinct_nodes_across_dateline_and_bitmap(self):
        handle = eccodes.codes_new_from_message(message(dataset='ifs-pressure'))
        try:
            self.assertEqual(eccodes.codes_get_double(handle, 'longitudeOfFirstGridPointInDegrees'), 180)
            self.assertEqual(eccodes.codes_get_double(handle, 'longitudeOfLastGridPointInDegrees'), 179.75)
            # Explicit native positions, independent of sampler/probe formulas.
            nodes = ((-180, -90, 1036800, 99000), (-179, -90, 1036804, 99001),
                     (0, 0, 519120, 101000), (179, 90, 1436, 102900),
                     (-1, 0, 519116, 100100), (153, -27, 675252, 100500),
                     (-180, 90, 0, 98000))
            values = np.full(1440 * 721, 101200, dtype=np.float64)
            for _, _, index, pressure in nodes: values[index] = pressure
            values[518404] = 9999  # Native (-179E, 0N), explicit bitmap unknown.
            eccodes.codes_set_long(handle, 'bitmapPresent', 1)
            eccodes.codes_set_double(handle, 'missingValue', 9999)
            eccodes.codes_set_long(handle, 'bitsPerValue', 24)
            eccodes.codes_set_values(handle, values)
            raw = eccodes.codes_get_message(handle)
        finally:
            eccodes.codes_release(handle)
        plane, mask = decode_message(raw, 'ifs-pressure', 'pressure', RUN, 0)
        original = native_source_probe(raw, 'ifs-pressure', 'pressure', RUN, 0)
        verify_samples((plane, mask), original)
        self.assertEqual([sample['sourceIndex'] for sample in original['samples']], INDEXES)
        self.assertEqual([sample['originalValue'] for sample in original['samples']],
                         [99000, 101000, 102900, 100500])
        for lon, lat, _, pressure in nodes:
            i = (lat + 90) * 360 + lon + 180
            self.assertEqual((plane[i], mask[i]), (pressure, 1))
        self.assertEqual((plane[32401], mask[32401]), (0, 0))
        self.assertEqual(sum(mask), CELLS - 1)

    def test_captured_original_ifs_pressure_replays_with_exact_metadata_nodes_and_frame(self):
        fixture = Path(__file__).parent / 'fixtures' / 'ifs-msl-20261016T1200Z-f000.grib2'
        provenance = json.loads(fixture.with_suffix('.json').read_text())
        raw = fixture.read_bytes()
        self.assertEqual(len(raw), 525958)
        self.assertEqual(hashlib.sha256(raw).hexdigest(),
                         '599ae5df95354ad5b2070bb853762ff7c8111d6dec0acb25d9af5c705e4e696e')
        self.assertEqual(provenance['sha256'], hashlib.sha256(raw).hexdigest())
        self.assertEqual(provenance['firstObservationRun'], 39011834081)
        run = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
        original = native_source_probe(raw, 'ifs-pressure', 'pressure', run, 0)
        self.assertEqual(original['metadata'], provenance['originalMetadataAtRefusal'])
        self.assertEqual([sample['sourceIndex'] for sample in original['samples']], INDEXES)
        plane = decode_message(raw, 'ifs-pressure', 'pressure', run, 0)
        verify_samples(plane, original)
        self.assertEqual(sum(plane[1]), CELLS)
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / 'historical-ifs-diagnostic.bin'
            write_frame(target, 'ifs-pressure', run, 0, {'pressure': plane})
            validate_frame(target, 'ifs-pressure', run, 0)
            frame_hash = hashlib.sha256(target.read_bytes()).hexdigest()
        print('Historical IFS replay:', json.dumps({
            'originalSha256': hashlib.sha256(raw).hexdigest(), 'run': provenance['run'],
            'offsetHours': 0, 'sourceIndexes': INDEXES, 'samples': original['samples'],
            'validCells': sum(plane[1]), 'diagnosticFrameSha256': frame_hash,
            'providerReads': 0, 'completeCohort': False, 'publication': False,
        }, allow_nan=False))


if __name__ == '__main__':
    unittest.main()
