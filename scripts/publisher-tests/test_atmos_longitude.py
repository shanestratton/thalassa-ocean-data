"""Original longitude-origin contracts; no native library or provider access."""
from __future__ import annotations

import sys
import unittest
from array import array
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from atmos_pipeline import decode_message, sample_original
from prove_atmos_first_source import native_source_probe, verify_samples
from publisher_contract import ContractError
from test_atmos import RUN, original_metadata
from test_atmos_admission import RAW, native_api

# Explicit original positions, independent of both implementation formulas.
# (longitude, latitude, GFS source index, IFS source index, known pressure Pa)
NODES = ((-180, -90, 1037520, 1036800, 99000),
         (-179, -90, 1037524, 1036804, 99001),
         (0, 0, 518400, 519120, 101000),
         (179, 90, 716, 1436, 102900),
         (-1, 0, 519836, 519116, 100100),
         (153, -27, 674532, 675252, 100500),
         (-180, 90, 720, 0, 98000))


def metadata(dataset):
    return {'discipline': 0, 'parameterCategory': 3, 'parameterNumber': 0,
            'paramId': 151, **original_metadata(dataset=dataset)}


class AtmosLongitudeOriginTests(unittest.TestCase):
    def test_declared_model_origin_and_endpoint_are_required_before_native_expansion(self):
        for dataset, wrong_pairs in [
            ('gfs-atmos', ((180, 179.75), (0, 359.5), (360, 359.75))),
            ('ifs-pressure', ((0, 359.75), (180, 179.5), (-180, 179.75))),
        ]:
            for first, last in wrong_pairs:
                with self.subTest(dataset=dataset, first=first, last=last):
                    wrong = {**metadata(dataset), 'longitudeOfFirstGridPointInDegrees': first,
                             'longitudeOfLastGridPointInDegrees': last}
                    api, handle = native_api(wrong)
                    with patch.dict(sys.modules, {'eccodes': api}):
                        with self.assertRaises(ContractError):
                            decode_message(RAW, dataset, 'pressure', RUN, 0)
                    api.codes_get_values.assert_not_called()
                    api.codes_release.assert_called_once_with(handle)

    def test_distinct_original_nodes_and_bitmap_keep_geography_for_both_origins(self):
        for dataset, index_position, missing_index in [('gfs-atmos', 2, 519124),
                                                        ('ifs-pressure', 3, 518404)]:
            with self.subTest(dataset=dataset):
                values = array('d', [101200]) * (1440 * 721)
                for node in NODES:
                    values[node[index_position]] = node[4]
                values[missing_index] = 9999  # (-179E, 0N), never a fabricated pressure
                original = {**metadata(dataset), 'bitmapPresent': 1}
                plane, mask = sample_original(values, original, dataset, 'pressure', RUN, 0)
                for lon, lat, _, _, pressure in NODES:
                    output = (lat + 90) * 360 + lon + 180
                    self.assertEqual((plane[output], mask[output]), (pressure, 1))
                self.assertEqual((plane[32401], mask[32401]), (0, 0))
                self.assertEqual(sum(mask), 360 * 181 - 1)

    def test_independent_native_probe_uses_exact_original_indexes_for_each_origin(self):
        for dataset, indexes in [('gfs-atmos', [1037520, 518400, 716, 674532]),
                                  ('ifs-pressure', [1036800, 519120, 1436, 675252])]:
            with self.subTest(dataset=dataset):
                values = array('d', [101200]) * (1440 * 721)
                selected = [99000, 101000, 102900, 100500]
                for index, value in zip(indexes, selected): values[index] = value
                original = metadata(dataset)
                api, handle = native_api(original)
                api.codes_get_double_elements = Mock(return_value=selected)
                with patch.dict(sys.modules, {'eccodes': api}):
                    proof = native_source_probe(RAW, dataset, 'pressure', RUN, 0)
                api.codes_get_double_elements.assert_called_once_with(handle, 'values', indexes)
                api.codes_get_values.assert_not_called()
                api.codes_release.assert_called_once_with(handle)
                self.assertEqual([sample['sourceIndex'] for sample in proof['samples']], indexes)
                verify_samples(sample_original(values, original, dataset, 'pressure', RUN, 0), proof)


if __name__ == '__main__':
    unittest.main()
