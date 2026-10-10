from __future__ import annotations

import importlib.util
import math
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
MODULE_PATH = REPO_ROOT / "scripts/cmems-waves-pipeline/pipeline.py"
SPEC = importlib.util.spec_from_file_location("waves_pipeline_under_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
waves = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(waves)


class CircularWaveMathTests(unittest.TestCase):
    def assert_vector(self, direction: float, expected_u: float, expected_v: float) -> None:
        u, v = waves.circular_wave_vector([2.0], [direction])
        self.assertAlmostEqual(u, expected_u, places=6)
        self.assertAlmostEqual(v, expected_v, places=6)
        self.assertAlmostEqual(math.hypot(u, v), 2.0, places=6)

    def test_cardinal_from_directions_project_to_opposite_travel_direction(self) -> None:
        self.assert_vector(0.0, 0.0, -2.0)
        self.assert_vector(90.0, -2.0, 0.0)
        self.assert_vector(180.0, 0.0, 2.0)
        self.assert_vector(270.0, 2.0, 0.0)

    def test_wraparound_359_and_1_average_to_zero_not_180(self) -> None:
        u, v = waves.circular_wave_vector([2.0, 2.0], [359.0, 1.0])
        self.assertAlmostEqual(u, 0.0, places=6)
        self.assertLess(v, -1.999)
        self.assertAlmostEqual(math.hypot(u, v), 2.0, places=6)

    def test_height_weighting_preserves_mean_height_as_vector_magnitude(self) -> None:
        u, v = waves.circular_wave_vector([3.0, 1.0], [0.0, 180.0])
        self.assertAlmostEqual(u, 0.0, places=6)
        self.assertAlmostEqual(v, -2.0, places=6)
        self.assertAlmostEqual(math.hypot(u, v), 2.0, places=6)

    def test_exact_antipodal_tie_is_stable_under_permutation(self) -> None:
        expected = waves.circular_wave_vector([2.0, 2.0], [0.0, 180.0])
        permuted = waves.circular_wave_vector([2.0, 2.0], [180.0, 0.0])
        self.assertAlmostEqual(expected[0], 0.0, places=6)
        self.assertAlmostEqual(expected[1], -2.0, places=6)
        self.assertEqual(expected, permuted)

    def test_equal_maxima_use_lowest_normalized_bearing_not_array_order(self) -> None:
        expected = waves.circular_wave_vector([2.0, 2.0], [270.0, 90.0])
        permuted = waves.circular_wave_vector([2.0, 2.0], [90.0, 270.0])
        self.assertAlmostEqual(expected[0], -2.0, places=6)
        self.assertAlmostEqual(expected[1], 0.0, places=6)
        self.assertEqual(expected, permuted)

    def test_unique_tallest_system_wins_ambiguous_resultant_under_permutation(self) -> None:
        expected = waves.circular_wave_vector([2.0, 1.0, 1.0], [0.0, 180.0, 180.0])
        permuted = waves.circular_wave_vector([1.0, 2.0, 1.0], [180.0, 0.0, 180.0])
        self.assertAlmostEqual(expected[0], 0.0, places=6)
        self.assertAlmostEqual(expected[1], -(4.0 / 3.0), places=6)
        self.assertEqual(expected, permuted)

    def test_grid_fallback_uses_same_stable_tie_rule(self) -> None:
        import numpy as np

        heights = np.array([[2.0, 2.0], [1.0, 1.0]])
        directions = np.array([[270.0, 90.0], [0.0, 180.0]])
        selected = waves.dominant_direction_grid(heights, directions, 2, 2)
        self.assertEqual(selected.shape, (1, 1))
        self.assertEqual(float(selected[0, 0]), 90.0)


if __name__ == "__main__":
    unittest.main()
