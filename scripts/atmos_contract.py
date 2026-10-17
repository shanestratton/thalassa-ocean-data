"""Independent THAS atmospheric scalars; never widens the existing THCU contract.

The provider decode stage must prove the original 0.25-degree grid, units, level,
run and valid time before handing these explicitly sampled 1-degree planes over.
This module and the isolated release writer use only the standard library.
"""
from __future__ import annotations

import json
import math
import struct
import sys
from array import array
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

WIDTH, HEIGHT, HEADER_BYTES = 360, 181, 64
CELLS = WIDTH * HEIGHT
BOUNDS = {"west": -180, "south": -90, "east": 179, "north": 90}
FIELDS = {
    "gust": (1, "m/s", 0, 150),
    "visibility": (2, "m", 0, 300_000),
    "pressure": (3, "Pa", 75_000, 115_000),
    "temperature": (4, "K", 150, 350),
    "dewpoint": (5, "K", 130, 350),
}
ATMOS_DATASETS = {
    "gfs-atmos": {
        "id": "noaa-gfs-0p25-to-1p00-surface-v1",
        "release_tag": "gfs-atmos-latest", "steps": 29, "cadence_hours": 6,
        "kind": "atmos-scalar", "max_file_bytes": 2 * 1024 * 1024,
        "code": 1, "model": "gfs", "max_age_hours": 12, "cycles": (0, 6, 12, 18),
        "fields": ("gust", "visibility", "pressure", "temperature", "dewpoint"),
        "attribution": "NOAA / NCEP GFS · 0.25° sampled to 1°",
        "licence": "NOAA public-domain data; no endorsement",
    },
    "ifs-pressure": {
        "id": "ecmwf-ifs-oper-0p25-to-1p00-msl-v1",
        "release_tag": "ifs-pressure-latest", "steps": 41, "cadence_hours": 6,
        "kind": "atmos-scalar", "max_file_bytes": 2 * 1024 * 1024,
        "code": 2, "model": "ecmwf", "max_age_hours": 24, "cycles": (0, 12),
        "fields": ("pressure",),
        "attribution": "Contains modified ECMWF IFS open data · 0.25° sampled to 1°",
        "licence": "CC-BY-4.0; ECMWF Terms of Use",
    },
}


def metadata_for(dataset: str) -> dict[str, str]:
    spec = ATMOS_DATASETS[dataset]
    return {
        "model": spec["model"], "source_resolution_degrees": "0.25",
        "display_resolution_degrees": "1", "fields": ",".join(spec["fields"]),
        "attribution": spec["attribution"], "licence": spec["licence"],
    }


def file_bytes(dataset: str) -> int:
    return HEADER_BYTES + CELLS * 5 * len(ATMOS_DATASETS[dataset]["fields"])


def validate_run(dataset: str, run: datetime) -> None:
    from publisher_contract import _require
    _require(run.tzinfo is not None, "atmospheric cycle lacks timezone")
    run = run.astimezone(timezone.utc)
    _require(run.year >= 2020 and run.hour in ATMOS_DATASETS[dataset]["cycles"]
             and run.minute == run.second == run.microsecond == 0,
             "invalid atmospheric model cycle")


def validate_frame(path: Path, dataset: str, run: datetime, offset: int) -> None:
    from publisher_contract import _require, is_regular_file
    spec = ATMOS_DATASETS[dataset]
    validate_run(dataset, run)
    _require(type(offset) is int and 0 <= offset <= (spec["steps"] - 1) * 6
             and offset % 6 == 0, "invalid atmospheric offset")
    _require(is_regular_file(path) and path.stat().st_size == file_bytes(dataset),
             "invalid THAS byte count")
    raw = path.read_bytes()
    header = struct.unpack("<4sBBBBHHdddddHH8B", raw[:HEADER_BYTES])
    expected = (b"THAS", 1, spec["code"], len(spec["fields"]), 0,
                WIDTH, HEIGHT, -180, -90, 1, 1, int(run.timestamp() * 1000), offset, 0,
                *(FIELDS[field][0] for field in spec["fields"]),
                *([0] * (8 - len(spec["fields"]))))
    _require(header == expected, "THAS header model/grid/run/fields mismatch")
    for channel, field in enumerate(spec["fields"]):
        _, _, low, high = FIELDS[field]
        base = HEADER_BYTES + channel * CELLS * 5
        mask = memoryview(raw)[base + CELLS * 4:base + CELLS * 5]
        values = struct.iter_unpack("<f", memoryview(raw)[base:base + CELLS * 4])
        count = 0
        for (value,), valid in zip(values, mask):
            _require((valid == 0 and value == 0) or
                     (valid == 1 and math.isfinite(value) and low <= value <= high),
                     f"THAS {field} invalid original values/mask")
            count += valid
        _require(count >= CELLS * 0.95, f"THAS {field} coverage incomplete")


def write_frame(path: Path, dataset: str, run: datetime, offset: int,
                planes: Mapping[str, tuple[Sequence[float], bytes]]) -> None:
    from publisher_contract import _require
    spec = ATMOS_DATASETS[dataset]
    validate_run(dataset, run)
    _require(set(planes) == set(spec["fields"]), "atmospheric fields incomplete")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation: a previous partial or immutable frame is never overwritten.
    with path.open("xb") as target:
        target.write(struct.pack("<4sBBBBHHdddddHH8B", b"THAS", 1, spec["code"],
                                 len(spec["fields"]), 0, WIDTH, HEIGHT, -180, -90, 1, 1,
                                 int(run.timestamp() * 1000), offset, 0,
                                 *(FIELDS[field][0] for field in spec["fields"]),
                                 *([0] * (8 - len(spec["fields"])))))
        for field in spec["fields"]:
            values, mask = planes[field]
            _require(len(values) == CELLS and len(mask) == CELLS, "atmospheric plane length")
            packed = array("f", values)
            if sys.byteorder != "little":
                packed.byteswap()
            target.write(packed.tobytes())
            target.write(mask)
    validate_frame(path, dataset, run, offset)


def validate_atmos_manifest(manifest: Mapping[str, Any], dataset: str, *,
                            allow_draft: bool, bundle_dir: Path | None = None) -> None:
    from publisher_contract import (_require, parse_utc, COMMIT_RE, SHA256_RE,
                                    GENERATION_RE, sha256_path, _generation)
    spec = ATMOS_DATASETS[dataset]
    required = {"schema_version", "dataset", "generation", "generated_at", "published_at",
                "data_start", "data_end", "cadence_hours", "dimensions", "bounds",
                "producer", "files", "metadata"}
    _require(set(manifest) == required, "atmospheric manifest fields")
    _require(manifest["schema_version"] == 2 and type(manifest["cadence_hours"]) is int
             and manifest["cadence_hours"] == 6, "atmospheric schema/cadence")
    _require(manifest["dataset"] == {"key": dataset, "id": spec["id"]}, "atmospheric dataset/model")
    _require(manifest["dimensions"] == {"width": WIDTH, "height": HEIGHT}
             and all(type(v) is int for v in manifest["dimensions"].values()), "atmospheric dimensions")
    _require(manifest["bounds"] == BOUNDS and all(type(v) in (int, float) for v in manifest["bounds"].values()), "atmospheric bounds")
    _require(manifest["metadata"] == metadata_for(dataset), "atmospheric provenance/fields")
    run = parse_utc(manifest["data_start"], "data_start")
    validate_run(dataset, run)
    generated = parse_utc(manifest["generated_at"], "generated_at")
    _require(generated >= run, "atmospheric generation precedes run")
    if allow_draft:
        _require(manifest["published_at"] is None, "atmospheric draft already published")
    else:
        _require(parse_utc(manifest["published_at"], "published_at") >= generated,
                 "atmospheric publication precedes generation")
    _require((parse_utc(manifest["data_end"], "data_end") - run).total_seconds()
             == (spec["steps"] - 1) * 6 * 3600, "atmospheric forecast incomplete")
    producer = manifest["producer"]
    _require(isinstance(producer, dict) and set(producer) == {"commit", "run_id", "run_attempt"},
             "atmospheric producer fields")
    _require(isinstance(producer["commit"], str) and COMMIT_RE.fullmatch(producer["commit"])
             and type(producer["run_id"]) is int and producer["run_id"] > 0
             and type(producer["run_attempt"]) is int and producer["run_attempt"] > 0,
             "atmospheric producer identity")
    generation = manifest["generation"]
    _require(isinstance(generation, str) and GENERATION_RE.fullmatch(generation), "atmospheric generation")
    files = manifest["files"]
    _require(isinstance(files, list) and len(files) == spec["steps"], "atmospheric cohort incomplete")
    for index, entry in enumerate(files):
        _require(isinstance(entry, dict) and set(entry) == {"step", "offset_hours", "data_time",
                 "filename", "bytes", "sha256", "content_type"}, "atmospheric frame fields")
        _require(type(entry["step"]) is int and entry["step"] == index
                 and type(entry["offset_hours"]) is int and entry["offset_hours"] == index * 6
                 and (parse_utc(entry["data_time"], "data_time") - run).total_seconds() == index * 6 * 3600
                 and entry["filename"] == f"{generation}-h{index:03d}.bin"
                 and type(entry["bytes"]) is int and entry["bytes"] == file_bytes(dataset)
                 and entry["content_type"] == "application/octet-stream"
                 and isinstance(entry["sha256"], str) and SHA256_RE.fullmatch(entry["sha256"]),
                 "atmospheric frame identity/clock/bytes")
        if bundle_dir is not None:
            path = bundle_dir / entry["filename"]
            validate_frame(path, dataset, run, index * 6)
            _require(sha256_path(path) == entry["sha256"], "THAS digest mismatch")
    _require(generation == _generation(manifest["data_start"], [f["sha256"] for f in files], dataset),
             "atmospheric generation digest mismatch")


def build_atmos_bundle(dataset: str, run: datetime, paths: Sequence[Path],
                       bundle_dir: Path, provenance: Mapping[str, Any], *,
                       generated_at: datetime | None = None) -> Path:
    from publisher_contract import (_require, _generation, _copy_immutable, sha256_path,
                                    utc_iso, DRAFT_MANIFEST_NAME)
    spec = ATMOS_DATASETS[dataset]
    validate_run(dataset, run)
    _require(len(paths) == spec["steps"], "atmospheric cohort incomplete")
    for index, path in enumerate(paths):
        validate_frame(path, dataset, run, index * 6)
    hashes = [sha256_path(path) for path in paths]
    start = utc_iso(run)
    generation = _generation(start, hashes, dataset)
    from datetime import timedelta
    files = []
    for index, (path, digest) in enumerate(zip(paths, hashes)):
        name = f"{generation}-h{index:03d}.bin"
        _copy_immutable(path, bundle_dir / name)
        files.append({"step": index, "offset_hours": index * 6,
                      "data_time": utc_iso(run + timedelta(hours=index * 6)),
                      "filename": name, "bytes": path.stat().st_size, "sha256": digest,
                      "content_type": "application/octet-stream"})
    manifest = {
        "schema_version": 2, "dataset": {"key": dataset, "id": spec["id"]},
        "generation": generation, "generated_at": utc_iso(generated_at or datetime.now(timezone.utc)),
        "published_at": None, "data_start": start,
        "data_end": utc_iso(run + timedelta(hours=(spec["steps"] - 1) * 6)),
        "cadence_hours": 6, "dimensions": {"width": WIDTH, "height": HEIGHT},
        "bounds": dict(BOUNDS), "producer": dict(provenance), "files": files,
        "metadata": metadata_for(dataset),
    }
    validate_atmos_manifest(manifest, dataset, allow_draft=True, bundle_dir=bundle_dir)
    draft = bundle_dir / DRAFT_MANIFEST_NAME
    with draft.open("x", encoding="utf-8", newline="\n") as target:
        target.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return draft
