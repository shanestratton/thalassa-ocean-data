#!/usr/bin/env python3
"""DCCEEW CAPAD marine layer -> validated immutable GeoJSON bundle.

Generation and publication are deliberately separate trust domains. This file
can read DCCEEW but cannot publish; ``scripts/publish_dataset.py`` publishes the
validated artifact later without this producer's network dependencies.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import requests

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from publisher_contract import build_mpa_bundle, producer_provenance, utc_iso

log = logging.getLogger("mpa-pipeline")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

FEATURE_LAYER = (
    "https://gis.environment.gov.au/gispubmap/rest/services/"
    "ogc_services/CAPAD/MapServer/1"
)
FEATURE_QUERY = f"{FEATURE_LAYER}/query"
WHERE = "GIS_AREA>0"
KEEP_FIELDS = ["NAME", "TYPE", "IUCN", "ZONE_TYPE", "AUTHORITY", "STATE", "GIS_AREA"]
PAGE_SIZE = 100
MAX_PAGES = 500
MIN_FEATURES = 100
MAX_FEATURES = 50_000
MIN_BYTES = 100_000
MAX_BYTES = 16 * 1024 * 1024
MAX_AGGREGATE_SOURCE_BYTES = 64 * 1024 * 1024
MAX_AGGREGATE_COORDINATES = 5_000_000
AU_ENVELOPE = {"west": 70.0, "east": 180.0, "south": -60.0, "north": 0.0}

OUT_DIR = Path(os.environ.get("MPA_OUT_DIR", "/tmp/mpa-pipeline"))
RAW_GEOJSON_PATH = OUT_DIR / "mpa.geojson"
BUNDLE_DIR = OUT_DIR / "bundle"
USER_AGENT = "thalassa-marine-weather/2.0 (mpa-pipeline; safety-data contact via repository)"

# These neutral protection classes drive map colours only. They are indicative
# heuristics from registry labels and never assert that fishing, anchoring or
# another activity is legally permitted or prohibited at a position.
HIGH_PROTECTION_KEYWORDS = (
    "sanctuary",
    "marine national park",
    "no-take",
    "no take",
    "scientific reference",
)
CONDITIONAL_PROTECTION_KEYWORDS = (
    "habitat protection",
    "habitat zone",
    "conservation",
    "buffer",
    "recreational use",
    "special purpose",
    "preservation",
)
ALLOWED_OUTPUT_PROPERTIES = {
    "name",
    "type",
    "iucn",
    "zone",
    "authority",
    "state",
    "area_km2",
    "protection_class",
    "classification_source",
}


class MpaContractError(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise MpaContractError(message)


def get_json(session: requests.Session, url: str, params: Mapping[str, Any]) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(3):
        response: requests.Response | None = None
        try:
            response = session.get(url, params=params, timeout=(15, 90), stream=True)
            response.raise_for_status()
            declared = response.headers.get("Content-Length")
            if declared is not None:
                require(declared.isdigit() and int(declared) <= MAX_BYTES, "DCCEEW declared response exceeds byte ceiling")
            body = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                require(len(body) + len(chunk) <= MAX_BYTES, "DCCEEW streamed response exceeded byte ceiling")
                body.extend(chunk)
            payload = json.loads(bytes(body).decode("utf-8"))
            require(isinstance(payload, dict), "DCCEEW response root is not an object")
            require("error" not in payload, f"DCCEEW returned an ArcGIS error: {payload.get('error')}")
            return payload
        except (requests.RequestException, UnicodeDecodeError, json.JSONDecodeError, MpaContractError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(2**attempt)
        finally:
            if response is not None:
                response.close()
    assert last_error is not None
    raise last_error


def count_coordinate_pairs(value: Any) -> int:
    if not isinstance(value, list):
        return 0
    if len(value) >= 2 and isinstance(value[0], (int, float)) and isinstance(value[1], (int, float)):
        return 1
    return sum(count_coordinate_pairs(child) for child in value)


def enforce_aggregate_source_budget(
    features: list[dict[str, Any]],
    current_bytes: int,
    current_coordinates: int,
) -> tuple[int, int]:
    page_bytes = len(json.dumps(features, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    page_coordinates = sum(count_coordinate_pairs((feature.get("geometry") or {}).get("coordinates")) for feature in features)
    total_bytes = current_bytes + page_bytes
    total_coordinates = current_coordinates + page_coordinates
    require(total_bytes <= MAX_AGGREGATE_SOURCE_BYTES, "CAPAD aggregate source exceeded compact-byte ceiling")
    require(total_coordinates <= MAX_AGGREGATE_COORDINATES, "CAPAD aggregate source exceeded coordinate ceiling")
    return total_bytes, total_coordinates


def query_feature_batch(
    session: requests.Session,
    object_ids: list[int],
    object_id_field: str,
    *,
    simplify: bool,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "objectIds": ",".join(str(value) for value in object_ids),
        "outFields": ",".join([object_id_field, *KEEP_FIELDS]),
        "orderByFields": f"{object_id_field} ASC",
        "outSR": 4326,
        "f": "geojson",
        "returnGeometry": "true",
    }
    if simplify:
        params["maxAllowableOffset"] = 0.005
        params["geometryPrecision"] = 4
    payload = get_json(session, FEATURE_QUERY, params)
    require(payload.get("type") == "FeatureCollection", "CAPAD page is not a GeoJSON FeatureCollection")
    require(payload.get("exceededTransferLimit") is not True, "CAPAD page was partial")
    features = payload.get("features")
    require(isinstance(features, list), "CAPAD page features are invalid")
    return features


def index_feature_batch(
    features: list[dict[str, Any]],
    requested: set[int],
    object_id_field: str,
) -> dict[int, dict[str, Any]]:
    indexed: dict[int, dict[str, Any]] = {}
    for feature in features:
        require(isinstance(feature, dict), "CAPAD feature is not an object")
        properties = feature.get("properties")
        require(isinstance(properties, dict), "CAPAD feature properties are missing")
        object_id = properties.get(object_id_field)
        require(isinstance(object_id, int) and object_id in requested, "CAPAD page returned an unrequested ID")
        require(object_id not in indexed, "CAPAD page returned a duplicate ID")
        indexed[object_id] = feature
    return indexed


def fetch_capad_marine() -> tuple[dict[str, Any], str]:
    """Fetch exact authoritative IDs, then deterministic ID-addressed pages."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    layer = get_json(session, FEATURE_LAYER, {"f": "json"})
    require(layer.get("geometryType") == "esriGeometryPolygon", "CAPAD layer is no longer polygonal")
    oid_fields = [
        field.get("name")
        for field in layer.get("fields", [])
        if isinstance(field, dict) and field.get("type") == "esriFieldTypeOID"
    ]
    object_id_field = layer.get("objectIdField") or layer.get("objectIdFieldName") or (oid_fields[0] if len(oid_fields) == 1 else None)
    require(isinstance(object_id_field, str) and object_id_field, "CAPAD layer has no object ID field")
    last_edit_ms = (layer.get("editingInfo") or {}).get("lastEditDate")
    if isinstance(last_edit_ms, (int, float)) and last_edit_ms > 0:
        source_time = utc_iso(datetime.fromtimestamp(last_edit_ms / 1000, tz=timezone.utc))
    else:
        # The current MapServer omits editingInfo but states its exact data
        # currency in the official layer description (for example
        # "data current for 30 June 2024"). Unknown wording fails closed.
        description = layer.get("description")
        match = re.search(
            r"data current for (\d{1,2} (?:January|February|March|April|May|June|July|August|September|October|November|December) \d{4})",
            description if isinstance(description, str) else "",
            flags=re.IGNORECASE,
        )
        require(match is not None, "CAPAD layer has no machine-verifiable source currency date")
        source_date = datetime.strptime(match.group(1).title(), "%d %B %Y").replace(tzinfo=timezone.utc)
        source_time = utc_iso(source_date)

    count_payload = get_json(
        session,
        FEATURE_QUERY,
        {"where": WHERE, "returnCountOnly": "true", "f": "json"},
    )
    expected_count = count_payload.get("count")
    require(isinstance(expected_count, int), "CAPAD count response is invalid")
    require(MIN_FEATURES <= expected_count <= MAX_FEATURES, f"CAPAD count {expected_count} is outside safety bounds")

    id_payload = get_json(
        session,
        FEATURE_QUERY,
        {"where": WHERE, "returnIdsOnly": "true", "f": "json"},
    )
    raw_ids = id_payload.get("objectIds")
    require(isinstance(raw_ids, list) and all(isinstance(value, int) for value in raw_ids), "CAPAD ID response is invalid")
    ids = sorted(raw_ids)
    require(len(ids) == expected_count, "CAPAD ID count does not match authoritative count")
    require(len(set(ids)) == expected_count, "CAPAD authoritative ID list contains duplicates")
    require(math.ceil(expected_count / PAGE_SIZE) <= MAX_PAGES, "CAPAD paging would exceed MAX_PAGES")

    all_features: dict[int, dict[str, Any]] = {}
    aggregate_bytes = 0
    aggregate_coordinates = 0
    for page_index, start in enumerate(range(0, len(ids), PAGE_SIZE)):
        page_ids = ids[start : start + PAGE_SIZE]
        log.info("Fetching deterministic CAPAD page %d (%d IDs)", page_index, len(page_ids))
        requested = set(page_ids)
        page_map = index_feature_batch(query_feature_batch(session, page_ids, object_id_field, simplify=True), requested, object_id_field)
        fallback_ids = set(page_ids) - set(page_map)
        for object_id, feature in page_map.items():
            area = (feature.get("properties") or {}).get("GIS_AREA")
            if isinstance(area, (int, float)) and area <= 0.1:
                fallback_ids.add(object_id)
                continue
            try:
                validate_geometry(feature.get("geometry"))
            except MpaContractError:
                fallback_ids.add(object_id)

        # Tiny or simplification-damaged polygons are deterministically
        # refetched without maxAllowableOffset. No source ID is dropped and no
        # replacement geometry is fabricated.
        for fallback_start in range(0, len(fallback_ids), 25):
            fallback_page = sorted(fallback_ids)[fallback_start : fallback_start + 25]
            fallback_map = index_feature_batch(
                query_feature_batch(session, fallback_page, object_id_field, simplify=False),
                set(fallback_page),
                object_id_field,
            )
            require(set(fallback_map) == set(fallback_page), "CAPAD unsimplified fallback lost source IDs")
            page_map.update(fallback_map)

        require(set(page_map) == requested, "CAPAD page did not return every authoritative ID")
        ordered_page = [page_map[object_id] for object_id in page_ids]
        for feature in ordered_page:
            validate_geometry(feature.get("geometry"))
        aggregate_bytes, aggregate_coordinates = enforce_aggregate_source_budget(
            ordered_page,
            aggregate_bytes,
            aggregate_coordinates,
        )
        for object_id, feature in zip(page_ids, ordered_page):
            require(object_id not in all_features, "CAPAD page returned a duplicate ID")
            all_features[object_id] = feature

    require(set(all_features) == set(ids), "CAPAD reconciliation failed: missing or extra IDs")
    ordered = [all_features[object_id] for object_id in ids]
    return {"type": "FeatureCollection", "features": ordered}, source_time


def classify_protection(properties: Mapping[str, Any]) -> str:
    """Return a neutral display class, never a legal activity determination."""
    iucn = str(properties.get("IUCN") or "").strip()
    if iucn in {"Ia", "Ib", "II"}:
        return "high"
    if iucn in {"III", "IV"}:
        return "conditional"
    haystack = " ".join(str(properties.get(key) or "").lower() for key in ("TYPE", "ZONE_TYPE", "NAME"))
    if any(keyword in haystack for keyword in HIGH_PROTECTION_KEYWORDS):
        return "high"
    if any(keyword in haystack for keyword in CONDITIONAL_PROTECTION_KEYWORDS):
        return "conditional"
    return "multiple_use"


def _rings(geometry: Mapping[str, Any]) -> Iterable[list[Any]]:
    coordinates = geometry.get("coordinates")
    if geometry.get("type") == "Polygon":
        require(isinstance(coordinates, list) and coordinates, "Polygon coordinates are empty")
        yield from coordinates
    elif geometry.get("type") == "MultiPolygon":
        require(isinstance(coordinates, list) and coordinates, "MultiPolygon coordinates are empty")
        for polygon in coordinates:
            require(isinstance(polygon, list) and polygon, "MultiPolygon member is empty")
            yield from polygon
    else:
        raise MpaContractError("MPA geometry must be Polygon or MultiPolygon")


def validate_geometry(geometry: Any) -> tuple[float, float, float, float]:
    require(isinstance(geometry, dict), "MPA geometry is null or invalid")
    west, south, east, north = 180.0, 90.0, -180.0, -90.0
    point_count = 0
    for ring in _rings(geometry):
        require(isinstance(ring, list) and len(ring) >= 4, "MPA polygon ring is too short")
        require(ring[0] == ring[-1], "MPA polygon ring is not closed")
        for position in ring:
            require(isinstance(position, list) and len(position) >= 2, "MPA coordinate is invalid")
            lon, lat = position[0], position[1]
            require(isinstance(lon, (int, float)) and isinstance(lat, (int, float)), "MPA coordinate is not numeric")
            require(math.isfinite(lon) and math.isfinite(lat), "MPA coordinate is not finite")
            require(AU_ENVELOPE["west"] <= lon <= AU_ENVELOPE["east"], "MPA longitude is outside Australian territory envelope")
            require(AU_ENVELOPE["south"] <= lat <= AU_ENVELOPE["north"], "MPA latitude is outside Australian territory envelope")
            west, south, east, north = min(west, lon), min(south, lat), max(east, lon), max(north, lat)
            point_count += 1
    require(point_count >= 4, "MPA geometry has no usable coordinates")
    return west, south, east, north


def normalise_features(collection: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, float]]:
    require(collection.get("type") == "FeatureCollection", "CAPAD root is not a FeatureCollection")
    source_features = collection.get("features")
    require(isinstance(source_features, list) and MIN_FEATURES <= len(source_features) <= MAX_FEATURES, "CAPAD feature count is invalid")
    output: list[dict[str, Any]] = []
    overall = {"west": 180.0, "south": 90.0, "east": -180.0, "north": -90.0}
    for feature in source_features:
        require(isinstance(feature, dict) and feature.get("type") == "Feature", "invalid CAPAD feature")
        properties = feature.get("properties")
        require(isinstance(properties, dict), "CAPAD feature has no properties")
        geometry = feature.get("geometry")
        west, south, east, north = validate_geometry(geometry)
        overall["west"] = min(overall["west"], west)
        overall["south"] = min(overall["south"], south)
        overall["east"] = max(overall["east"], east)
        overall["north"] = max(overall["north"], north)
        area = properties.get("GIS_AREA")
        require(isinstance(area, (int, float)) and math.isfinite(area) and 0 < area < 200_000_000, "CAPAD GIS_AREA hectares value is invalid")
        slim = {
            "name": str(properties.get("NAME") or "Unknown reserve")[:300],
            "type": str(properties.get("TYPE") or "")[:200],
            "iucn": str(properties.get("IUCN") or "")[:20],
            "zone": str(properties.get("ZONE_TYPE") or "")[:200],
            "authority": str(properties.get("AUTHORITY") or "")[:200],
            "state": str(properties.get("STATE") or "")[:80],
            "area_km2": round(float(area) / 100, 8),
            "protection_class": classify_protection(properties),
            "classification_source": "indicative_heuristic",
        }
        require(slim["area_km2"] > 0, "CAPAD GIS_AREA loses positivity during hectares-to-km² conversion")
        require(set(slim) == ALLOWED_OUTPUT_PROPERTIES, "MPA output property contract changed")
        output.append({"type": "Feature", "geometry": geometry, "properties": slim})
    return {"type": "FeatureCollection", "features": output}, overall


def write_validated_geojson(collection: Mapping[str, Any]) -> int:
    encoded = (json.dumps(collection, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    require(MIN_BYTES <= len(encoded) <= MAX_BYTES, f"MPA GeoJSON byte size {len(encoded)} is outside safety bounds")
    RAW_GEOJSON_PATH.write_bytes(encoded)
    # Reparse the exact on-disk bytes before bundling; the isolated publisher
    # performs this validation again after the artifact boundary.
    reparsed = json.loads(RAW_GEOJSON_PATH.read_text(encoding="utf-8"))
    require(reparsed == collection, "MPA GeoJSON changed during serialization")
    return len(encoded)


def main() -> int:
    try:
        raw, source_time = fetch_capad_marine()
        normalised, bounds = normalise_features(raw)
        size = write_validated_geojson(normalised)
        count = len(normalised["features"])
        build_mpa_bundle(
            source_path=RAW_GEOJSON_PATH,
            bundle_dir=BUNDLE_DIR,
            source_time=source_time,
            feature_count=count,
            bounds=bounds,
            provenance=producer_provenance(),
            metadata={
                "attribution": "Commonwealth of Australia (DCCEEW), CAPAD",
                "licence": "CC BY 4.0",
                "area_units": "km2 (GIS_AREA hectares divided by 100)",
                "classification_notice": (
                    "Map colour categories are automated, indicative registry interpretations only. "
                    "They do not determine whether fishing, anchoring, entry or another activity is lawful; "
                    "consult the responsible authority and current zoning rules."
                ),
            },
        )
        log.info("Generated %d reconciled MPA features (%d bytes) in %s", count, size, BUNDLE_DIR)
    except Exception:  # noqa: BLE001
        log.exception("MPA generation failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
