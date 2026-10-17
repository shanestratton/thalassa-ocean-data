"""Metadata admission ordering with a mocked native API; no provider/native I/O."""
from __future__ import annotations

import sys
import types
import unittest
from array import array
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from atmos_pipeline import decode_message
from publisher_contract import ContractError
from test_atmos import RUN, original_metadata

RAW = b'GRIB' + bytes(4) + (20).to_bytes(8, 'big') + b'7777'


def native_api(metadata, values=None):
    handle = object()
    get = lambda _handle, key: metadata[key]
    return types.SimpleNamespace(
        codes_new_from_message=Mock(return_value=handle),
        codes_get_long=get, codes_get_double=get, codes_get_string=get,
        codes_get_values=Mock(return_value=values), codes_release=Mock(),
    ), handle


class AtmosMetadataAdmissionTests(unittest.TestCase):
    def test_wrong_grid_run_units_level_bitmap_never_expand_native_values(self):
        changes = [
            {'gridType': 'reduced_gg'}, {'Ni': 1441}, {'Nj': 722},
            {'numberOfPoints': 1}, {'iDirectionIncrementInDegrees': 1},
            {'latitudeOfFirstGridPointInDegrees': 89.75}, {'iScansNegatively': 1},
            {'centre': 98}, {'dataDate': 20261016}, {'validityTime': 600},
            {'units': 'hPa'}, {'level': 10}, {'typeOfFirstFixedSurface': 103},
            {'parameterNumber': 0}, {'stepType': 'avg'}, {'stepUnits': 0},
            {'bitmapPresent': None}, {'bitmapPresent': 2},
        ]
        for change in changes:
            with self.subTest(change=change):
                metadata = {**original_metadata(), 'paramId': 151, **change}
                api, handle = native_api(metadata)
                with patch.dict(sys.modules, {'eccodes': api}):
                    with self.assertRaises(ContractError):
                        decode_message(RAW, 'gfs-atmos', 'pressure', RUN, 0)
                api.codes_get_values.assert_not_called()
                api.codes_release.assert_called_once_with(handle)

    def test_admitted_original_still_expands_once_and_preserves_samples(self):
        api, handle = native_api({**original_metadata(), 'paramId': 151},
                                 array('d', [101200]) * (1440 * 721))
        with patch.dict(sys.modules, {'eccodes': api}):
            values, mask = decode_message(RAW, 'gfs-atmos', 'pressure', RUN, 0)
        api.codes_get_values.assert_called_once_with(handle)
        api.codes_release.assert_called_once_with(handle)
        self.assertEqual(len(values), 360 * 181)
        self.assertEqual(values[0], 101200)
        self.assertEqual(sum(mask), 360 * 181)

    def test_admitted_metadata_does_not_relax_value_cardinality_or_physical_guards(self):
        for values in [array('d', [101200]), array('d', [1e8]) * (1440 * 721)]:
            api, handle = native_api({**original_metadata(), 'paramId': 151}, values)
            with patch.dict(sys.modules, {'eccodes': api}):
                with self.assertRaises(ContractError):
                    decode_message(RAW, 'gfs-atmos', 'pressure', RUN, 0)
            api.codes_get_values.assert_called_once_with(handle)
            api.codes_release.assert_called_once_with(handle)

    def test_metadata_read_failure_still_releases_the_native_handle_without_values(self):
        api, handle = native_api({})
        with patch.dict(sys.modules, {'eccodes': api}):
            with self.assertRaises(KeyError):
                decode_message(RAW, 'gfs-atmos', 'pressure', RUN, 0)
        api.codes_get_values.assert_not_called()
        api.codes_release.assert_called_once_with(handle)


if __name__ == '__main__':
    unittest.main()
