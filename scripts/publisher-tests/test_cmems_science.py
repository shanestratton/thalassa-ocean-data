from __future__ import annotations

import importlib.util
import math
import sys
import unittest
from pathlib import Path

try:
    import numpy as np
except ModuleNotFoundError:  # MPA's deliberately minimal offline test environment.
    np = None

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import cmems_contract
from publisher_contract import ContractError

CHL_PATH = REPO_ROOT / "scripts/cmems-chl-pipeline/pipeline.py"
if np is not None:
    CHL_SPEC = importlib.util.spec_from_file_location("chl_pipeline_under_test", CHL_PATH)
    assert CHL_SPEC is not None and CHL_SPEC.loader is not None
    chl = importlib.util.module_from_spec(CHL_SPEC)
    CHL_SPEC.loader.exec_module(chl)
else:
    chl = None


@unittest.skipUnless(np is not None, "NumPy is installed only in CMEMS generation jobs")
class CmemsScientificContractTests(unittest.TestCase):
    OFFICIAL_UNITS = {
        "longitude": "degrees_east",
        "latitude": "degrees_north",
        "depth": "m",
        "time": "hours since 1950-01-01 00:00:00",
        "uo": "m s-1",
        "vo": "m s-1",
        "VHM0": "m",
        "VMDR": "degree",
        "thetao": "degrees_C",
        "chl": "mg m-3",
        "siconc": "1",
        "mlotst": "m",
    }

    WRONG_UNITS = {
        "longitude": "radian",
        "latitude": "radian",
        "depth": "km",
        "time": "days since 1950-01-01",
        "uo": "knots",
        "vo": "cm s-1",
        "VHM0": "ft",
        "VMDR": "radian",
        "thetao": "K",
        "chl": "kg m-3",
        "siconc": "%",
        "mlotst": "km",
    }

    @staticmethod
    def attrs_for(name: str, units: str) -> tuple[dict, dict]:
        spec = cmems_contract.CF_METADATA_CONTRACT[name]
        attrs = {"standard_name": spec["standard_name"]}
        for attribute in ("axis", "positive"):
            if attribute in spec:
                attrs[attribute] = spec[attribute]
        if name == "time":
            return attrs, {"units": units}
        attrs["units"] = units
        return attrs, {}

    def test_official_cf_metadata_is_accepted_for_every_field_and_axis(self) -> None:
        self.assertEqual(set(self.OFFICIAL_UNITS), set(cmems_contract.CF_METADATA_CONTRACT))
        for name, units in self.OFFICIAL_UNITS.items():
            attrs, encoding = self.attrs_for(name, units)
            with self.subTest(name=name):
                cmems_contract.validate_cf_metadata(name, attrs, encoding)
        # These are notation-only aliases, not numeric conversions.
        attrs, encoding = self.attrs_for("uo", "m s−1")
        cmems_contract.validate_cf_metadata("uo", attrs, encoding)
        attrs, encoding = self.attrs_for("chl", "MG.M-3")
        cmems_contract.validate_cf_metadata("chl", attrs, encoding)

    def test_convertible_but_unhandled_units_are_rejected_for_every_field_and_axis(self) -> None:
        self.assertEqual(set(self.WRONG_UNITS), set(cmems_contract.CF_METADATA_CONTRACT))
        for name, units in self.WRONG_UNITS.items():
            attrs, encoding = self.attrs_for(name, units)
            with self.subTest(name=name), self.assertRaisesRegex(ContractError, "unexpected units"):
                cmems_contract.validate_cf_metadata(name, attrs, encoding)

    def test_wrong_standard_names_and_coordinate_directions_are_rejected(self) -> None:
        for name, units in self.OFFICIAL_UNITS.items():
            attrs, encoding = self.attrs_for(name, units)
            attrs["standard_name"] = "not_the_expected_field"
            with self.subTest(name=name), self.assertRaisesRegex(ContractError, "standard_name"):
                cmems_contract.validate_cf_metadata(name, attrs, encoding)
        attrs, encoding = self.attrs_for("longitude", "degrees_east")
        attrs["axis"] = "Y"
        with self.assertRaisesRegex(ContractError, "axis"):
            cmems_contract.validate_cf_metadata("longitude", attrs, encoding)
        attrs, encoding = self.attrs_for("depth", "m")
        attrs["positive"] = "up"
        with self.assertRaisesRegex(ContractError, "positive"):
            cmems_contract.validate_cf_metadata("depth", attrs, encoding)

    def test_time_varying_nan_mask_is_rejected(self) -> None:
        reference = np.array([[False, True], [True, True]], dtype=bool)
        cmems_contract.require_invariant_finite_mask(reference, reference.copy(), "fixture")
        drifted = reference.copy()
        drifted[1, 1] = False
        with self.assertRaisesRegex(ContractError, "refusing to fabricate ocean values"):
            cmems_contract.require_invariant_finite_mask(reference, drifted, "fixture")

    def test_direction_fringe_superset_is_accepted_within_bound(self) -> None:
        reference = np.zeros((10, 10), dtype=bool)
        reference[2:8, 2:8] = True
        exact = reference.copy()
        cmems_contract.require_finite_superset_of_reference(exact, exact.copy(), "fixture", 0.05)
        fringe = reference.copy()
        fringe[1, 2] = True
        fringe[8, 5] = True
        cmems_contract.require_finite_superset_of_reference(reference, fringe, "fixture", 0.05)

    def test_direction_missing_over_ocean_is_rejected(self) -> None:
        reference = np.zeros((10, 10), dtype=bool)
        reference[2:8, 2:8] = True
        hole = reference.copy()
        hole[4, 4] = False
        with self.assertRaisesRegex(ContractError, "refusing to fabricate ocean values"):
            cmems_contract.require_finite_superset_of_reference(reference, hole, "fixture", 0.05)

    def test_direction_fringe_beyond_bound_is_rejected(self) -> None:
        reference = np.zeros((10, 10), dtype=bool)
        reference[2:8, 2:8] = True
        flooded = np.ones((10, 10), dtype=bool)
        with self.assertRaisesRegex(ContractError, "upstream mask changed"):
            cmems_contract.require_finite_superset_of_reference(reference, flooded, "fixture", 0.05)
        with self.assertRaisesRegex(ContractError, "finite-mask shape changed"):
            cmems_contract.require_finite_superset_of_reference(reference, np.ones((4, 4), dtype=bool), "fixture", 0.05)

    def test_reference_variable_cannot_be_declared_a_superset(self) -> None:
        with self.assertRaisesRegex(ContractError, "reference variable"):
            cmems_contract.validate_cmems_source(
                Path("/nonexistent.nc"),
                dataset_key="fixture",
                variables=["VHM0", "VMDR"],
                expected_steps=1,
                cadence_hours=3,
                native_resolution=1 / 12,
                finite_superset_variables=frozenset({"VHM0"}),
            )
        with self.assertRaisesRegex(ContractError, "must name declared variables"):
            cmems_contract.validate_cmems_source(
                Path("/nonexistent.nc"),
                dataset_key="fixture",
                variables=["VHM0", "VMDR"],
                expected_steps=1,
                cadence_hours=3,
                native_resolution=1 / 12,
                finite_superset_variables=frozenset({"thetao"}),
            )

    def test_chlorophyll_floor_midpoint_and_ceiling_are_exact(self) -> None:
        self.assertEqual(chl.normalise_chlorophyll_value(0.0), 0.0)
        self.assertEqual(chl.normalise_chlorophyll_value(0.01), 0.0)
        self.assertAlmostEqual(chl.normalise_chlorophyll_value(math.sqrt(0.01 * 50.0)), 0.5, places=12)
        self.assertEqual(chl.normalise_chlorophyll_value(50.0), 1.0)
        self.assertEqual(chl.normalise_chlorophyll_value(500.0), 1.0)
        with self.assertRaises(ValueError):
            chl.normalise_chlorophyll_value(math.nan)


if __name__ == "__main__":
    unittest.main()
