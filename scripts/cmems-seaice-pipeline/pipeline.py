#!/usr/bin/env python3
"""
CMEMS sea-ice concentration → GitHub Release binary pipeline.

Pulls daily-mean sea-ice fraction from the global physics
analysis-forecast suite (`siconc` variable, 0–1 fraction) and packs
the values into the u-channel of our v2 THCU binary (same shape as
SST and chl). Frontend reads a single scalar plane and renders with
an ice-white colour ramp that fades smoothly to transparent below the
15% concentration "ice edge" threshold meteorologists use.

Why sea-ice: unlocks high-latitude routing (Baltic winter, Alaska,
Svalbard, Antarctic) — the last globally-relevant data layer the
Thalassa map didn't cover. Roadmap item 3.5 final piece.

Notes on the data:
- siconc is dimensionless [0, 1] — already-normalised, no log scale or
  offset like chl/SST need. We pack it directly into the u-channel.
- 15% concentration = "ice edge" by convention. Below that we let the
  shader discard so polar routes through marginal ice show clean ocean.
- 100% = consolidated pack ice (think central Arctic Ocean in winter).

Dataset: cmems_mod_glo_phy-siconc_anfc_0.083deg_P1D-m. Same per-variable
physics convention as SST (cmems_mod_glo_phy-thetao_...). Coarsened to
0.25° globally to match sister pipelines and keep file sizes sane.
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

log = logging.getLogger("cmems-seaice-pipeline")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ── Config ────────────────────────────────────────────────────────────────

# Sea-ice variables live in the COMBINED daily-mean physics dataset
# (unlike thetao, which CMEMS split into a per-variable variant). The
# per-variable -siconc variant doesn't exist (DatasetNotFound).
# Per Google Earth Engine's catalog, the combined daily 18-band product
# `cmems_mod_glo_phy_anfc_0.083deg_P1D-m` carries all 9 sea-ice
# variables (siconc, sithick, sialb, siage, ist, sivelo, usi, vsi,
# sisnthick) plus zos / mlotst / tob / sob / pbo / etc.
DATASET_ID = "cmems_mod_glo_phy_anfc_0.083deg_P1D-m"
VARIABLES = ["siconc"]  # sea-ice concentration, dimensionless [0, 1]

FORECAST_DAYS = 5
# Coarsen 0.083° → 0.25° to match the rest of the CMEMS layer suite.
# Sea ice is naturally smooth — fronts evolve over days, not hours, and
# polar regions are vast — so 0.25° loses essentially nothing visible
# at marine routing zoom (4-12) while shrinking each daily binary by ~9×.
SUBSAMPLE_DEG = 0.25

RELEASE_TAG = "cmems-seaice-latest"

OUT_DIR = Path(os.environ.get("CMEMS_OUT_DIR", "/tmp/cmems-seaice"))
OUT_DIR.mkdir(parents=True, exist_ok=True)
BUNDLE_DIR = OUT_DIR / "bundle"

BINARY_MAGIC = b"THCU"
BINARY_VERSION = 2


# ── Steps ─────────────────────────────────────────────────────────────────


def fetch_cmems(start: datetime, end: datetime) -> Path:
    """Download sea-ice concentration for the forecast window."""
    import copernicusmarine

    out_path = OUT_DIR / f"cmems-seaice-{start:%Y%m%d}.nc"
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
                # Full latitude range — we want both Arctic and Antarctic
                # ice. CMEMS's tripolar→regular regridded grid typically
                # tops out around 89.95°N / -80°S.
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


def encode_daily_binaries(nc_path: Path) -> list[tuple[Path, int]]:
    """Encode the multi-day NetCDF and write one .bin per forecast day."""
    import numpy as np
    import xarray as xr

    ds = xr.open_dataset(nc_path)
    # siconc has no depth dimension — surface only by definition. Strip
    # if CMEMS happens to add a depth axis on some build of the dataset.
    if "depth" in ds.dims:
        ds = ds.isel(depth=0, drop=True)

    lat_res = abs(float(ds.latitude[1] - ds.latitude[0]))
    lon_res = abs(float(ds.longitude[1] - ds.longitude[0]))
    lat_block = max(1, int(round(SUBSAMPLE_DEG / lat_res)))
    lon_block = max(1, int(round(SUBSAMPLE_DEG / lon_res)))

    # siconc is NaN over land — perfect for deriving the land mask.
    land_native = ds["siconc"].isel(time=0).isnull().astype("float32")

    if lat_block > 1 or lon_block > 1:
        ds = ds.coarsen(latitude=lat_block, longitude=lon_block, boundary="trim").mean()
        land_frac = land_native.coarsen(latitude=lat_block, longitude=lon_block, boundary="trim").mean()
        land_da = (land_frac >= 0.5).astype("uint8")
    else:
        land_da = land_native.astype("uint8")

    # Frontend wants rows north→south, cols west→east (matches sister layers).
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
        # Open water = 0 (we replace NaN-over-ocean with 0 so the shader
        # discard threshold cleanly identifies "no ice"). NaN-over-land
        # stays in the land mask and gets discarded separately.
        siconc_raw = ds["siconc"].isel(time=i).fillna(0.0).astype(np.float32).values

        # siconc is dimensionless [0, 1] already — clip in case of tiny
        # numerical overshoots from the regridding step and pack directly.
        t_norm = np.clip(siconc_raw, 0.0, 1.0).astype(np.float32)

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

        ocean = siconc_raw[land_mask == 0]
        ice_pixels = int((ocean >= 0.15).sum()) if ocean.size else 0
        c_max = float(np.nanmax(ocean)) if ocean.size else float("nan")
        log.info(
            "Wrote %s (%dx%d, t=%s, T+%dh, ice cells=%d at >=15%%, max=%.2f, %d land / %d total)",
            bin_path.name, width, height, t, hour_offset,
            ice_pixels, c_max, land_count, width * height,
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
            dataset_key="seaice",
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
            dataset_key="seaice",
            source_paths=paths,
            offsets_hours=offsets,
            data_times=data_times,
            bundle_dir=BUNDLE_DIR,
            provenance=producer_provenance(),
            metadata={"attribution": "Copernicus Marine Service", "encoding": "sea-ice concentration [0,1] in u; v=0"},
        )
    except Exception:  # noqa: BLE001
        log.exception("Pipeline failed")
        return 1

    log.info("✓ Generated and validated %d immutable daily snapshots in %s", len(entries), BUNDLE_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
