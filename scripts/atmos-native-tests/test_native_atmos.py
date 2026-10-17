"""Actual ecCodes round-trip using locally generated samples; no provider I/O.

Run with the atmospheric hash-locked Linux dependencies installed. This proof is
separate from the existing CMEMS dependency closure, not skipped when unavailable.
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

import eccodes
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from atmos_pipeline import GFS_PARAMETERS, decode_message
from publisher_contract import ContractError

RUN = datetime(2026, 10, 17, tzinfo=timezone.utc)
VALUES = {'gust': 12, 'visibility': 5000, 'pressure': 101200, 'temperature': 290, 'dewpoint': 289}


def message(field='pressure', dataset='gfs-atmos', offset=0, masked=False):
    handle = eccodes.codes_grib_new_from_samples('regular_ll_sfc_grib2')
    try:
        pairs = dict(centre=7 if dataset == 'gfs-atmos' else 98,
                     Ni=1440, Nj=721, iDirectionIncrementInDegrees=.25,
                     jDirectionIncrementInDegrees=.25,
                     latitudeOfFirstGridPointInDegrees=90, latitudeOfLastGridPointInDegrees=-90,
                     longitudeOfFirstGridPointInDegrees=0 if dataset == 'gfs-atmos' else 180,
                     longitudeOfLastGridPointInDegrees=359.75 if dataset == 'gfs-atmos' else 179.75,
                     iScansNegatively=0, jScansPositively=0, jPointsAreConsecutive=0,
                     alternativeRowScanning=0, dataDate=20261017, dataTime=0,
                     stepUnits=1, forecastTime=offset)
        for key, value in pairs.items(): eccodes.codes_set(handle, key, value)
        if dataset == 'gfs-atmos':
            keys = ('discipline', 'parameterCategory', 'parameterNumber', 'typeOfFirstFixedSurface', 'level')
            for key, value in zip(keys, GFS_PARAMETERS[field]): eccodes.codes_set_long(handle, key, value)
        else:
            eccodes.codes_set_long(handle, 'paramId', 151)
            eccodes.codes_set_long(handle, 'typeOfFirstFixedSurface', 101)
            eccodes.codes_set_long(handle, 'level', 0)
        values = np.full(1440 * 721, VALUES[field], dtype=np.float64)
        if masked:
            eccodes.codes_set_long(handle, 'bitmapPresent', 1)
            eccodes.codes_set_double(handle, 'missingValue', 9999)
            values[720 * 1440 + 720] = 9999
        eccodes.codes_set_values(handle, values)
        return eccodes.codes_get_message(handle)
    finally:
        eccodes.codes_release(handle)


class NativeAtmosTests(unittest.TestCase):
    def test_actual_decoder_all_five_gfs_fields_and_units(self):
        for field in VALUES:
            with self.subTest(field=field):
                values, mask = decode_message(message(field), 'gfs-atmos', field, RUN, 0)
                self.assertEqual(values[0], VALUES[field])
                self.assertEqual(mask[0], 1)

    def test_actual_decoder_ifs_pressure_and_clock(self):
        raw = message(dataset='ifs-pressure', offset=6)
        values, mask = decode_message(raw, 'ifs-pressure', 'pressure', RUN, 6)
        self.assertEqual(values[0], 101200)
        self.assertEqual(mask[0], 1)
        with self.assertRaises(ContractError): decode_message(raw, 'ifs-pressure', 'pressure', RUN, 0)
        with self.assertRaises(ContractError): decode_message(raw, 'gfs-atmos', 'pressure', RUN, 6)

    def test_actual_decoder_preserves_bitmap_unknown(self):
        values, mask = decode_message(message(masked=True), 'gfs-atmos', 'pressure', RUN, 0)
        self.assertEqual(values[0], 0)
        self.assertEqual(mask[0], 0)
        self.assertEqual(values[1], 101200)
        self.assertEqual(mask[1], 1)


if __name__ == '__main__':
    print('Actual ecCodes API:', eccodes.codes_get_api_version())
    unittest.main()
