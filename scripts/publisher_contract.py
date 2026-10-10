#!/usr/bin/env python3
"""Fail-closed schema-v2 contracts for generated release datasets.

This module deliberately uses only the Python standard library.  Producers use
it to build immutable bundles and the isolated publish job uses the same code to
revalidate those bundles before changing a release manifest.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import stat
import struct
import sys
from array import array
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SCHEMA_VERSION = 2
MANIFEST_NAME = "manifest.json"
DRAFT_MANIFEST_NAME = "manifest.draft.json"
GENERATION_RE = re.compile(r"^g-(\d{8}T\d{6}Z)-([0-9a-f]{12})$")
UTC_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
SHARD_TAG_RE = re.compile(r"^[a-z0-9-]+-assets-(\d{4})-W(\d{2})$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
METADATA_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
CMEMS_ASSET_RE = re.compile(r"^(g-\d{8}T\d{6}Z-[0-9a-f]{12})-h(\d{3})\.bin$")
MPA_ASSET_RE = re.compile(r"^(g-\d{8}T\d{6}Z-[0-9a-f]{12})-mpa\.geojson$")

DATASETS: dict[str, dict[str, Any]] = {
    "currents": {
        "id": "cmems_mod_glo_phy_anfc_merged-uv_PT1H-i",
        "release_tag": "cmems-currents-latest",
        "steps": 13,
        "cadence_hours": 1,
        "kind": "cmems-binary",
        "max_file_bytes": 16 * 1024 * 1024,
    },
    "waves": {
        "id": "cmems_mod_glo_wav_anfc_0.083deg_PT3H-i",
        "release_tag": "cmems-waves-latest",
        "steps": 17,
        "cadence_hours": 3,
        "kind": "cmems-binary",
        "max_file_bytes": 16 * 1024 * 1024,
    },
    "sst": {
        "id": "cmems_mod_glo_phy-thetao_anfc_0.083deg_P1D-m",
        "release_tag": "cmems-sst-latest",
        "steps": 6,
        "cadence_hours": 24,
        "kind": "cmems-binary",
        "max_file_bytes": 16 * 1024 * 1024,
    },
    "chl": {
        "id": "cmems_mod_glo_bgc-pft_anfc_0.25deg_P1D-m",
        "release_tag": "cmems-chl-latest",
        "steps": 6,
        "cadence_hours": 24,
        "kind": "cmems-binary",
        "max_file_bytes": 16 * 1024 * 1024,
    },
    "seaice": {
        "id": "cmems_mod_glo_phy_anfc_0.083deg_P1D-m",
        "release_tag": "cmems-seaice-latest",
        "steps": 6,
        "cadence_hours": 24,
        "kind": "cmems-binary",
        "max_file_bytes": 16 * 1024 * 1024,
    },
    "mld": {
        "id": "cmems_mod_glo_phy_anfc_0.083deg_P1D-m",
        "release_tag": "cmems-mld-latest",
        "steps": 6,
        "cadence_hours": 24,
        "kind": "cmems-binary",
        "max_file_bytes": 16 * 1024 * 1024,
    },
    "mpa": {
        "id": "dcceew-capad-mapserver-layer-1",
        "release_tag": "mpa-aus-latest",
        "steps": 1,
        "cadence_hours": None,
        "kind": "geojson",
        "max_file_bytes": 16 * 1024 * 1024,
    },
}


class ContractError(ValueError):
    """Raised when generated or remotely downloaded data violates a contract."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def utc_iso(value: datetime) -> str:
    _require(value.tzinfo is not None, "timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_utc(value: Any, field: str) -> datetime:
    _require(isinstance(value, str) and UTC_ISO_RE.fullmatch(value) is not None, f"{field} must be an exact UTC ISO-8601 second")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ContractError(f"{field} is not a valid timestamp") from exc
    _require(parsed.tzinfo is not None, f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def parse_legacy_time(value: Any, field: str) -> datetime:
    _require(isinstance(value, str), f"{field} must be a timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{field} is not a valid timestamp") from exc
    _require(parsed.tzinfo is not None, f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except FileNotFoundError:
        return False


def asset_shard_tag(dataset_key: str, generation: str) -> str:
    spec = DATASETS.get(dataset_key)
    _require(spec is not None, "unknown dataset for asset shard")
    match = GENERATION_RE.fullmatch(generation)
    _require(match is not None, "invalid generation for asset shard")
    try:
        timestamp = datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ContractError("generation contains an invalid UTC timestamp") from exc
    iso_year, iso_week, _ = timestamp.isocalendar()
    tag = f"{spec['release_tag']}-assets-{iso_year:04d}-W{iso_week:02d}"
    _require(SHARD_TAG_RE.fullmatch(tag) is not None, "derived asset shard tag is invalid")
    return tag


def asset_filename_generation(dataset_key: str, filename: str) -> str:
    spec = DATASETS.get(dataset_key)
    _require(spec is not None, "unknown dataset for asset filename")
    match = MPA_ASSET_RE.fullmatch(filename) if dataset_key == "mpa" else CMEMS_ASSET_RE.fullmatch(filename)
    _require(match is not None, "asset filename is not an immutable generation name")
    if dataset_key != "mpa":
        _require(int(match.group(2)) < int(spec["steps"]), "asset filename step is out of range")
    return match.group(1)


def validate_shard_inventory(
    dataset_key: str,
    shard_tag: str,
    inventory: Sequence[Mapping[str, Any]],
    candidate_files: Sequence[Mapping[str, Any]],
    *,
    maximum_assets: int = 900,
) -> dict[str, int]:
    _require(0 < maximum_assets < 1_000, "shard capacity guard must leave headroom below 1,000")
    existing: dict[str, int] = {}
    for asset in inventory:
        name = asset.get("name")
        size = asset.get("size")
        _require(isinstance(name, str) and isinstance(size, int) and size > 0, "shard inventory entry is malformed")
        _require(name not in existing, "asset shard inventory contains duplicate names")
        generation = asset_filename_generation(dataset_key, name)
        _require(asset_shard_tag(dataset_key, generation) == shard_tag, f"asset {name} does not belong to shard {shard_tag}")
        existing[name] = size
    candidate_names: set[str] = set()
    for entry in candidate_files:
        name = entry.get("filename")
        size = entry.get("bytes")
        _require(isinstance(name, str) and isinstance(size, int) and size > 0, "candidate asset entry is malformed")
        _require(name not in candidate_names, "candidate asset names are duplicated")
        generation = asset_filename_generation(dataset_key, name)
        _require(asset_shard_tag(dataset_key, generation) == shard_tag, f"candidate {name} does not belong to shard {shard_tag}")
        candidate_names.add(name)
        if name in existing:
            _require(existing[name] == size, f"remote immutable asset size collision: {name}")
    _require(len(set(existing) | candidate_names) <= maximum_assets, f"asset shard would exceed the conservative {maximum_assets}-asset capacity guard")
    return existing


def producer_provenance(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    values = os.environ if env is None else env
    commit = values.get("GITHUB_SHA", "")
    run_id = values.get("GITHUB_RUN_ID", "")
    run_attempt = values.get("GITHUB_RUN_ATTEMPT", "")
    _require(bool(COMMIT_RE.fullmatch(commit)), "GITHUB_SHA must be a full 40-character lowercase commit SHA")
    _require(run_id.isdigit() and int(run_id) > 0, "GITHUB_RUN_ID must be a positive integer")
    _require(run_attempt.isdigit() and int(run_attempt) > 0, "GITHUB_RUN_ATTEMPT must be a positive integer")
    return {"commit": commit, "run_id": int(run_id), "run_attempt": int(run_attempt)}


def validate_publish_context(
    manifest: Mapping[str, Any],
    repository: str,
    env: Mapping[str, str] | None = None,
) -> str:
    values = os.environ if env is None else env
    _require(values.get("GITHUB_REPOSITORY") == repository and repository.count("/") == 1, "publisher repository context mismatch")
    _require(values.get("GITHUB_REF") == "refs/heads/master", "publisher must run from the exact master ref")
    publisher = producer_provenance(values)
    producer = manifest.get("producer")
    _require(isinstance(producer, Mapping), "bundle producer provenance is malformed")
    _require(
        producer.get("commit") == publisher["commit"] and producer.get("run_id") == publisher["run_id"],
        "bundle producer commit/run does not match publish job",
    )
    producer_attempt = producer.get("run_attempt")
    _require(
        isinstance(producer_attempt, int)
        and not isinstance(producer_attempt, bool)
        and 0 < producer_attempt <= publisher["run_attempt"],
        "bundle producer attempt must be positive and no newer than publish job attempt",
    )
    return publisher["commit"]


def _generation(data_start: str, file_hashes: Sequence[str], dataset_key: str) -> str:
    start = parse_utc(data_start, "data_start").strftime("%Y%m%dT%H%M%SZ")
    material = json.dumps(
        {"dataset": dataset_key, "data_start": data_start, "sha256": list(file_hashes)},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return f"g-{start}-{hashlib.sha256(material).hexdigest()[:12]}"


def _copy_immutable(source: Path, destination: Path) -> None:
    _require(is_regular_file(source), f"generated asset is missing or non-regular: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        _require(
            destination.stat().st_size == source.stat().st_size
            and sha256_path(destination) == sha256_path(source),
            f"immutable local asset collision: {destination.name}",
        )
        return
    shutil.copyfile(source, destination)


def read_thcu_header(path: Path) -> dict[str, Any]:
    with path.open("rb") as source:
        header = source.read(30)
    _require(len(header) == 30, f"{path.name}: truncated THCU header")
    magic, version, reserved, width, height, north, south, west, east, hours, tail = struct.unpack(
        "<4sBBHHffffHH", header
    )
    _require(magic == b"THCU", f"{path.name}: bad THCU magic")
    _require(version == 2, f"{path.name}: unsupported THCU version")
    _require(reserved == 0 and tail == 0 and hours == 1, f"{path.name}: invalid reserved/header fields")
    expected_bytes = 30 + width * height * 9
    _require(path.stat().st_size == expected_bytes, f"{path.name}: byte length does not match dimensions")
    _require(1300 <= width <= 1500 and 600 <= height <= 750, f"{path.name}: implausible global dimensions")
    _require(north > south and east > west, f"{path.name}: non-oriented bounds")
    _require(-90.0 <= south < north <= 90.0 and -180.0 <= west < east <= 180.0, f"{path.name}: bounds exceed the legal coordinate domain")
    _require(north >= 89.0 and south <= -79.0, f"{path.name}: latitude coverage is not global")
    _require(west <= -179.0 and east >= 179.0, f"{path.name}: longitude coverage is not global")
    lon_step = (east - west) / (width - 1)
    lat_step = (north - south) / (height - 1)
    _require(0.23 <= lon_step <= 0.27 and 0.23 <= lat_step <= 0.27, f"{path.name}: output resolution is not 0.25 degrees")
    _require(path.stat().st_size <= 16 * 1024 * 1024, f"{path.name}: binary exceeds size ceiling")
    return {
        "width": width,
        "height": height,
        "bounds": {"north": north, "south": south, "west": west, "east": east},
    }


def _read_float_plane(source: Any, cell_count: int, label: str) -> array:
    raw = source.read(cell_count * 4)
    _require(len(raw) == cell_count * 4, f"truncated {label} float plane")
    values = array("f")
    values.frombytes(raw)
    if sys.byteorder != "little":
        values.byteswap()
    _require(all(math.isfinite(value) for value in values), f"{label} contains a non-finite value")
    return values


def validate_thcu_payload(path: Path, dataset_key: str) -> str:
    """Reparse a generated binary after the artifact trust boundary."""
    header = read_thcu_header(path)
    cells = header["width"] * header["height"]
    with path.open("rb") as source:
        source.seek(30)
        u = _read_float_plane(source, cells, f"{path.name} u")
        v = _read_float_plane(source, cells, f"{path.name} v")
        mask = source.read(cells)
        _require(source.read(1) == b"", f"{path.name}: trailing bytes")
    _require(len(mask) == cells and set(mask) <= {0, 1}, f"{path.name}: land mask must contain only 0/1")
    land_count = mask.count(1)
    ocean_count = cells - land_count
    _require(ocean_count > 0 and land_count > 0, f"{path.name}: both ocean and land cells are required")
    _require(0.02 <= land_count / cells <= 0.60, f"{path.name}: implausible land-mask fraction")

    for index, value in enumerate(mask):
        if value == 1:
            _require(abs(u[index]) <= 1e-7 and abs(v[index]) <= 1e-7, f"{path.name}: masked land values must be exactly zero")

    ocean_indexes = (index for index, value in enumerate(mask) if value == 0)
    u_min, u_max, v_min, v_max = math.inf, -math.inf, math.inf, -math.inf
    for index in ocean_indexes:
        u_value = u[index]
        v_value = v[index]
        u_min, u_max = min(u_min, u_value), max(u_max, u_value)
        v_min, v_max = min(v_min, v_value), max(v_max, v_value)
    if dataset_key == "currents":
        _require(-8.0 <= u_min <= u_max <= 8.0 and -8.0 <= v_min <= v_max <= 8.0, f"{path.name}: current values implausible")
        _require((u_max - u_min) + (v_max - v_min) > 1e-5, f"{path.name}: current field is constant")
    elif dataset_key == "waves":
        _require(-40.0 <= u_min <= u_max <= 40.0 and -40.0 <= v_min <= v_max <= 40.0, f"{path.name}: wave values implausible")
        max_magnitude = max(math.hypot(u[index], v[index]) for index, value in enumerate(mask) if value == 0)
        _require(max_magnitude <= 40.01, f"{path.name}: wave vector magnitude exceeds VHM0 ceiling")
        _require((u_max - u_min) + (v_max - v_min) > 1e-5, f"{path.name}: wave field is constant")
    elif dataset_key == "sst":
        _require(-3.0 <= u_min <= u_max <= 45.0, f"{path.name}: SST values implausible")
        _require(abs(v_min) <= 1e-6 and abs(v_max) <= 1e-6, f"{path.name}: SST v plane is not zero")
        _require(u_max - u_min > 1e-4, f"{path.name}: SST field is constant")
    elif dataset_key in {"chl", "seaice", "mld"}:
        _require(-1e-6 <= u_min <= u_max <= 1.000001, f"{path.name}: normalised scalar values implausible")
        _require(abs(v_min) <= 1e-6 and abs(v_max) <= 1e-6, f"{path.name}: scalar v plane is not zero")
        _require(u_max - u_min > 1e-6, f"{path.name}: scalar field is constant")
    else:
        raise ContractError(f"{path.name}: unsupported THCU dataset {dataset_key}")
    return hashlib.sha256(mask).hexdigest()


MPA_PROPERTIES = {
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


def _geojson_rings(geometry: Mapping[str, Any]) -> Iterable[list[Any]]:
    coordinates = geometry.get("coordinates")
    if geometry.get("type") == "Polygon":
        _require(isinstance(coordinates, list) and coordinates, "empty Polygon coordinates")
        yield from coordinates
    elif geometry.get("type") == "MultiPolygon":
        _require(isinstance(coordinates, list) and coordinates, "empty MultiPolygon coordinates")
        for polygon in coordinates:
            _require(isinstance(polygon, list) and polygon, "empty MultiPolygon member")
            yield from polygon
    else:
        raise ContractError("MPA geometry must be Polygon or MultiPolygon")


def validate_mpa_geojson(path: Path, *, feature_count: int, expected_bounds: Mapping[str, float]) -> None:
    _require(path.is_file() and 100_000 <= path.stat().st_size <= 16 * 1024 * 1024, "MPA GeoJSON size outside bounds")
    try:
        payload = json.loads(path.read_bytes().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError("MPA asset is not bounded UTF-8 JSON") from exc
    _require(isinstance(payload, dict) and set(payload) == {"type", "features"}, "MPA root fields are invalid")
    _require(payload.get("type") == "FeatureCollection", "MPA root must be FeatureCollection")
    features = payload.get("features")
    _require(isinstance(features, list) and len(features) == feature_count, "MPA exact feature count mismatch")
    actual = {"west": 180.0, "south": 90.0, "east": -180.0, "north": -90.0}
    for index, feature in enumerate(features):
        _require(isinstance(feature, dict) and set(feature) == {"type", "geometry", "properties"}, f"MPA feature {index} fields invalid")
        _require(feature.get("type") == "Feature", f"MPA feature {index} has wrong type")
        properties = feature.get("properties")
        _require(isinstance(properties, dict) and set(properties) == MPA_PROPERTIES, f"MPA feature {index} properties invalid")
        for field, maximum in (("name", 300), ("type", 200), ("iucn", 20), ("zone", 200), ("authority", 200), ("state", 80)):
            _require(isinstance(properties[field], str) and len(properties[field]) <= maximum, f"MPA feature {index} {field} invalid")
        area = properties.get("area_km2")
        _require(isinstance(area, (int, float)) and math.isfinite(area) and 0 < area < 2_000_000, f"MPA feature {index} area_km2 invalid")
        _require(properties.get("protection_class") in {"high", "conditional", "multiple_use"}, f"MPA feature {index} display class invalid")
        _require(properties.get("classification_source") == "indicative_heuristic", f"MPA feature {index} classification source invalid")
        geometry = feature.get("geometry")
        _require(isinstance(geometry, dict) and set(geometry) == {"type", "coordinates"}, f"MPA feature {index} geometry invalid")
        for ring in _geojson_rings(geometry):
            _require(isinstance(ring, list) and len(ring) >= 4 and ring[0] == ring[-1], f"MPA feature {index} ring invalid")
            for point in ring:
                _require(isinstance(point, list) and len(point) == 2, f"MPA feature {index} coordinate invalid")
                lon, lat = point
                _require(isinstance(lon, (int, float)) and isinstance(lat, (int, float)), f"MPA feature {index} coordinate not numeric")
                _require(math.isfinite(lon) and math.isfinite(lat), f"MPA feature {index} coordinate not finite")
                _require(70.0 <= lon <= 180.0 and -60.0 <= lat <= 0.0, f"MPA feature {index} coordinate outside Australian territory envelope")
                actual["west"], actual["east"] = min(actual["west"], lon), max(actual["east"], lon)
                actual["south"], actual["north"] = min(actual["south"], lat), max(actual["north"], lat)
    for key in actual:
        _require(abs(actual[key] - float(expected_bounds[key])) <= 1e-6, f"MPA {key} bound does not match coordinates")


def build_cmems_bundle(
    *,
    dataset_key: str,
    source_paths: Sequence[Path],
    offsets_hours: Sequence[int],
    data_times: Sequence[str],
    bundle_dir: Path,
    provenance: Mapping[str, Any],
    metadata: Mapping[str, Any] | None = None,
) -> Path:
    spec = DATASETS.get(dataset_key)
    _require(spec is not None and spec["kind"] == "cmems-binary", "unknown CMEMS dataset key")
    expected_steps = int(spec["steps"])
    cadence = int(spec["cadence_hours"])
    _require(len(source_paths) == expected_steps, f"{dataset_key}: expected {expected_steps} files")
    _require(len(offsets_hours) == expected_steps and len(data_times) == expected_steps, "file/time cardinality mismatch")
    _require(list(offsets_hours) == [i * cadence for i in range(expected_steps)], "forecast offsets violate cadence")
    parsed_times = [parse_utc(value, f"data_times[{i}]") for i, value in enumerate(data_times)]
    _require(
        all((parsed_times[i] - parsed_times[0]).total_seconds() == offsets_hours[i] * 3600 for i in range(expected_steps)),
        "actual data times violate declared forecast offsets",
    )

    headers = [read_thcu_header(path) for path in source_paths]
    first_header = headers[0]
    _require(all(header == first_header for header in headers), "binary grid changed between forecast steps")
    hashes = [sha256_path(path) for path in source_paths]
    generation = _generation(data_times[0], hashes, dataset_key)
    files: list[dict[str, Any]] = []
    for step, (source, offset, data_time, digest) in enumerate(zip(source_paths, offsets_hours, data_times, hashes)):
        filename = f"{generation}-h{step:03d}.bin"
        destination = bundle_dir / filename
        _copy_immutable(source, destination)
        files.append(
            {
                "step": step,
                "offset_hours": offset,
                "data_time": data_time,
                "filename": filename,
                "bytes": destination.stat().st_size,
                "sha256": digest,
                "content_type": "application/octet-stream",
            }
        )

    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "dataset": {"key": dataset_key, "id": spec["id"]},
        "generation": generation,
        "generated_at": utc_iso(datetime.now(timezone.utc)),
        "published_at": None,
        "data_start": data_times[0],
        "data_end": data_times[-1],
        "cadence_hours": cadence,
        "dimensions": {"width": first_header["width"], "height": first_header["height"]},
        "bounds": first_header["bounds"],
        "producer": dict(provenance),
        "files": files,
    }
    if metadata:
        manifest["metadata"] = dict(metadata)
    bundle_dir.mkdir(parents=True, exist_ok=True)
    draft = bundle_dir / DRAFT_MANIFEST_NAME
    draft.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    validate_manifest(manifest, expected_dataset=dataset_key, allow_draft=True, bundle_dir=bundle_dir)
    return draft


def build_mpa_bundle(
    *,
    source_path: Path,
    bundle_dir: Path,
    source_time: str,
    feature_count: int,
    bounds: Mapping[str, float],
    provenance: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> Path:
    spec = DATASETS["mpa"]
    _require(100 <= feature_count <= 50_000, "MPA feature count is outside safety bounds")
    _require(source_path.is_file(), "MPA GeoJSON is missing")
    size = source_path.stat().st_size
    _require(100_000 <= size <= int(spec["max_file_bytes"]), "MPA GeoJSON size is outside safety bounds")
    digest = sha256_path(source_path)
    generation = _generation(source_time, [digest], "mpa")
    filename = f"{generation}-mpa.geojson"
    destination = bundle_dir / filename
    _copy_immutable(source_path, destination)
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "dataset": {"key": "mpa", "id": spec["id"]},
        "generation": generation,
        "generated_at": utc_iso(datetime.now(timezone.utc)),
        "published_at": None,
        "data_start": source_time,
        "data_end": source_time,
        "cadence_hours": None,
        "dimensions": {"feature_count": feature_count},
        "bounds": dict(bounds),
        "producer": dict(provenance),
        "files": [
            {
                "step": 0,
                "filename": filename,
                "bytes": size,
                "sha256": digest,
                "content_type": "application/geo+json",
            }
        ],
        "metadata": dict(metadata),
    }
    bundle_dir.mkdir(parents=True, exist_ok=True)
    draft = bundle_dir / DRAFT_MANIFEST_NAME
    draft.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    validate_manifest(manifest, expected_dataset="mpa", allow_draft=True, bundle_dir=bundle_dir)
    return draft


def load_manifest(path: Path) -> dict[str, Any]:
    _require(is_regular_file(path) and path.stat().st_size <= 256 * 1024, "manifest is missing, non-regular or oversized")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError("manifest is not valid UTF-8 JSON") from exc
    _require(isinstance(value, dict), "manifest root must be an object")
    return value


def validate_manifest(
    manifest: Mapping[str, Any],
    *,
    expected_dataset: str,
    allow_draft: bool,
    bundle_dir: Path | None = None,
) -> None:
    spec = DATASETS.get(expected_dataset)
    _require(spec is not None, "unknown expected dataset")
    required_root = {
        "schema_version",
        "dataset",
        "generation",
        "generated_at",
        "published_at",
        "data_start",
        "data_end",
        "cadence_hours",
        "dimensions",
        "bounds",
        "producer",
        "files",
    }
    _require(set(manifest) in (required_root, required_root | {"metadata"}), "manifest has missing or unknown top-level fields")
    if "metadata" in manifest:
        metadata = manifest["metadata"]
        _require(isinstance(metadata, dict), "metadata must be an object")
        _require(0 < len(metadata) <= 16, "metadata field count is invalid")
        for key, value in metadata.items():
            _require(isinstance(key, str) and METADATA_KEY_RE.fullmatch(key) is not None, "metadata key is invalid")
            _require(
                isinstance(value, str) and bool(value.strip()) and len(value) <= 2048,
                f"metadata {key} must be a bounded nonempty string",
            )
    _require(manifest.get("schema_version") == SCHEMA_VERSION, "manifest schema_version must be 2")
    dataset = manifest.get("dataset")
    _require(dataset == {"key": expected_dataset, "id": spec["id"]}, "manifest dataset identity mismatch")
    generation = manifest.get("generation")
    _require(isinstance(generation, str) and GENERATION_RE.fullmatch(generation) is not None, "invalid generation")
    generated_at = parse_utc(manifest.get("generated_at"), "generated_at")
    published_at = manifest.get("published_at")
    if allow_draft:
        _require(published_at is None, "draft published_at must be null")
    else:
        _require(parse_utc(published_at, "published_at") >= generated_at, "published_at precedes generated_at")
    data_start = parse_utc(manifest.get("data_start"), "data_start")
    data_end = parse_utc(manifest.get("data_end"), "data_end")
    _require(generated_at >= data_start, "generated_at precedes data_start")
    _require(data_end >= data_start, "data_end precedes data_start")
    producer = manifest.get("producer")
    _require(isinstance(producer, dict), "producer must be an object")
    _require(set(producer) == {"commit", "run_id", "run_attempt"}, "producer fields are invalid")
    _require(bool(COMMIT_RE.fullmatch(str(producer.get("commit", "")))), "invalid producer commit")
    _require(type(producer.get("run_id")) is int and producer["run_id"] > 0, "invalid producer run_id")
    _require(type(producer.get("run_attempt")) is int and producer["run_attempt"] > 0, "invalid producer run_attempt")
    bounds = manifest.get("bounds")
    _require(isinstance(bounds, dict) and set(bounds) == {"north", "south", "west", "east"}, "invalid bounds object")
    _require(all(type(bounds[k]) in (int, float) and math.isfinite(bounds[k]) for k in bounds), "bounds must be finite numbers")
    _require(bounds["north"] > bounds["south"] and bounds["east"] > bounds["west"], "bounds are not oriented")
    _require(
        -90.0 <= bounds["south"] < bounds["north"] <= 90.0
        and -180.0 <= bounds["west"] < bounds["east"] <= 180.0,
        "bounds exceed the legal coordinate domain",
    )

    files = manifest.get("files")
    _require(isinstance(files, list) and len(files) == int(spec["steps"]), "manifest file count mismatch")
    seen: set[str] = set()
    for index, entry in enumerate(files):
        _require(isinstance(entry, dict), f"files[{index}] must be an object")
        expected_file_fields = (
            {"step", "filename", "bytes", "sha256", "content_type"}
            if expected_dataset == "mpa"
            else {"step", "offset_hours", "data_time", "filename", "bytes", "sha256", "content_type"}
        )
        _require(set(entry) == expected_file_fields, f"files[{index}] fields are invalid")
        _require(type(entry.get("step")) is int and entry.get("step") == index, f"files[{index}] step mismatch")
        filename = entry.get("filename")
        _require(isinstance(filename, str) and filename == Path(filename).name, f"files[{index}] unsafe filename")
        expected_name = (
            f"{generation}-mpa.geojson" if expected_dataset == "mpa" else f"{generation}-h{index:03d}.bin"
        )
        _require(filename == expected_name and filename not in seen, f"files[{index}] immutable filename mismatch")
        seen.add(filename)
        size = entry.get("bytes")
        _require(type(size) is int and 0 < size <= int(spec["max_file_bytes"]), f"files[{index}] invalid byte count")
        digest = entry.get("sha256")
        _require(isinstance(digest, str) and SHA256_RE.fullmatch(digest) is not None, f"files[{index}] invalid SHA-256")
        expected_content_type = "application/geo+json" if expected_dataset == "mpa" else "application/octet-stream"
        _require(entry.get("content_type") == expected_content_type, f"files[{index}] content type mismatch")
        if bundle_dir is not None:
            path = bundle_dir / filename
            _require(is_regular_file(path), f"bundle asset missing or non-regular: {filename}")
            _require(path.stat().st_size == size, f"bundle asset byte mismatch: {filename}")
            _require(sha256_path(path) == digest, f"bundle asset SHA-256 mismatch: {filename}")

    _require(generation == _generation(manifest["data_start"], [entry["sha256"] for entry in files], expected_dataset), "generation digest does not match source time and assets")

    if spec["kind"] == "cmems-binary":
        cadence = int(spec["cadence_hours"])
        _require(manifest.get("cadence_hours") == cadence, "manifest cadence mismatch")
        dimensions = manifest.get("dimensions")
        _require(isinstance(dimensions, dict) and set(dimensions) == {"width", "height"}, "invalid grid dimensions")
        _require(type(dimensions.get("width")) is int and type(dimensions.get("height")) is int, "grid dimensions must be integers")
        _require(1300 <= dimensions["width"] <= 1500 and 600 <= dimensions["height"] <= 750, "implausible grid dimensions")
        longitude_step = (bounds["east"] - bounds["west"]) / (dimensions["width"] - 1)
        latitude_step = (bounds["north"] - bounds["south"]) / (dimensions["height"] - 1)
        _require(0.23 <= longitude_step <= 0.27 and 0.23 <= latitude_step <= 0.27, "grid resolution is implausible")
        exact_bytes = 30 + dimensions["width"] * dimensions["height"] * 9
        _require(all(entry["bytes"] == exact_bytes for entry in files), "CMEMS file byte count disagrees with dimensions")
        _require((data_end - data_start).total_seconds() == (int(spec["steps"]) - 1) * cadence * 3600, "data window violates cadence")
        invariant_mask_sha256: str | None = None
        for index, entry in enumerate(files):
            _require(type(entry.get("offset_hours")) is int and entry.get("offset_hours") == index * cadence, f"files[{index}] offset mismatch")
            entry_time = parse_utc(entry.get("data_time"), f"files[{index}].data_time")
            _require((entry_time - data_start).total_seconds() == index * cadence * 3600, f"files[{index}] data_time mismatch")
            if bundle_dir is not None:
                header = read_thcu_header(bundle_dir / entry["filename"])
                _require(header["width"] == dimensions["width"] and header["height"] == dimensions["height"], "THCU dimensions disagree with manifest")
                mask_sha256 = validate_thcu_payload(bundle_dir / entry["filename"], expected_dataset)
                if invariant_mask_sha256 is None:
                    invariant_mask_sha256 = mask_sha256
                _require(mask_sha256 == invariant_mask_sha256, "THCU land mask changed between forecast steps")
    else:
        _require(manifest.get("cadence_hours") is None, "MPA cadence must be null")
        dimensions = manifest.get("dimensions")
        _require(isinstance(dimensions, dict) and set(dimensions) == {"feature_count"} and type(dimensions.get("feature_count")) is int, "invalid MPA dimensions")
        _require(100 <= dimensions["feature_count"] <= 50_000, "MPA feature count outside bounds")
        _require(100_000 <= files[0]["bytes"] <= int(spec["max_file_bytes"]), "MPA GeoJSON size is outside safety bounds")
        _require(data_end == data_start, "MPA data_end must equal its registry source time")
        _require(70.0 <= bounds["west"] <= 80.0 and 165.0 <= bounds["east"] <= 180.0, "MPA longitude coverage omits Australian external territories")
        _require(-60.0 <= bounds["south"] <= -55.0 and -10.0 <= bounds["north"] <= 0.0, "MPA latitude coverage omits Australian external territories")
        _require(bounds["east"] - bounds["west"] >= 90.0 and bounds["north"] - bounds["south"] >= 45.0, "MPA overall coverage span is incomplete")
        if bundle_dir is not None:
            validate_mpa_geojson(
                bundle_dir / files[0]["filename"],
                feature_count=dimensions["feature_count"],
                expected_bounds=bounds,
            )


def validate_mpa_feature_count_review_bound(previous_count: Any, candidate_count: Any) -> None:
    """Require explicit review when an MPA snapshot changes by more than 25%."""
    _require(
        isinstance(previous_count, int) and isinstance(candidate_count, int),
        "MPA count missing during delta check",
    )
    _require(
        0.75 <= candidate_count / previous_count <= 1.25,
        "MPA feature-count delta exceeds 25% review bound",
    )


def validate_no_source_regression(current: Mapping[str, Any], candidate: Mapping[str, Any]) -> None:
    _require(current.get("dataset") == candidate.get("dataset"), "cannot compare manifests from different datasets")
    current_start = parse_utc(current.get("data_start"), "current.data_start")
    current_end = parse_utc(current.get("data_end"), "current.data_end")
    candidate_start = parse_utc(candidate.get("data_start"), "candidate.data_start")
    candidate_end = parse_utc(candidate.get("data_end"), "candidate.data_end")
    _require(candidate_start >= current_start, "candidate source start time regressed")
    _require(candidate_end >= current_end, "candidate source end time regressed")
    if candidate.get("dataset", {}).get("key") == "mpa":
        old_count = current.get("dimensions", {}).get("feature_count")
        new_count = candidate.get("dimensions", {}).get("feature_count")
        validate_mpa_feature_count_review_bound(old_count, new_count)
        old_bytes = current.get("files", [{}])[0].get("bytes")
        new_bytes = candidate.get("files", [{}])[0].get("bytes")
        _require(isinstance(old_bytes, int) and isinstance(new_bytes, int), "MPA byte count missing during delta check")
        _require(0.5 <= new_bytes / old_bytes <= 2.0, "MPA byte-size delta exceeds review bound")


def validate_same_generation_core(current: Mapping[str, Any], candidate: Mapping[str, Any]) -> None:
    _require(current.get("generation") == candidate.get("generation"), "generation values differ")
    immutable_fields = (
        "dataset",
        "generation",
        "data_start",
        "data_end",
        "cadence_hours",
        "dimensions",
        "bounds",
        "files",
        "metadata",
    )
    _require(
        all(current.get(field) == candidate.get(field) for field in immutable_fields),
        "same generation has altered immutable core metadata",
    )


def validate_bundle_layout(bundle_dir: Path, manifest: Mapping[str, Any]) -> None:
    _require(bundle_dir.is_dir() and not bundle_dir.is_symlink(), "bundle root must be a real directory")
    expected = {DRAFT_MANIFEST_NAME, *(entry["filename"] for entry in manifest.get("files", []))}
    actual = {entry.name for entry in bundle_dir.iterdir()}
    _require(actual == expected, f"bundle entries differ from manifest: expected {sorted(expected)}, got {sorted(actual)}")
    for name in expected:
        _require(is_regular_file(bundle_dir / name), f"bundle entry is not a regular file: {name}")


def validate_publication_freshness(manifest: Mapping[str, Any], now: datetime | None = None) -> None:
    """Reject stale/future source windows immediately before release mutation."""
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    generated_at = parse_utc(manifest.get("generated_at"), "generated_at")
    data_start = parse_utc(manifest.get("data_start"), "data_start")
    data_end = parse_utc(manifest.get("data_end"), "data_end")
    _require(generated_at <= current_time + timedelta(minutes=15), "generation timestamp is too far in the future")
    _require(generated_at >= current_time - timedelta(hours=6), "generated bundle is older than six hours")
    dataset_key = manifest.get("dataset", {}).get("key")
    if dataset_key in {"currents", "waves"}:
        # Waves publish twice daily on a three-hour cadence. Their last good
        # snapshot is normally about 13 hours old just before the replacement
        # run finishes, so one native step of margin prevents a deterministic
        # false-stale gap without weakening the 48-hour coverage requirement.
        maximum_source_age = 15 if dataset_key == "waves" else 12
        _require(data_start >= current_time - timedelta(hours=maximum_source_age), f"{dataset_key} source start is stale")
        _require(data_start <= current_time, f"{dataset_key} source start is in the future")
        _require(data_end >= current_time, f"{dataset_key} window does not cover publication time")
        maximum_horizon = 18 if dataset_key == "currents" else 54
        _require(data_end <= current_time + timedelta(hours=maximum_horizon), f"{dataset_key} source end is implausibly far in the future")
    elif dataset_key in {"sst", "chl", "seaice", "mld"}:
        _require(data_start >= current_time - timedelta(hours=48), f"{dataset_key} source start is stale")
        _require(data_start <= current_time, f"{dataset_key} source start is in the future")
        _require(data_end >= current_time + timedelta(hours=48), f"{dataset_key} daily forecast coverage is too short")
        _require(data_end <= current_time + timedelta(days=7), f"{dataset_key} source end is implausibly far in the future")
    elif dataset_key == "mpa":
        # CAPAD is an infrequently issued registry rather than a forecast. Ten
        # years is a deliberate hard ceiling that catches broken epoch values
        # without pretending a weekly fetch makes the underlying law current.
        _require(data_start >= current_time - timedelta(days=3653), "MPA registry source is over ten years old")
        _require(data_start <= current_time, "MPA registry source time is in the future")
    else:
        raise ContractError("unknown dataset during freshness validation")


def validate_legacy_v1_bootstrap(manifest: Mapping[str, Any], expected_dataset: str) -> None:
    """Narrowly recognize the exact rolling v1 shape for one-way v2 migration."""
    spec = DATASETS.get(expected_dataset)
    _require(spec is not None, "unknown legacy dataset")
    _require(manifest.get("version") == 1 and "schema_version" not in manifest, "not a legacy-v1 manifest")
    parse_legacy_time(manifest.get("generated_at"), "legacy.generated_at")
    if expected_dataset == "mpa":
        _require(
            set(manifest) == {"version", "generated_at", "feature_count", "data_file", "attribution"},
            "legacy MPA manifest fields are not the known v1 shape",
        )
        _require(manifest.get("data_file") == "mpa.geojson", "legacy MPA filename is not recognized")
        count = manifest.get("feature_count")
        _require(isinstance(count, int) and 100 <= count <= 50_000, "legacy MPA feature count is invalid")
        _require(manifest.get("attribution") == "© Commonwealth of Australia (DCCEEW)", "legacy MPA attribution is not recognized")
        return
    _require(set(manifest) == {"version", "generated_at", "hours"}, "legacy CMEMS manifest fields are not the known v1 shape")
    entries = manifest.get("hours")
    _require(isinstance(entries, list) and len(entries) == int(spec["steps"]), "legacy CMEMS file count mismatch")
    cadence = int(spec["cadence_hours"])
    for index, entry in enumerate(entries):
        _require(isinstance(entry, dict) and set(entry) == {"hour", "file", "bytes"}, f"legacy files[{index}] fields invalid")
        _require(entry.get("hour") == index * cadence, f"legacy files[{index}] cadence mismatch")
        _require(entry.get("file") == f"h{index:02d}.bin", f"legacy files[{index}] filename mismatch")
        size = entry.get("bytes")
        _require(isinstance(size, int) and 0 < size <= int(spec["max_file_bytes"]), f"legacy files[{index}] byte count invalid")
