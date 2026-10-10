from __future__ import annotations

import copy
import json
import math
import struct
import sys
import tempfile
import unittest
from array import array
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import publisher_contract as contract


NOW = datetime(2026, 8, 5, 12, 0, 0, tzinfo=timezone.utc)
GRID = {
    "width": 1440,
    "height": 680,
    "bounds": {"north": 89.875, "south": -79.875, "west": -179.875, "east": 179.875},
}


def iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def cmems_manifest(dataset: str, *, draft: bool = False) -> dict:
    spec = contract.DATASETS[dataset]
    cadence = spec["cadence_hours"]
    start = NOW - timedelta(hours=1) if cadence < 24 else NOW.replace(hour=0)
    times = [start + timedelta(hours=index * cadence) for index in range(spec["steps"])]
    digests = [f"{index + 1:064x}" for index in range(spec["steps"])]
    generation = contract._generation(iso(start), digests, dataset)
    files = [
        {
            "step": index,
            "offset_hours": index * cadence,
            "data_time": iso(times[index]),
            "filename": f"{generation}-h{index:03d}.bin",
            "bytes": 8_812_830,
            "sha256": digests[index],
            "content_type": "application/octet-stream",
        }
        for index in range(spec["steps"])
    ]
    return {
        "schema_version": 2,
        "dataset": {"key": dataset, "id": spec["id"]},
        "generation": generation,
        "generated_at": iso(NOW - timedelta(minutes=2)),
        "published_at": None if draft else iso(NOW - timedelta(minutes=1)),
        "data_start": iso(start),
        "data_end": iso(times[-1]),
        "cadence_hours": cadence,
        "dimensions": {"width": GRID["width"], "height": GRID["height"]},
        "bounds": dict(GRID["bounds"]),
        "producer": {"commit": "a" * 40, "run_id": 123, "run_attempt": 1},
        "files": files,
        "metadata": {"attribution": "Copernicus Marine Service"},
    }


def mpa_manifest(*, draft: bool = False) -> dict:
    source_time = "2024-06-30T00:00:00Z"
    digest = "b" * 64
    generation = contract._generation(source_time, [digest], "mpa")
    return {
        "schema_version": 2,
        "dataset": {"key": "mpa", "id": contract.DATASETS["mpa"]["id"]},
        "generation": generation,
        "generated_at": iso(NOW - timedelta(minutes=2)),
        "published_at": None if draft else iso(NOW - timedelta(minutes=1)),
        "data_start": source_time,
        "data_end": source_time,
        "cadence_hours": None,
        "dimensions": {"feature_count": 4541},
        "bounds": {"west": 70.717, "east": 170.3667, "south": -58.4488, "north": -8.4738},
        "producer": {"commit": "c" * 40, "run_id": 456, "run_attempt": 2},
        "files": [{"step": 0, "filename": f"{generation}-mpa.geojson", "bytes": 6_641_680, "sha256": digest, "content_type": "application/geo+json"}],
        "metadata": {"classification_notice": "Indicative only; consult current zoning rules."},
    }


class ManifestContractTests(unittest.TestCase):
    def test_weekly_shard_derivation_and_iso_year_boundary(self) -> None:
        self.assertEqual(
            contract.asset_shard_tag("currents", "g-20260805T000000Z-0123456789ab"),
            "cmems-currents-latest-assets-2026-W32",
        )
        self.assertEqual(
            contract.asset_shard_tag("mpa", "g-20251231T235959Z-0123456789ab"),
            "mpa-aus-latest-assets-2026-W01",
        )
        with self.assertRaisesRegex(contract.ContractError, "invalid UTC"):
            contract.asset_shard_tag("currents", "g-20261340T250000Z-0123456789ab")

    def test_shard_capacity_and_collision_guard_runs_before_mutation(self) -> None:
        generation = "g-20260805T000000Z-0123456789ab"
        shard = contract.asset_shard_tag("currents", generation)
        candidates = [
            {"filename": f"{generation}-h{index:03d}.bin", "bytes": 100}
            for index in range(3)
        ]
        with self.assertRaisesRegex(contract.ContractError, "capacity"):
            contract.validate_shard_inventory("currents", shard, [], candidates, maximum_assets=2)
        with self.assertRaisesRegex(contract.ContractError, "size collision"):
            contract.validate_shard_inventory(
                "currents",
                shard,
                [{"name": candidates[0]["filename"], "size": 99}],
                candidates,
            )

    def test_bundle_layout_rejects_extra_entries_and_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            bundle = Path(name)
            asset = bundle / "asset.bin"
            draft = bundle / contract.DRAFT_MANIFEST_NAME
            asset.write_bytes(b"data")
            draft.write_text("{}")
            manifest = {"files": [{"filename": asset.name}]}
            contract.validate_bundle_layout(bundle, manifest)
            extra = bundle / "unexpected"
            extra.write_bytes(b"x")
            with self.assertRaisesRegex(contract.ContractError, "entries differ"):
                contract.validate_bundle_layout(bundle, manifest)
            extra.unlink()
            asset.unlink()
            asset.symlink_to(draft)
            with self.assertRaisesRegex(contract.ContractError, "regular file"):
                contract.validate_bundle_layout(bundle, manifest)

    def test_publish_context_binds_run_commit_repo_and_allows_publish_only_rerun(self) -> None:
        manifest = mpa_manifest(draft=True)
        env = {
            "GITHUB_REPOSITORY": "owner/repo",
            "GITHUB_REF": "refs/heads/master",
            "GITHUB_SHA": "c" * 40,
            "GITHUB_RUN_ID": "456",
            "GITHUB_RUN_ATTEMPT": "2",
        }
        self.assertEqual(contract.validate_publish_context(manifest, "owner/repo", env), "c" * 40)
        rerun_publish = dict(env, GITHUB_RUN_ATTEMPT="3")
        self.assertEqual(contract.validate_publish_context(manifest, "owner/repo", rerun_publish), "c" * 40)
        for key, bad in (
            ("GITHUB_SHA", "d" * 40),
            ("GITHUB_RUN_ID", "457"),
            ("GITHUB_RUN_ATTEMPT", "1"),
            ("GITHUB_REF", "refs/heads/feature"),
            ("GITHUB_REPOSITORY", "other/repo"),
        ):
            changed = dict(env)
            changed[key] = bad
            with self.subTest(key=key), self.assertRaises(contract.ContractError):
                contract.validate_publish_context(manifest, "owner/repo", changed)

    def test_exact_v2_shapes_and_cadences(self) -> None:
        for dataset in ("currents", "waves", "sst", "chl", "seaice", "mld"):
            with self.subTest(dataset=dataset):
                contract.validate_manifest(cmems_manifest(dataset), expected_dataset=dataset, allow_draft=False)
        contract.validate_manifest(mpa_manifest(), expected_dataset="mpa", allow_draft=False)

    def test_unknown_fields_and_stable_filenames_fail(self) -> None:
        manifest = cmems_manifest("currents")
        manifest["surprise"] = True
        with self.assertRaises(contract.ContractError):
            contract.validate_manifest(manifest, expected_dataset="currents", allow_draft=False)
        manifest = cmems_manifest("currents")
        manifest["files"][0]["filename"] = "h00.bin"
        with self.assertRaises(contract.ContractError):
            contract.validate_manifest(manifest, expected_dataset="currents", allow_draft=False)

    def test_exact_timestamp_draft_and_mpa_source_time_semantics(self) -> None:
        draft = cmems_manifest("currents", draft=True)
        draft["published_at"] = iso(NOW)
        with self.assertRaisesRegex(contract.ContractError, "must be null"):
            contract.validate_manifest(draft, expected_dataset="currents", allow_draft=True)
        fractional = cmems_manifest("currents")
        fractional["generated_at"] = "2026-08-05T11:58:00.000Z"
        with self.assertRaisesRegex(contract.ContractError, "exact UTC"):
            contract.validate_manifest(fractional, expected_dataset="currents", allow_draft=False)
        mpa = mpa_manifest()
        mpa["data_end"] = "2024-07-01T00:00:00Z"
        with self.assertRaisesRegex(contract.ContractError, "must equal"):
            contract.validate_manifest(mpa, expected_dataset="mpa", allow_draft=False)

    def test_boolean_and_nonfinite_numeric_fields_are_rejected(self) -> None:
        manifest = cmems_manifest("currents")
        manifest["dimensions"]["width"] = True
        with self.assertRaisesRegex(contract.ContractError, "integers"):
            contract.validate_manifest(manifest, expected_dataset="currents", allow_draft=False)
        manifest = cmems_manifest("currents")
        manifest["bounds"]["north"] = math.nan
        with self.assertRaisesRegex(contract.ContractError, "finite"):
            contract.validate_manifest(manifest, expected_dataset="currents", allow_draft=False)

    def test_consumer_metadata_size_and_coordinate_contracts_match_publisher_authority(self) -> None:
        for metadata in ({}, {"attribution": 42}, {"Bad Key": "value"}, {"attribution": "   "}):
            manifest = cmems_manifest("currents")
            manifest["metadata"] = metadata
            with self.subTest(metadata=metadata), self.assertRaises(contract.ContractError):
                contract.validate_manifest(manifest, expected_dataset="currents", allow_draft=False)

        undersized_mpa = mpa_manifest()
        undersized_mpa["files"][0]["bytes"] = 1
        with self.assertRaisesRegex(contract.ContractError, "size"):
            contract.validate_manifest(undersized_mpa, expected_dataset="mpa", allow_draft=False)

        illegal_bounds = cmems_manifest("currents")
        illegal_bounds["bounds"]["north"] = 91.0
        with self.assertRaisesRegex(contract.ContractError, "legal coordinate"):
            contract.validate_manifest(illegal_bounds, expected_dataset="currents", allow_draft=False)

        future_cmems = cmems_manifest("currents", draft=True)
        future_cmems["data_start"] = iso(NOW + timedelta(minutes=1))
        future_cmems["data_end"] = iso(NOW + timedelta(hours=12, minutes=1))
        with self.assertRaisesRegex(contract.ContractError, "future"):
            contract.validate_publication_freshness(future_cmems, NOW)

        future_mpa = mpa_manifest(draft=True)
        future_mpa["data_start"] = iso(NOW + timedelta(minutes=1))
        future_mpa["data_end"] = future_mpa["data_start"]
        with self.assertRaisesRegex(contract.ContractError, "future"):
            contract.validate_publication_freshness(future_mpa, NOW)

    def test_generation_digest_is_bound_to_ordered_hashes(self) -> None:
        manifest = cmems_manifest("waves")
        manifest["files"][0]["sha256"] = "f" * 64
        with self.assertRaisesRegex(contract.ContractError, "generation digest"):
            contract.validate_manifest(manifest, expected_dataset="waves", allow_draft=False)

    def test_generation_cross_language_vector(self) -> None:
        self.assertEqual(
            contract._generation("2026-08-05T00:00:00Z", ["0" * 64, "f" * 64], "currents"),
            "g-20260805T000000Z-606e60c9f9ed",
        )

    def test_freshness_rejects_stale_and_future_sources(self) -> None:
        fresh = cmems_manifest("currents", draft=True)
        contract.validate_publication_freshness(fresh, NOW)
        stale = copy.deepcopy(fresh)
        stale["data_start"] = iso(NOW - timedelta(days=2))
        stale["data_end"] = iso(NOW - timedelta(days=2) + timedelta(hours=12))
        with self.assertRaisesRegex(contract.ContractError, "stale"):
            contract.validate_publication_freshness(stale, NOW)
        future = copy.deepcopy(fresh)
        future["data_start"] = iso(NOW + timedelta(hours=4))
        future["data_end"] = iso(NOW + timedelta(hours=16))
        with self.assertRaisesRegex(contract.ContractError, "future"):
            contract.validate_publication_freshness(future, NOW)
        contract.validate_publication_freshness(mpa_manifest(draft=True), NOW)

    def test_wave_freshness_allows_one_native_step_of_schedule_margin(self) -> None:
        wave = cmems_manifest("waves", draft=True)
        wave["data_start"] = iso(NOW - timedelta(hours=15))
        wave["data_end"] = iso(NOW + timedelta(hours=33))
        contract.validate_publication_freshness(wave, NOW)
        wave["data_start"] = iso(NOW - timedelta(hours=15, seconds=1))
        wave["data_end"] = iso(NOW + timedelta(hours=32, minutes=59, seconds=59))
        with self.assertRaisesRegex(contract.ContractError, "waves source start is stale"):
            contract.validate_publication_freshness(wave, NOW)

    def test_same_generation_refresh_allows_new_health_provenance_only(self) -> None:
        current = mpa_manifest()
        candidate = copy.deepcopy(current)
        candidate["generated_at"] = iso(NOW)
        candidate["published_at"] = None
        candidate["producer"] = {"commit": "d" * 40, "run_id": 999, "run_attempt": 1}
        contract.validate_same_generation_core(current, candidate)
        candidate["metadata"] = {"classification_notice": "changed"}
        with self.assertRaisesRegex(contract.ContractError, "immutable core"):
            contract.validate_same_generation_core(current, candidate)

    def test_real_shaped_legacy_bootstrap_and_arbitrary_legacy_rejection(self) -> None:
        live_cases = {
            "currents": (13, 1, 8_812_830),
            "waves": (17, 3, 8_812_830),
            "sst": (6, 24, 8_812_830),
        }
        for dataset, (steps, cadence, size) in live_cases.items():
            legacy = {
                "version": 1,
                "generated_at": "2026-08-05T00:00:00+00:00",
                "hours": [{"hour": index * cadence, "file": f"h{index:02d}.bin", "bytes": size} for index in range(steps)],
            }
            contract.validate_legacy_v1_bootstrap(legacy, dataset)
        legacy_mpa = {
            "version": 1,
            "generated_at": "2026-08-04T00:00:00+00:00",
            "feature_count": 4541,
            "data_file": "mpa.geojson",
            "attribution": "© Commonwealth of Australia (DCCEEW)",
        }
        contract.validate_legacy_v1_bootstrap(legacy_mpa, "mpa")
        legacy_mpa["download"] = "https://evil.invalid/payload"
        with self.assertRaises(contract.ContractError):
            contract.validate_legacy_v1_bootstrap(legacy_mpa, "mpa")


def write_thcu(path: Path, *, dataset: str, wave_30_30: bool = False) -> None:
    width, height = GRID["width"], GRID["height"]
    cells = width * height
    land = cells // 10
    u = array("f", [0.0]) * cells
    v = array("f", [0.0]) * cells
    for index in range(land, cells):
        u[index] = 30.0 if wave_30_30 else 1.0
        v[index] = 30.0 if wave_30_30 else 0.5
    u[land + 1] = 2.0 if not wave_30_30 else 30.0
    mask = bytearray(cells)
    mask[:land] = b"\x01" * land
    header = struct.pack(
        "<4sBBHHffffHH",
        b"THCU",
        2,
        0,
        width,
        height,
        GRID["bounds"]["north"],
        GRID["bounds"]["south"],
        GRID["bounds"]["west"],
        GRID["bounds"]["east"],
        1,
        0,
    )
    path.write_bytes(header + u.tobytes() + v.tobytes() + mask)


class BinaryContractTests(unittest.TestCase):
    def test_exact_thcu_payload_passes(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "field.bin"
            write_thcu(path, dataset="currents")
            contract.validate_thcu_payload(path, "currents")
            self.assertEqual(path.stat().st_size, 8_812_830)

    def test_nonfinite_mask_domain_and_wave_magnitude_fail(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "field.bin"
            write_thcu(path, dataset="waves", wave_30_30=True)
            with self.assertRaisesRegex(contract.ContractError, "magnitude"):
                contract.validate_thcu_payload(path, "waves")
            write_thcu(path, dataset="currents")
            cells = GRID["width"] * GRID["height"]
            with path.open("r+b") as output:
                output.seek(30 + (cells // 10 + 5) * 4)
                output.write(struct.pack("<f", math.nan))
            with self.assertRaisesRegex(contract.ContractError, "non-finite"):
                contract.validate_thcu_payload(path, "currents")
            write_thcu(path, dataset="currents")
            with path.open("r+b") as output:
                output.seek(30 + cells * 8 + cells - 1)
                output.write(b"\x02")
            with self.assertRaisesRegex(contract.ContractError, "0/1"):
                contract.validate_thcu_payload(path, "currents")


def mpa_feature(index: int, ring: list[list[float]]) -> dict:
    return {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [ring]},
        "properties": {
            "name": (f"Reserve {index} " + "n" * 290)[:300],
            "type": "t" * 200,
            "iucn": "IV",
            "zone": "z" * 200,
            "authority": "a" * 200,
            "state": "s" * 80,
            "area_km2": 10.0 + index,
            "protection_class": "conditional",
            "classification_source": "indicative_heuristic",
        },
    }


class MpaAssetContractTests(unittest.TestCase):
    def make_collection(self) -> tuple[dict, dict[str, float]]:
        observed_ring = [
            [70.717, -58.4488],
            [170.3667, -58.4488],
            [170.3667, -8.4738],
            [70.717, -8.4738],
            [70.717, -58.4488],
        ]
        local_ring = [[150.0, -30.0], [150.1, -30.0], [150.1, -29.9], [150.0, -30.0]]
        features = [mpa_feature(0, observed_ring)] + [mpa_feature(index, local_ring) for index in range(1, 100)]
        bounds = {"west": 70.717, "east": 170.3667, "south": -58.4488, "north": -8.4738}
        return {"type": "FeatureCollection", "features": features}, bounds

    def test_representative_external_territory_bounds_and_exact_properties(self) -> None:
        collection, bounds = self.make_collection()
        collection["features"][1]["properties"]["area_km2"] = 0.001
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "mpa.geojson"
            path.write_text(json.dumps(collection, separators=(",", ":")) + "\n", encoding="utf-8")
            self.assertGreater(path.stat().st_size, 100_000)
            contract.validate_mpa_geojson(path, feature_count=100, expected_bounds=bounds)

    def test_null_geometry_and_count_mismatch_fail(self) -> None:
        collection, bounds = self.make_collection()
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "mpa.geojson"
            path.write_text(json.dumps(collection, separators=(",", ":")) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(contract.ContractError, "count"):
                contract.validate_mpa_geojson(path, feature_count=101, expected_bounds=bounds)
            collection["features"][2]["geometry"] = None
            path.write_text(json.dumps(collection, separators=(",", ":")) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(contract.ContractError, "geometry"):
                contract.validate_mpa_geojson(path, feature_count=100, expected_bounds=bounds)


if __name__ == "__main__":
    unittest.main()
