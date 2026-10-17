from __future__ import annotations

import copy
import importlib.util
import json
import math
import struct
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import publisher_contract as contract
import ssh_grid as ssh

try:
    import numpy as np
except ModuleNotFoundError:
    np = None

spec = importlib.util.spec_from_file_location('ssh_pipeline_under_test', SCRIPTS / 'cmems-ssh-pipeline/pipeline.py')
pipeline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pipeline)


@unittest.skipUnless(np is not None, 'Scientific fixtures execute with the CMEMS hash lock')
class SshGridTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.mask = np.zeros((ssh.HEIGHT, ssh.WIDTH), dtype=bool)
        self.mask[:, :ssh.WIDTH // 2] = True
        self.fields = [np.full(self.mask.shape, value, dtype=np.float32) for value in (-.2, .1, -.4, .3)]
        self.day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
        self.epoch = int(self.day.timestamp()) * 1000
        self.path = self.root / 'source.bin'
        ssh.write_ssh_grid(self.path, self.fields, self.mask, self.epoch)

    def tearDown(self):
        self.temp.cleanup()

    def change(self, offset, data):
        raw = bytearray(self.path.read_bytes())
        raw[offset:offset + len(data)] = data
        self.path.write_bytes(raw)

    def test_valid_signed_heights_and_vector(self):
        result = ssh.validate_ssh_payload(self.path)
        self.assertEqual(result['source_time_ms'], self.epoch)
        self.assertEqual(result['valid_cells'], ssh.CELLS // 2)
        self.assertEqual(self.path.stat().st_size, 4_406_464)

    def test_distinct_protocol_and_reserved_bytes(self):
        self.change(0, b'THCU')
        with self.assertRaises(ValueError):
            ssh.validate_ssh_payload(self.path)
        self.change(0, b'THSS')
        self.change(63, b'\x01')
        with self.assertRaises(ValueError):
            ssh.validate_ssh_payload(self.path)

    def test_exact_dimensions_size_and_original_time(self):
        self.change(8, struct.pack('<H', 721))
        with self.assertRaises(ValueError):
            ssh.validate_ssh_payload(self.path)
        self.change(8, struct.pack('<H', 720))
        self.change(44, struct.pack('<q', self.epoch + 1))
        with self.assertRaises(ValueError):
            ssh.validate_ssh_payload(self.path)

    def test_unknown_and_masked_values_are_not_silent(self):
        self.change(64 + ssh.CELLS * 16, b'\x02')
        with self.assertRaises(ValueError):
            ssh.validate_ssh_payload(self.path)
        self.change(64 + ssh.CELLS * 16, b'\x01')
        self.change(64 + 360 * 4, struct.pack('<f', 1))
        with self.assertRaises(ValueError):
            ssh.validate_ssh_payload(self.path)

    def test_nonfinite_and_nonphysical_never_publish(self):
        self.change(64, struct.pack('<f', math.nan))
        with self.assertRaises(ValueError):
            ssh.validate_ssh_payload(self.path)
        self.change(64, struct.pack('<f', 6))
        with self.assertRaises(ValueError):
            ssh.validate_ssh_payload(self.path)

    def test_zero_is_valid_height_and_current_when_observed(self):
        self.change(64, struct.pack('<f', 0))
        self.assertEqual(ssh.validate_ssh_payload(self.path)['valid_cells'], ssh.CELLS // 2)

    def test_stable_four_channel_ice_and_coast_mask(self):
        # Half the native globe is deliberately unknown. The finite coast cell
        # beside a single NaN/ice contributor must also remain unknown.
        value = np.broadcast_to(np.arange(2880, dtype=np.float32)[None, :] * .00001, (1440, 2880))
        ice = np.zeros((1440, 2880), dtype=np.float32)
        ice[:, 1440:] = 1
        ice[0, 0] = 1
        fields, mask = ssh.coarsen_duacs([value] * 4, ice)
        self.assertFalse(mask[0, 0])
        self.assertTrue(mask[0, 1])
        self.assertAlmostEqual(float(fields[0][0, 1]), float(value[0, 4:8].mean()), places=7)
        self.assertTrue(np.all(fields[0][~mask] == 0))
        self.assertEqual(mask.shape, (360, 720))
        self.assertEqual(int(mask.sum()), ssh.CELLS // 2 - 1)

    def test_manifest_round_trip_and_provenance(self):
        stamp = self.day.isoformat().replace('+00:00', 'Z')
        draft = contract.build_cmems_bundle(dataset_key='ssh', source_paths=[self.path], offsets_hours=[0],
                    data_times=[stamp], bundle_dir=self.root / 'bundle',
                    provenance={'commit': 'a' * 40, 'run_id': 123, 'run_attempt': 1}, metadata=ssh.SSH_METADATA)
        body = json.loads(draft.read_text())
        contract.validate_manifest(body, expected_dataset='ssh', allow_draft=True, bundle_dir=draft.parent)
        self.assertEqual(body['data_start'], body['data_end'])
        for mutate in [lambda b: b['metadata'].update(units='ft,ft,kts,kts'),
                       lambda b: b['metadata'].update(kind='forecast'),
                       lambda b: b['dimensions'].update(width=1440),
                       lambda b: b['files'][0].update(offset_hours=24),
                       lambda b: b['bounds'].update(west=-180),
                       lambda b: b['metadata'].update(dataset_version='demo')]:
            changed = copy.deepcopy(body)
            mutate(changed)
            with self.assertRaises(contract.ContractError):
                contract.validate_manifest(changed, expected_dataset='ssh', allow_draft=True)

    def catalog(self):
        return {'id': ssh.DATASET_ID + '_' + ssh.DATASET_VERSION, 'properties': {
            'end_datetime': self.day.isoformat().replace('+00:00', 'Z'),
            'cube:dimensions': {
                'time': {'step': 'P1D'},
                'latitude': {'type': 'spatial', 'axis': 'y', 'extent': [-89.9375, 89.9375], 'step': .125, 'reference_system': 4326},
                'longitude': {'type': 'spatial', 'axis': 'x', 'extent': [-179.9375, 179.9375], 'step': .125, 'reference_system': 4326}},
            'cube:variables': {**{key: {'unit': pipeline.UNITS[key], 'standardName': pipeline.STANDARDS[key],
                                      'dimensions': ['time', 'latitude', 'longitude']}
                                  for key in ssh.VARIABLES}, 'flag_ice': {'flagValues': [0, 1]}}}}

    def test_source_date_and_nondemo_unit_gate(self):
        body = self.catalog()
        self.assertEqual(pipeline.source_date(body, self.day + timedelta(days=1, hours=1)), self.day)
        for mutate in [lambda b: b['properties']['cube:variables']['adt'].update(unit='ft'),
                       lambda b: b['properties']['cube:variables']['sla'].update(standardName='depth'),
                       lambda b: b['properties']['cube:variables']['flag_ice'].update(flagValues=[1, 0]),
                       lambda b: b['properties']['cube:dimensions']['latitude'].update(step=.0625),
                       lambda b: b['properties']['cube:variables']['vgos'].update(dimensions=['time', 'longitude', 'latitude']),
                       lambda b: b['properties'].update(end_datetime='2026-10-16T00:00:00+01:00')]:
            changed = copy.deepcopy(body)
            mutate(changed)
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                pipeline.source_date(changed, self.day + timedelta(days=1))
        body['id'] = 'demo'
        with self.assertRaises(ValueError):
            pipeline.source_date(body, self.day + timedelta(days=1))

    def test_sdk_selection_uses_original_cell_centres_not_world_edges(self):
        raw = json.loads((SCRIPTS / 'publisher-tests/fixtures/ssh-original-zarr-metadata.json').read_text())['metadata']
        for axis in ['latitude', 'longitude']:
            attrs = raw[axis + '/.zattrs']
            self.assertEqual(pipeline.SUBSET_BOUNDS['minimum_' + axis], attrs['valid_min'])
            self.assertEqual(pipeline.SUBSET_BOUNDS['maximum_' + axis], attrs['valid_max'])

    def test_stale_observation_is_not_redated(self):
        with self.assertRaises(ValueError):
            pipeline.source_date(self.catalog(), self.day + timedelta(days=5))

    def test_freshness_has_no_future_forecast_exception(self):
        stamp = self.day.isoformat().replace('+00:00', 'Z')
        body = {'dataset': {'key': 'ssh'}, 'data_start': stamp, 'data_end': stamp,
                'generated_at': (self.day + timedelta(days=1)).isoformat().replace('+00:00', 'Z')}
        contract.validate_publication_freshness(body, self.day + timedelta(days=1, hours=1))
        with self.assertRaises(contract.ContractError):
            contract.validate_publication_freshness(body, self.day + timedelta(days=5))
        body['data_end'] = (self.day + timedelta(days=1)).isoformat().replace('+00:00', 'Z')
        with self.assertRaises(contract.ContractError):
            contract.validate_publication_freshness(body, self.day + timedelta(days=1, hours=1))


@unittest.skipUnless(np is not None, 'Real CF packing fixtures require the CMEMS hash lock')
class SshCfRoundTripTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import xarray as xr
        cls.xr = xr
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.day = datetime(2026, 10, 16, tzinfo=timezone.utc)
        raw = json.loads((SCRIPTS / 'publisher-tests/fixtures/ssh-original-zarr-metadata.json').read_text())['metadata']
        cls.original = raw
        shape = (1, 1440, 2880)
        plane = np.zeros((1440, 2880), dtype=np.float32)
        plane[:, 1440:] = np.nan
        variables = {}
        for key, value in zip(ssh.VARIABLES, (-.2, .1, -.4, .3)):
            attrs = {k: v for k, v in raw[key + '/.zattrs'].items() if k not in ('_ARRAY_DIMENSIONS', 'scale_factor')}
            variables[key] = (('time', 'latitude', 'longitude'), (plane + value).reshape(shape), attrs)
        ice = np.zeros(shape, dtype=np.int32)
        ice[:, :, 1440:] = 1
        ice[0, 0, 0] = 1
        variables['flag_ice'] = (('time', 'latitude', 'longitude'), ice,
                {k: v for k, v in raw['flag_ice/.zattrs'].items() if k != '_ARRAY_DIMENSIONS'})
        coords = {key: (key, values, {k: v for k, v in raw[key + '/.zattrs'].items() if k != '_ARRAY_DIMENSIONS'})
                  for key, values in [('latitude', -89.9375 + np.arange(1440) * .125),
                                      ('longitude', -179.9375 + np.arange(2880) * .125)]}
        coords['time'] = ('time', [np.datetime64('2026-10-16')])
        ds = xr.Dataset(variables, coords=coords)
        # Actual source permits coordinate-bound variables with this dimension.
        ds['lat_bnds'] = (('latitude', 'nv'), np.column_stack((ds.latitude.values - .0625, ds.latitude.values + .0625)))
        encoding = {key: {'dtype': 'int32', 'scale_factor': .0001, '_FillValue': -2147483647,
                          'zlib': True, 'complevel': 1} for key in ssh.VARIABLES}
        encoding['time'] = {'units': 'days since 1950-01-01', 'calendar': 'gregorian'}
        cls.path = cls.root / 'actual-cf-packed.nc'
        ds.to_netcdf(cls.path, engine='netcdf4', encoding=encoding)
        ds.close()

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_original_cf_packed_values_fill_ice_and_bounds_dimension(self):
        with self.xr.open_dataset(self.path, decode_cf=True) as ds:
            (fields, mask), epoch = pipeline.decode_native(ds, self.day)
            self.assertEqual(epoch, 1792108800000)
            self.assertEqual(dict(ds.sizes)['nv'], 2)
            self.assertEqual(int(mask.sum()), ssh.CELLS // 2 - 1)
            self.assertFalse(mask[0, 0])
            self.assertFalse(mask[0, 360])
            for field, expected in zip(fields, (-.2, .1, -.4, .3)):
                self.assertAlmostEqual(float(field[0, 1]), expected, places=6)
                self.assertTrue(np.all(field[~mask] == 0))

    def test_cf_wrong_units_identity_or_orientation_rejected(self):
        for key, attribute, wrong in [('latitude', 'units', 'radian'), ('longitude', 'axis', 'Y'),
                                       ('adt', 'units', 'ft'), ('vgos', 'standard_name', 'northward_wind')]:
            with self.subTest(key=key, attribute=attribute), self.xr.open_dataset(self.path, decode_cf=True) as ds:
                ds[key].attrs[attribute] = wrong
                with self.assertRaises(ValueError):
                    pipeline.decode_native(ds, self.day)
        with self.xr.open_dataset(self.path, decode_cf=True) as ds:
            with self.assertRaises(ValueError):
                pipeline.decode_native(ds.isel(latitude=slice(None, None, -1)), self.day)

    def test_cf_original_time_calendar_and_unreviewed_dimension_rejected(self):
        with self.xr.open_dataset(self.path, decode_cf=True) as ds:
            with self.assertRaises(ValueError):
                pipeline.decode_native(ds, self.day + timedelta(days=1))
            ds.time.encoding['calendar'] = '360_day'
            with self.assertRaises(ValueError):
                pipeline.decode_native(ds, self.day)
        with self.xr.open_dataset(self.path, decode_cf=True) as ds:
            ds['unexpected'] = ('depth', [1., 2., 3.])
            with self.assertRaises(ValueError):
                pipeline.decode_native(ds, self.day)


if __name__ == '__main__':
    unittest.main()
