#!/usr/bin/env python3
"""
CMEMS ocean-currents → GitHub Release binary pipeline.

Runs daily via GitHub Action. Pulls the Copernicus Marine global physics
forecast (GLOBAL_ANALYSISFORECAST_PHY_001_024), surface currents only,
subsamples to a grid suitable for client-side WebGL particle rendering,
encodes each hour as a compact binary blob, and attaches the blobs to
a rolling GitHub release so the Thalassa web client can fetch them
directly (no third-party tile server dependency).

Binary file format (little-endian):
    bytes  0..3   magic 'THCU' (Thalassa Currents)
    byte   4      version (2 — v1 was identical sans land mask plane)
    byte   5      reserved (0)
    u16    6..7   width
    u16    8..9   height
    f32   10..13  north (decimal degrees)
    f32   14..17  south
    f32   18..21  west
    f32   22..25  east
    u16   26..27  hours  (always 1 per file — one file per forecast hour)
    u16   28..29  reserved (0)
    // pixel data, row-major, north-to-south, west-to-east:
    f32[width*height] u           (east velocity, m/s)
    f32[width*height] v           (north velocity, m/s)
    u8 [width*height] land_mask   (1=land, 0=ocean) — v2+ only

Each forecast hour is packaged under an immutable generation filename.
"""
from __future__ import annotations

import logging
import os
import struct
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from cmems_contract import validate_cmems_source, validate_thcu_payloads
from publisher_contract import build_cmems_bundle, producer_provenance

log = logging.getLogger("cmems-pipeline")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ── Config ────────────────────────────────────────────────────────────────

DATASET_ID = "cmems_mod_glo_phy_anfc_merged-uv_PT1H-i"
VARIABLES = ["uo", "vo"]
# 12h forecast horizon — near-term passage-planning window.
FORECAST_HOURS = 12
# Subsample to this resolution (degrees). 0.25° = 1440×681 ≈ 980k cells per
# hour × 9 bytes (u+v float32 + land u8) = ~9 MB/hour uncompressed.
# 13 hours total ≈ 117 MB, within GitHub Release's 2 GB asset cap.
# Was 0.5° but at that resolution the East Australian Current and other
# narrow western-boundary currents alias to ~1 cell wide and disappear
# under nearest-neighbor downsampling.
SUBSAMPLE_DEG = 0.25

# GitHub Release tag to update daily. Must exist before first run — the
# workflow step creates it if missing.
RELEASE_TAG = "cmems-currents-latest"

OUT_DIR = Path(os.environ.get("CMEMS_OUT_DIR", "/tmp/cmems-currents"))
OUT_DIR.mkdir(parents=True, exist_ok=True)
BUNDLE_DIR = OUT_DIR / "bundle"

BINARY_MAGIC = b"THCU"
BINARY_VERSION = 2  # v1 = no land-mask plane; v2 adds u8[w*h] mask after v


# ── Steps ─────────────────────────────────────────────────────────────────


def fetch_cmems(start: datetime, end: datetime) -> Path:
    """Download surface currents for the forecast window as a single NetCDF."""
    import copernicusmarine  # lazy import

    out_path = OUT_DIR / f"cmems-currents-{start:%Y%m%dT%H}.nc"
    username = require_env("COPERNICUS_MARINE_USERNAME")
    password = require_env("COPERNICUS_MARINE_PASSWORD")

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        log.info(
            "Fetching %s → %s into %s (attempt %d/%d)",
            start.isoformat(), end.isoformat(), out_path, attempt, max_attempts,
        )
        try:
            copernicusmarine.subset(
                dataset_id=DATASET_ID,
                variables=VARIABLES,
                minimum_longitude=-180,
                maximum_longitude=180,
                minimum_latitude=-80,
                maximum_latitude=90,
                start_datetime=start.strftime("%Y-%m-%dT%H:%M:%S"),
                end_datetime=end.strftime("%Y-%m-%dT%H:%M:%S"),
                output_filename=out_path.name,
                output_directory=str(out_path.parent),
                overwrite=True,
                username=username,
                password=password,
            )
            return out_path
        except Exception as exc:  # noqa: BLE001
            msg = f"{type(exc).__name__}: {exc}"
            is_transient = any(
                sig in msg
                for sig in (
                    "CouldNotConnectToAuthenticationSystem",
                    "ConnectTimeout", "ReadTimeout", "ConnectionError",
                )
            )
            if is_transient and attempt < max_attempts:
                wait_s = 90 * attempt
                log.warning("Transient CMEMS auth failure: %s — retrying in %ds", msg, wait_s)
                time.sleep(wait_s)
                continue
            raise


def encode_hourly_binaries(nc_path: Path) -> list[Path]:
    """Coarsen the multi-hour NetCDF and write one .bin per forecast hour."""
    import numpy as np
    import xarray as xr

    ds = xr.open_dataset(nc_path)
    if "depth" in ds.dims:
        ds = ds.squeeze("depth", drop=True)

    # Build a NATIVE-resolution land mask BEFORE filling NaNs. CMEMS marks
    # land cells as NaN — that's our source of truth.
    lat_block = max(1, int(round(SUBSAMPLE_DEG / abs(float(ds.latitude[1] - ds.latitude[0])))))
    lon_block = max(1, int(round(SUBSAMPLE_DEG / abs(float(ds.longitude[1] - ds.longitude[0])))))

    land_native = ds["uo"].isel(time=0).isnull().astype("float32")  # 1=land, 0=ocean

    # Area-average (coarsen+mean) instead of nearest-neighbor subsampling.
    # Critical for narrow western-boundary currents (EAC, Gulf Stream,
    # Kuroshio): nearest-neighbor at 0.25° picks every Nth native cell and
    # frequently grabs the edge of the current rather than its peak,
    # quantizing the visible flow away. Area-averaging preserves it.
    ds = ds.coarsen(latitude=lat_block, longitude=lon_block, boundary="trim").mean()
    land_frac = land_native.coarsen(latitude=lat_block, longitude=lon_block, boundary="trim").mean()
    # Cell is "land" if more than half of its contributing native cells were
    # land. Threshold means coastal cells (mixed) lean ocean — we'd rather
    # paint slightly into the coast than not show the EAC at all.
    land_da = (land_frac >= 0.5).astype("uint8")

    # Ensure lat goes north→south in output (client expects row-major
    # north-to-south). CMEMS native is south→north.
    ds = ds.reindex(latitude=ds.latitude[::-1])
    land_da = land_da.reindex(latitude=land_da.latitude[::-1])

    height = ds.sizes["latitude"]
    width = ds.sizes["longitude"]
    north = float(ds.latitude[0])
    south = float(ds.latitude[-1])
    west = float(ds.longitude[0])
    east = float(ds.longitude[-1])

    land_mask = np.ascontiguousarray(land_da.values, dtype=np.uint8)  # (h, w)
    land_count = int(land_mask.sum())

    out_paths: list[Path] = []
    for i, t in enumerate(ds.time.values):
        u = ds["uo"].isel(time=i).fillna(0.0).astype(np.float32).values
        v = ds["vo"].isel(time=i).fillna(0.0).astype(np.float32).values
        u = np.where(land_mask == 1, 0.0, u).astype(np.float32)
        v = np.where(land_mask == 1, 0.0, v).astype(np.float32)

        bin_path = OUT_DIR / f"h{i:02d}.bin"
        header = struct.pack(
            "<4sBBHHffffHH",
            BINARY_MAGIC,
            BINARY_VERSION,
            0,
            width, height,
            north, south, west, east,
            1,  # hours in this file
            0,  # reserved
        )
        with bin_path.open("wb") as f:
            f.write(header)
            f.write(np.ascontiguousarray(u, dtype=np.float32).tobytes())
            f.write(np.ascontiguousarray(v, dtype=np.float32).tobytes())
            f.write(land_mask.tobytes())
        out_paths.append(bin_path)
        log.info(
            "Wrote %s (%dx%d, time=%s, %d bytes, %d land cells / %d total)",
            bin_path.name, width, height, t, bin_path.stat().st_size,
            land_count, width * height,
        )
    return out_paths


def require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        log.error("Missing required env var %s", name)
        sys.exit(2)
    return val


def main() -> int:
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    end = now + timedelta(hours=FORECAST_HOURS)

    try:
        nc_path = fetch_cmems(now, end)
        data_times = validate_cmems_source(
            nc_path,
            dataset_key="currents",
            variables=VARIABLES,
            expected_steps=13,
            cadence_hours=1,
            native_resolution=1 / 12,
        )
        bins = encode_hourly_binaries(nc_path)
        validate_thcu_payloads(bins)
        build_cmems_bundle(
            dataset_key="currents",
            source_paths=bins,
            offsets_hours=list(range(13)),
            data_times=data_times,
            bundle_dir=BUNDLE_DIR,
            provenance=producer_provenance(),
            metadata={"attribution": "Copernicus Marine Service"},
        )
    except Exception:  # noqa: BLE001
        log.exception("Pipeline failed")
        return 1

    log.info("✓ Generated and validated %d immutable hourly binaries in %s", len(bins), BUNDLE_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
