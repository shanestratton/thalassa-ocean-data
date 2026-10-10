from __future__ import annotations

import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
try:
    import requests as _requests  # noqa: F401
except ModuleNotFoundError:
    requests_stub = types.ModuleType("requests")
    requests_stub.RequestException = type("RequestException", (Exception,), {})
    requests_stub.Response = object
    requests_stub.Session = object
    sys.modules["requests"] = requests_stub
MODULE_PATH = REPO_ROOT / "scripts/mpa-pipeline/pipeline.py"
SPEC = importlib.util.spec_from_file_location("mpa_pipeline_under_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
mpa = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mpa)


class FakeResponse:
    def __init__(self, chunks: list[bytes], *, content_length: str | None = None) -> None:
        self.chunks = chunks
        self.headers = {} if content_length is None else {"Content-Length": content_length}
        self.closed = False

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, *, chunk_size: int):
        assert chunk_size == 64 * 1024
        yield from self.chunks

    def close(self) -> None:
        self.closed = True


class FakeSession:
    def __init__(self, factory) -> None:
        self.factory = factory
        self.calls: list[dict] = []
        self.responses: list[FakeResponse] = []

    def get(self, url: str, **kwargs):
        self.calls.append(kwargs)
        response = self.factory()
        self.responses.append(response)
        return response


class MpaTransportTests(unittest.TestCase):
    def test_valid_json_is_streamed_and_closed(self) -> None:
        raw = json.dumps({"count": 4541}).encode()
        session = FakeSession(lambda: FakeResponse([raw[:4], raw[4:]], content_length=str(len(raw))))
        self.assertEqual(mpa.get_json(session, "https://example.invalid", {}), {"count": 4541})
        self.assertTrue(session.calls[0]["stream"])
        self.assertTrue(session.responses[0].closed)

    def test_oversized_declared_body_is_rejected_before_iteration(self) -> None:
        session = FakeSession(lambda: FakeResponse([], content_length="11"))
        with patch.object(mpa, "MAX_BYTES", 10), patch.object(mpa.time, "sleep"):
            with self.assertRaisesRegex(mpa.MpaContractError, "declared"):
                mpa.get_json(session, "https://example.invalid", {})
        self.assertEqual(len(session.calls), 3)
        self.assertTrue(all(response.closed for response in session.responses))

    def test_oversized_chunked_body_is_rejected_incrementally(self) -> None:
        session = FakeSession(lambda: FakeResponse([b"123456", b"789012"]))
        with patch.object(mpa, "MAX_BYTES", 10), patch.object(mpa.time, "sleep"):
            with self.assertRaisesRegex(mpa.MpaContractError, "streamed"):
                mpa.get_json(session, "https://example.invalid", {})
        self.assertEqual(len(session.calls), 3)
        self.assertTrue(all(response.closed for response in session.responses))

    def test_aggregate_page_budget_blocks_multi_page_memory_growth(self) -> None:
        feature = {
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [[[150.0, -30.0], [150.1, -30.0], [150.0, -30.0]]]},
            "properties": {"OBJECTID": 1},
        }
        with patch.object(mpa, "MAX_AGGREGATE_SOURCE_BYTES", 10):
            with self.assertRaisesRegex(mpa.MpaContractError, "compact-byte"):
                mpa.enforce_aggregate_source_budget([feature], 0, 0)
        with patch.object(mpa, "MAX_AGGREGATE_COORDINATES", 2):
            with self.assertRaisesRegex(mpa.MpaContractError, "coordinate"):
                mpa.enforce_aggregate_source_budget([feature], 0, 0)

    def test_unsimplified_fallback_omits_offset_and_coordinate_rounding(self) -> None:
        captured: list[dict] = []

        def fake_get_json(_session, _url, params):
            captured.append(params)
            return {"type": "FeatureCollection", "features": []}

        with patch.object(mpa, "get_json", side_effect=fake_get_json):
            mpa.query_feature_batch(object(), [7], "OBJECTID", simplify=False)
            mpa.query_feature_batch(object(), [7], "OBJECTID", simplify=True)
        self.assertNotIn("maxAllowableOffset", captured[0])
        self.assertNotIn("geometryPrecision", captured[0])
        self.assertEqual(captured[1]["maxAllowableOffset"], 0.005)
        self.assertEqual(captured[1]["geometryPrecision"], 4)

    def test_gis_area_hectares_are_converted_without_losing_tiny_positive_area(self) -> None:
        feature = {
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[150.0, -30.0], [150.01, -30.0], [150.01, -29.99], [150.0, -30.0]]],
            },
            "properties": {
                "NAME": "Tiny zone",
                "TYPE": "Marine Park",
                "IUCN": "IV",
                "ZONE_TYPE": "",
                "AUTHORITY": "TEST",
                "STATE": "QLD",
                "GIS_AREA": 0.1,
            },
        }
        collection = {"type": "FeatureCollection", "features": [feature for _ in range(100)]}
        normalized, _ = mpa.normalise_features(collection)
        self.assertEqual(normalized["features"][0]["properties"]["area_km2"], 0.001)
        self.assertEqual(normalized["features"][0]["properties"]["protection_class"], "conditional")
        self.assertEqual(normalized["features"][0]["properties"]["classification_source"], "indicative_heuristic")
        self.assertNotIn("restriction", normalized["features"][0]["properties"])


if __name__ == "__main__":
    unittest.main()
