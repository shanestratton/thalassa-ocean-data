"""Actual indexed-node diagnostic, isolated from every NetCDF process."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from atmos_pipeline import decode_message
from prove_atmos_first_source import native_source_probe, verify_samples
from test_native_atmos import RUN, VALUES, message


class NativeFirstSourceTests(unittest.TestCase):
    def test_actual_indexed_original_nodes_match_full_decoder_for_both_models(self):
        for dataset, offsets, fields in [('gfs-atmos', (0, 168), tuple(VALUES)),
                                         ('ifs-pressure', (0, 240), ('pressure',))]:
            for offset in offsets:
                for field in fields:
                    with self.subTest(dataset=dataset, offset=offset, field=field):
                        raw = message(field=field, dataset=dataset, offset=offset)
                        original = native_source_probe(raw, dataset, field, RUN, offset)
                        plane = decode_message(raw, dataset, field, RUN, offset)
                        verify_samples(plane, original)
                        self.assertEqual(original['metadata']['endStep'], offset)
                        self.assertTrue(all(sample['originalValue'] == VALUES[field]
                                            and not sample['unknown']
                                            for sample in original['samples']))

    def test_actual_indexed_bitmap_unknown_matches_the_full_decoder_mask(self):
        raw = message(masked=True)
        original = native_source_probe(raw, 'gfs-atmos', 'pressure', RUN, 0)
        plane = decode_message(raw, 'gfs-atmos', 'pressure', RUN, 0)
        verify_samples(plane, original)
        self.assertTrue(original['samples'][0]['unknown'])
        self.assertEqual((plane[0][0], plane[1][0]), (0, 0))
        self.assertTrue(all(not sample['unknown'] for sample in original['samples'][1:]))


if __name__ == '__main__':
    unittest.main()
