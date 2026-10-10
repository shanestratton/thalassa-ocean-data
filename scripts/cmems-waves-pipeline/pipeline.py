#!/usr/bin/env python3
"""
CMEMS ocean-waves → GitHub Release binary pipeline.

Runs twice daily via GitHub Action. Pulls the Copernicus Marine global wave
analysis+forecast (GLOBAL_ANALYSISFORECAST_WAV_001_027), significant
height + mean direction only, area-averages to a grid matching the
currents layer, converts direction + height into u/v components so the
existing CurrentParticleLayer-style renderer can ingest it directly
(same binary format, same parser), and seals an immutable bundle for the
separate credential-isolated publisher job.

Sister pipeline to cmems-currents-pipeline; same v2 binary format so
the frontend parser reuses all the same code paths. The only real
difference is the source dataset + the VHM0/VMDR → u/v conversion.

Binary file format (little-endian) — v2, identical to currents:
    bytes  0..3   magic 'THCU' (kept so existing parser works unchanged)
    byte   4      version (2)
    byte   5      reserved (0)
    u16    6..7   width
    u16    8..9   height
    f32   10..13  north (decimal degrees)
    f32   14..17  south
    f32   18..21  west
    f32   22..25  east
    u16   26..27  hours  (always 1 per file)
    u16   28..29  reserved (0)
    // pixel data, row-major, north-to-south, west-to-east:
    f32[width*height] u           (east "wave velocity" = VHM0 * sin(to_dir), m)
    f32[width*height] v           (north "wave velocity" = VHM0 * cos(to_dir), m)
    u8 [width*height] land_mask   (1=land, 0=ocean)

u/v are the VHM0 magnitude projected onto cardinal axes using the
to-direction (VMDR + 180°, since VMDR is in meteorological "from"
convention). Downstream this lets the particle layer advect and colour
particles by the same vector-field math as currents — the only UI-side
difference is the colour-ramp bounds (a 6m swell is "red" where a 1.5
m/s current is "red").

Each time step is packaged under an immutable generation filename. The
dataset is 3-hourly, so step 0 = T+0h, step 1 = T+3h, etc.
"""
from __future__ import annotations

import logging
import math
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

log = logging.getLogger("cmems-waves-pipeline")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ── Config ────────────────────────────────────────────────────────────────

DATASET_ID = "cmems_mod_glo_wav_anfc_0.083deg_PT3H-i"
# VHM0 = significant wave height (m)
# VMDR = mean wave direction (degrees, meteorological "from" convention)
VARIABLES = ["VHM0", "VMDR"]
# 48h forecast window — waves matter more for passage planning than
# currents (storms develop over days), so a longer horizon is useful.
# At 3-hourly native cadence, 48h = 17 snapshots.
FORECAST_HOURS = 48
# 0.25° to match currents layer and share the same parser + mesh.
SUBSAMPLE_DEG = 0.25

RELEASE_TAG = "cmems-waves-latest"

OUT_DIR = Path(os.environ.get("CMEMS_OUT_DIR", "/tmp/cmems-waves"))
OUT_DIR.mkdir(parents=True, exist_ok=True)
BUNDLE_DIR = OUT_DIR / "bundle"

# Same magic as currents — the frontend parser checks this; reusing it
# means no new parsing code. "THCU" stood for Thalassa Currents but is
# effectively now "Thalassa Cartesian-UV binary" which covers both.
BINARY_MAGIC = b"THCU"
BINARY_VERSION = 2
# Below this dimensionless resultant/height ratio, a circular mean has no
# trustworthy direction (for example, equal opposing systems).  Floating
# point noise must not turn that cancellation into an arbitrary bearing.
CIRCULAR_COHERENCE_EPSILON = 1e-6


# ── Steps ─────────────────────────────────────────────────────────────────


def dominant_wave_direction(heights: list[float], directions_from_degrees: list[float]) -> float:
    """Direction of the tallest native system; ties use lowest [0, 360) bearing."""
    if not heights or len(heights) != len(directions_from_degrees):
        raise ValueError("heights and directions must be non-empty and equally sized")
    if any(not math.isfinite(value) or value < 0 for value in heights):
        raise ValueError("wave heights must be finite and non-negative")
    if any(not math.isfinite(value) or value < 0 or value > 360 for value in directions_from_degrees):
        raise ValueError("wave directions must be finite and in [0, 360]")
    maximum = max(heights)
    return min(direction % 360.0 for height, direction in zip(heights, directions_from_degrees) if height == maximum)


def circular_wave_vector(heights: list[float], directions_from_degrees: list[float]) -> tuple[float, float]:
    """Height-honest vector using a deterministic dominant-system fallback."""
    fallback = dominant_wave_direction(heights, directions_from_degrees)
    height = sum(heights) / len(heights)
    sin_sum = sum(h * math.sin(math.radians(direction)) for h, direction in zip(heights, directions_from_degrees))
    cos_sum = sum(h * math.cos(math.radians(direction)) for h, direction in zip(heights, directions_from_degrees))
    norm = math.hypot(sin_sum, cos_sum)
    if norm <= max(sum(heights) * CIRCULAR_COHERENCE_EPSILON, 1e-12):
        direction = math.radians(fallback)
        unit_sin, unit_cos = math.sin(direction), math.cos(direction)
    else:
        unit_sin, unit_cos = sin_sum / norm, cos_sum / norm
    return -height * unit_sin, -height * unit_cos


def dominant_direction_grid(height_values, direction_values, lat_block: int, lon_block: int):
    """Select each block's tallest native system with a stable bearing tie-break."""
    import numpy as np

    heights = np.asarray(height_values)
    directions = np.asarray(direction_values)
    if heights.ndim != 2 or heights.shape != directions.shape or lat_block <= 0 or lon_block <= 0:
        raise ValueError("invalid native wave grids or block shape")
    out_height = heights.shape[0] // lat_block
    out_width = heights.shape[1] // lon_block
    if out_height == 0 or out_width == 0:
        raise ValueError("wave grid is smaller than its coarsening block")
    lat_stop = out_height * lat_block
    lon_stop = out_width * lon_block
    best_height = np.full((out_height, out_width), -np.inf, dtype=np.float64)
    best_direction = np.full((out_height, out_width), np.inf, dtype=np.float64)
    for lat_offset in range(lat_block):
        for lon_offset in range(lon_block):
            sample_height = heights[lat_offset:lat_stop:lat_block, lon_offset:lon_stop:lon_block]
            sample_direction = np.mod(
                directions[lat_offset:lat_stop:lat_block, lon_offset:lon_stop:lon_block],
                360.0,
            )
            valid = np.isfinite(sample_height) & np.isfinite(sample_direction) & (sample_height >= 0)
            choose = valid & (
                (sample_height > best_height)
                | ((sample_height == best_height) & (sample_direction < best_direction))
            )
            np.copyto(best_height, sample_height, where=choose)
            np.copyto(best_direction, sample_direction, where=choose)
    # Fully missing blocks are land and are zeroed later; avoid propagating an
    # infinite placeholder through trigonometric functions in the meantime.
    best_direction[~np.isfinite(best_direction)] = 0.0
    return best_direction


def fetch_cmems(start: datetime, end: datetime) -> Path:
    """Download surface waves for the forecast window as a single NetCDF."""
    import copernicusmarine  # lazy import

    out_path = OUT_DIR / f"cmems-waves-{start:%Y%m%dT%H}.nc"
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


def encode_hourly_binaries(nc_path: Path) -> list[tuple[Path, int]]:
    """Coarsen the multi-hour NetCDF and write one .bin per forecast step.

    Returns list of (path, hour_offset) tuples so the manifest builder
    can record actual forecast hours (0, 3, 6, 9, ...) rather than step
    indices.
    """
    import numpy as np
    import xarray as xr

    ds = xr.open_dataset(nc_path)
    if "depth" in ds.dims:
        ds = ds.squeeze("depth", drop=True)

    # Build a NATIVE-resolution land mask BEFORE filling NaNs. CMEMS marks
    # land cells as NaN — use VHM0's NaN pattern as the truth.
    lat_block = max(1, int(round(SUBSAMPLE_DEG / abs(float(ds.latitude[1] - ds.latitude[0])))))
    lon_block = max(1, int(round(SUBSAMPLE_DEG / abs(float(ds.longitude[1] - ds.longitude[0])))))

    land_native = ds["VHM0"].isel(time=0).isnull().astype("float32")  # 1=land, 0=ocean

    # Heights can be averaged arithmetically, but directions cannot: 359° and
    # 1° mean to 0°, not 180°. Compute a height-weighted circular direction at
    # native resolution, then normalize it so output vector magnitude remains
    # exactly the coarsened significant wave height.
    height_native = ds["VHM0"]
    direction_native = ds["VMDR"]
    direction_radians = np.deg2rad(direction_native)
    height_da = height_native.coarsen(latitude=lat_block, longitude=lon_block, boundary="trim").mean()
    weighted_sin_da = (height_native * np.sin(direction_radians)).coarsen(
        latitude=lat_block, longitude=lon_block, boundary="trim"
    ).mean()
    weighted_cos_da = (height_native * np.cos(direction_radians)).coarsen(
        latitude=lat_block, longitude=lon_block, boundary="trim"
    ).mean()
    land_frac = land_native.coarsen(latitude=lat_block, longitude=lon_block, boundary="trim").mean()
    land_da = (land_frac >= 0.5).astype("uint8")

    # Reverse latitude so output is row-major north→south (client convention).
    height_da = height_da.reindex(latitude=height_da.latitude[::-1])
    weighted_sin_da = weighted_sin_da.reindex(latitude=weighted_sin_da.latitude[::-1])
    weighted_cos_da = weighted_cos_da.reindex(latitude=weighted_cos_da.latitude[::-1])
    land_da = land_da.reindex(latitude=land_da.latitude[::-1])

    height = height_da.sizes["latitude"]
    width = height_da.sizes["longitude"]
    north = float(height_da.latitude[0])
    south = float(height_da.latitude[-1])
    west = float(height_da.longitude[0])
    east = float(height_da.longitude[-1])

    land_mask = np.ascontiguousarray(land_da.values, dtype=np.uint8)
    land_count = int(land_mask.sum())

    # Pre-compute hour offsets from the first time step
    times = height_da.time.values
    t0 = times[0]

    out: list[tuple[Path, int]] = []
    for i, t in enumerate(times):
        # VHM0 = significant wave height (m)
        # VMDR = mean wave direction, "from" convention in degrees
        H = height_da.isel(time=i).fillna(0.0).astype(np.float32).values
        weighted_sin = weighted_sin_da.isel(time=i).fillna(0.0).astype(np.float32).values
        weighted_cos = weighted_cos_da.isel(time=i).fillna(0.0).astype(np.float32).values
        norm = np.hypot(weighted_sin, weighted_cos)
        # A near-zero circular resultant has no meaningful mean direction.
        # Preserve the honest arithmetic mean VHM0, but use the direction of
        # the maximum-height native system. Equal maxima choose the smallest
        # normalized from-bearing, making the result permutation-independent.
        fallback_direction = dominant_direction_grid(
            height_native.isel(time=i).values,
            direction_native.isel(time=i).values,
            lat_block,
            lon_block,
        )
        fallback_rad = np.deg2rad(fallback_direction[::-1, :])
        coherent = norm > np.maximum(H * CIRCULAR_COHERENCE_EPSILON, 1e-12)
        safe_norm = np.maximum(norm, 1e-12)
        unit_sin = np.where(coherent, weighted_sin / safe_norm, np.sin(fallback_rad))
        unit_cos = np.where(coherent, weighted_cos / safe_norm, np.cos(fallback_rad))
        u = (-H * unit_sin).astype(np.float32)
        v = (-H * unit_cos).astype(np.float32)
        u = np.where(land_mask == 1, 0.0, u).astype(np.float32)
        v = np.where(land_mask == 1, 0.0, v).astype(np.float32)

        # Hour offset in whole hours from the first time step. NumPy
        # timedelta64 → float64 seconds via .astype('timedelta64[s]').
        hour_offset = int(round(float((t - t0).astype("timedelta64[s]").astype(float)) / 3600.0))

        bin_path = OUT_DIR / f"h{i:02d}.bin"
        header = struct.pack(
            "<4sBBHHffffHH",
            BINARY_MAGIC,
            BINARY_VERSION,
            0,
            width, height,
            north, south, west, east,
            1,   # hours in this file
            0,   # reserved
        )
        with bin_path.open("wb") as f:
            f.write(header)
            f.write(np.ascontiguousarray(u, dtype=np.float32).tobytes())
            f.write(np.ascontiguousarray(v, dtype=np.float32).tobytes())
            f.write(land_mask.tobytes())
        out.append((bin_path, hour_offset))
        log.info(
            "Wrote %s (%dx%d, t=%s, T+%dh, %d bytes, %d land / %d total)",
            bin_path.name, width, height, t, hour_offset, bin_path.stat().st_size,
            land_count, width * height,
        )
    return out


def require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        log.error("Missing required env var %s", name)
        sys.exit(2)
    return val


def main() -> int:
    # Align to the nearest 3-hour slot before NOW so we catch the freshest
    # analysis snapshot. CMEMS WAV forecasts run at 00/06/12/18 UTC and
    # publish ~2h later.
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    now = now.replace(hour=now.hour - (now.hour % 3))
    end = now + timedelta(hours=FORECAST_HOURS)

    try:
        nc_path = fetch_cmems(now, end)
        data_times = validate_cmems_source(
            nc_path,
            dataset_key="waves",
            variables=VARIABLES,
            expected_steps=17,
            cadence_hours=3,
            native_resolution=1 / 12,
            # VMDR is finite on a bounded coastal/ice-edge fringe outside
            # VHM0's wet mask. VHM0's mask stays the published land truth;
            # fringe directions are annihilated by the NaN height weighting
            # and zeroed by the land mask, so nothing outside it is encoded.
            finite_superset_variables=frozenset({"VMDR"}),
        )
        entries = encode_hourly_binaries(nc_path)
        paths = [path for path, _ in entries]
        offsets = [offset for _, offset in entries]
        validate_thcu_payloads(paths)
        build_cmems_bundle(
            dataset_key="waves",
            source_paths=paths,
            offsets_hours=offsets,
            data_times=data_times,
            bundle_dir=BUNDLE_DIR,
            provenance=producer_provenance(),
            metadata={
                "attribution": "Copernicus Marine Service",
                "encoding": "VHM0 magnitude projected toward VMDR + 180 degrees",
            },
        )
    except Exception:  # noqa: BLE001
        log.exception("Pipeline failed")
        return 1

    log.info("✓ Generated and validated %d immutable snapshots in %s", len(entries), BUNDLE_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
