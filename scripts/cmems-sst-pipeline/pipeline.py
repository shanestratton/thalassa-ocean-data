#!/usr/bin/env python3
"""
CMEMS sea-surface-temperature → GitHub Release binary pipeline.

Pulls daily-mean potential temperature at the surface from the global
physics forecast (GLOBAL_ANALYSISFORECAST_PHY_001_024), coarsens to the
same 0.25° grid as the currents + waves layers, packs temperature °C
into the u-channel of our v2 THCU binary (v-channel is zero — it's a
scalar field so the vector format has 50% slack, but reusing the binary
format means the parser and edge function are identical). 5-day forecast
= 5 daily snapshots.

Rendering side: a dedicated SstRasterLayer renders only the heatmap pass
(no particles — a scalar field has no direction to animate), with a
temperature-tuned colour ramp.

Binary format: identical to v2 THCU. u=temperature (°C), v=0.
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

log = logging.getLogger("cmems-sst-pipeline")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ── Config ────────────────────────────────────────────────────────────────

DATASET_ID = "cmems_mod_glo_phy-thetao_anfc_0.083deg_P1D-m"
# thetao = sea water potential temperature (°C). Has a depth dim; we
# select the surface level (index 0 = ~0.494m).
VARIABLES = ["thetao"]
# 5-day forecast, daily cadence = 5 snapshots. SST barely changes over
# a day, no point pulling hourly.
FORECAST_DAYS = 5
SUBSAMPLE_DEG = 0.25

RELEASE_TAG = "cmems-sst-latest"

OUT_DIR = Path(os.environ.get("CMEMS_OUT_DIR", "/tmp/cmems-sst"))
OUT_DIR.mkdir(parents=True, exist_ok=True)
BUNDLE_DIR = OUT_DIR / "bundle"

BINARY_MAGIC = b"THCU"
BINARY_VERSION = 2


# ── Steps ─────────────────────────────────────────────────────────────────


def fetch_cmems(start: datetime, end: datetime) -> Path:
    """Download surface temperature for the forecast window as a single NetCDF."""
    import copernicusmarine

    out_path = OUT_DIR / f"cmems-sst-{start:%Y%m%d}.nc"
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
                # Constrain depth to surface only — P1D-m has many levels
                # but we only want the top.
                minimum_depth=0,
                maximum_depth=1,
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


def encode_daily_binaries(nc_path: Path) -> list[tuple[Path, int]]:
    """Coarsen the multi-day NetCDF and write one .bin per forecast day.

    Returns list of (path, hour_offset) tuples; hour offsets are 0, 24,
    48, 72, 96 for a 5-day forecast so the shared frontend scrubber
    handles SST identically to currents/waves despite the coarser
    temporal cadence.
    """
    import numpy as np
    import xarray as xr

    ds = xr.open_dataset(nc_path)
    # Select the surface level even if multiple depths came back.
    if "depth" in ds.dims:
        ds = ds.isel(depth=0, drop=True)

    # NATIVE-resolution land mask from thetao's NaN pattern (ocean is
    # numeric, land is NaN in CMEMS).
    lat_block = max(1, int(round(SUBSAMPLE_DEG / abs(float(ds.latitude[1] - ds.latitude[0])))))
    lon_block = max(1, int(round(SUBSAMPLE_DEG / abs(float(ds.longitude[1] - ds.longitude[0])))))

    land_native = ds["thetao"].isel(time=0).isnull().astype("float32")

    ds = ds.coarsen(latitude=lat_block, longitude=lon_block, boundary="trim").mean()
    land_frac = land_native.coarsen(latitude=lat_block, longitude=lon_block, boundary="trim").mean()
    land_da = (land_frac >= 0.5).astype("uint8")

    ds = ds.reindex(latitude=ds.latitude[::-1])
    land_da = land_da.reindex(latitude=land_da.latitude[::-1])

    height = ds.sizes["latitude"]
    width = ds.sizes["longitude"]
    north = float(ds.latitude[0])
    south = float(ds.latitude[-1])
    west = float(ds.longitude[0])
    east = float(ds.longitude[-1])

    land_mask = np.ascontiguousarray(land_da.values, dtype=np.uint8)
    land_count = int(land_mask.sum())

    times = ds.time.values
    t0 = times[0]

    out: list[tuple[Path, int]] = []
    for i, t in enumerate(times):
        # thetao is °C. Fill NaN (land) with 0 for the binary — the land
        # mask plane handles "don't paint here" so the numeric value
        # doesn't matter anywhere it's masked.
        T = ds["thetao"].isel(time=i).fillna(0.0).astype(np.float32).values

        # Pack scalar temperature into u-channel. v-channel is zero so
        # we can reuse the v2 THCU binary unchanged. Wastes 50% of the
        # cell bytes but keeps the parser + edge-fn identical across
        # currents/waves/sst. At 5 snapshots × ~9MB = 45MB total; the
        # wasted bytes aren't worth a separate binary format yet.
        u = np.where(land_mask == 1, 0.0, T).astype(np.float32)  # temperature in °C
        v = np.zeros_like(T, dtype=np.float32)

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

        # Compute ocean-only min/max for the log so we can spot bad
        # encoding (e.g., if the dataset returns Kelvin instead of °C).
        ocean = T[land_mask == 0]
        t_min = float(ocean.min()) if ocean.size else float("nan")
        t_max = float(ocean.max()) if ocean.size else float("nan")
        log.info(
            "Wrote %s (%dx%d, t=%s, T+%dh, SST range [%.2f, %.2f]°C, %d land / %d total)",
            bin_path.name, width, height, t, hour_offset, t_min, t_max,
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
    # CMEMS P1D-m is daily-mean — align the request to the UTC day
    # boundary so we get clean daily centroids.
    now = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    end = now + timedelta(days=FORECAST_DAYS)

    try:
        nc_path = fetch_cmems(now, end)
        data_times = validate_cmems_source(
            nc_path,
            dataset_key="sst",
            variables=VARIABLES,
            expected_steps=6,
            cadence_hours=24,
            native_resolution=1 / 12,
        )
        entries = encode_daily_binaries(nc_path)
        paths = [path for path, _ in entries]
        offsets = [offset for _, offset in entries]
        validate_thcu_payloads(paths)
        build_cmems_bundle(
            dataset_key="sst",
            source_paths=paths,
            offsets_hours=offsets,
            data_times=data_times,
            bundle_dir=BUNDLE_DIR,
            provenance=producer_provenance(),
            metadata={"attribution": "Copernicus Marine Service", "encoding": "thetao degrees Celsius in u; v=0"},
        )
    except Exception:  # noqa: BLE001
        log.exception("Pipeline failed")
        return 1

    log.info("✓ Generated and validated %d immutable daily snapshots in %s", len(entries), BUNDLE_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
