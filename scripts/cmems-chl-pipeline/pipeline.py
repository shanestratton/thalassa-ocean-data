#!/usr/bin/env python3
"""
CMEMS chlorophyll-a → GitHub Release binary pipeline.

Pulls daily-mean surface chlorophyll concentration from the global
biogeochemistry forecast (GLOBAL_ANALYSISFORECAST_BGC_001_028) and
packs the values into the u-channel of our v2 THCU binary (same shape
as SST). Frontend reads a single scalar plane and renders with an
algal colour ramp.

Key difference from SST: chlorophyll concentration spans ~4 orders
of magnitude (0.01 mg/m³ in oligotrophic gyres up to ~50 mg/m³ in
productive coastal blooms). Linear encoding would saturate 90% of the
ocean to one colour bucket. We encode LOG10(chl + 0.01) normalized to
[0, 1] in the pipeline so the frontend can treat it as a linear-u8
texture without doing log math in the shader.

Binary format: v2 THCU. u = normalized log-chl [0,1], v = 0.
Frontend shader maps u directly into the algal colour ramp (u=0 →
deep purple gyres, u=1 → bright green blooms).

Dataset: cmems_mod_glo_bgc-pft_anfc_0.25deg_P1D-m is already at 0.25°
native (the resolution we coarsen currents/waves/sst down TO), so no
coarsening is needed — grab it as-is.
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

log = logging.getLogger("cmems-chl-pipeline")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ── Config ────────────────────────────────────────────────────────────────

# BGC-PFT dataset: plankton functional types + chlorophyll. CMEMS
# publishes per-variable variants — if this combined id fails the
# way the SST physics one did, try cmems_mod_glo_bgc-chl_anfc_...
DATASET_ID = "cmems_mod_glo_bgc-pft_anfc_0.25deg_P1D-m"
VARIABLES = ["chl"]  # chlorophyll concentration in mg/m³

FORECAST_DAYS = 5
# BGC native is already 0.25° = no coarsening step. We leave the
# coarsen machinery in the pipeline anyway (block size = 1 is a no-op)
# so the code path is identical to sister pipelines.
SUBSAMPLE_DEG = 0.25

# Log-normalisation range for chlorophyll. 0.01 mg/m³ = open-ocean
# oligotrophic floor, 50 mg/m³ = rare but-observed bloom peak. Cover
# the full 4-orders-of-magnitude span so every real ocean value maps
# into the colour ramp.
CHL_FLOOR = 0.01
CHL_CEILING = 50.0
CHL_LOG_MIN = math.log10(CHL_FLOOR)
CHL_LOG_MAX = math.log10(CHL_CEILING)
CHL_LOG_RANGE = CHL_LOG_MAX - CHL_LOG_MIN

RELEASE_TAG = "cmems-chl-latest"

OUT_DIR = Path(os.environ.get("CMEMS_OUT_DIR", "/tmp/cmems-chl"))
OUT_DIR.mkdir(parents=True, exist_ok=True)
BUNDLE_DIR = OUT_DIR / "bundle"

BINARY_MAGIC = b"THCU"
BINARY_VERSION = 2


# ── Steps ─────────────────────────────────────────────────────────────────


def normalise_chlorophyll_value(value_mg_m3: float) -> float:
    """Reference scalar transform used by offline and client parity fixtures."""
    if not math.isfinite(value_mg_m3) or value_mg_m3 < 0:
        raise ValueError("chlorophyll must be finite and non-negative")
    clipped = min(CHL_CEILING, max(CHL_FLOOR, value_mg_m3))
    return (math.log10(clipped) - CHL_LOG_MIN) / CHL_LOG_RANGE


def fetch_cmems(start: datetime, end: datetime) -> Path:
    """Download surface chlorophyll for the forecast window."""
    import copernicusmarine

    out_path = OUT_DIR / f"cmems-chl-{start:%Y%m%d}.nc"
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
    """Encode the multi-day NetCDF and write one .bin per forecast day."""
    import numpy as np
    import xarray as xr

    ds = xr.open_dataset(nc_path)
    if "depth" in ds.dims:
        ds = ds.isel(depth=0, drop=True)

    # Coarsening — at native 0.25° this is a 1×1 block (no-op) so we
    # preserve every native cell. Kept in the code path for parity with
    # the other pipelines in case CMEMS publishes a finer BGC grid.
    lat_res = abs(float(ds.latitude[1] - ds.latitude[0]))
    lon_res = abs(float(ds.longitude[1] - ds.longitude[0]))
    lat_block = max(1, int(round(SUBSAMPLE_DEG / lat_res)))
    lon_block = max(1, int(round(SUBSAMPLE_DEG / lon_res)))

    land_native = ds["chl"].isel(time=0).isnull().astype("float32")

    if lat_block > 1 or lon_block > 1:
        ds = ds.coarsen(latitude=lat_block, longitude=lon_block, boundary="trim").mean()
        land_frac = land_native.coarsen(latitude=lat_block, longitude=lon_block, boundary="trim").mean()
        land_da = (land_frac >= 0.5).astype("uint8")
    else:
        land_da = land_native.astype("uint8")

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
        chl_raw = ds["chl"].isel(time=i).fillna(0.01).astype(np.float32).values

        # Exact documented transform: clip physical chlorophyll into
        # [0.01, 50] mg/m³, take log10, then normalize those endpoints to
        # exactly [0,1]. No additive shift is applied.
        log_chl = np.log10(np.clip(chl_raw, CHL_FLOOR, CHL_CEILING)).astype(np.float32)
        t_norm = np.clip((log_chl - CHL_LOG_MIN) / CHL_LOG_RANGE, 0.0, 1.0).astype(np.float32)

        u = np.where(land_mask == 1, 0.0, t_norm).astype(np.float32)
        v = np.zeros_like(t_norm, dtype=np.float32)

        hour_offset = int(round(float((t - t0).astype("timedelta64[s]").astype(float)) / 3600.0))

        bin_path = OUT_DIR / f"h{i:02d}.bin"
        header = struct.pack(
            "<4sBBHHffffHH",
            BINARY_MAGIC,
            BINARY_VERSION,
            0,
            width, height,
            north, south, west, east,
            1, 0,
        )
        with bin_path.open("wb") as f:
            f.write(header)
            f.write(np.ascontiguousarray(u, dtype=np.float32).tobytes())
            f.write(np.ascontiguousarray(v, dtype=np.float32).tobytes())
            f.write(land_mask.tobytes())
        out.append((bin_path, hour_offset))

        ocean = chl_raw[land_mask == 0]
        c_min = float(np.nanmin(ocean)) if ocean.size else float("nan")
        c_max = float(np.nanmax(ocean)) if ocean.size else float("nan")
        c_med = float(np.nanmedian(ocean)) if ocean.size else float("nan")
        log.info(
            "Wrote %s (%dx%d, t=%s, T+%dh, chl min=%.3f med=%.3f max=%.2f mg/m³, %d land / %d total)",
            bin_path.name, width, height, t, hour_offset, c_min, c_med, c_max,
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
    now = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    end = now + timedelta(days=FORECAST_DAYS)

    try:
        nc_path = fetch_cmems(now, end)
        data_times = validate_cmems_source(
            nc_path,
            dataset_key="chl",
            variables=VARIABLES,
            expected_steps=6,
            cadence_hours=24,
            native_resolution=0.25,
        )
        entries = encode_daily_binaries(nc_path)
        paths = [path for path, _ in entries]
        offsets = [offset for _, offset in entries]
        validate_thcu_payloads(paths)
        build_cmems_bundle(
            dataset_key="chl",
            source_paths=paths,
            offsets_hours=offsets,
            data_times=data_times,
            bundle_dir=BUNDLE_DIR,
            provenance=producer_provenance(),
            metadata={
                "attribution": "Copernicus Marine Service",
                "encoding": "u=(log10(clip(chl,0.01,50))-log10(0.01))/(log10(50)-log10(0.01)); v=0",
            },
        )
    except Exception:  # noqa: BLE001
        log.exception("Pipeline failed")
        return 1

    log.info("✓ Generated and validated %d immutable daily snapshots in %s", len(entries), BUNDLE_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
