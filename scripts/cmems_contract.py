#!/usr/bin/env python3
"""Validation gates shared by all CMEMS producers."""
from __future__ import annotations

from pathlib import Path
import hashlib
from typing import Any, Collection, Mapping, Sequence

from publisher_contract import ContractError, read_thcu_header


PLAUSIBLE_RANGES: dict[str, tuple[float, float]] = {
    "uo": (-8.0, 8.0),
    "vo": (-8.0, 8.0),
    "VHM0": (0.0, 40.0),
    "VMDR": (0.0, 360.0),
    "thetao": (-3.0, 45.0),
    "chl": (0.0, 1_000.0),
    "siconc": (0.0, 1.01),
    "mlotst": (0.0, 6_000.0),
}

# Exact scientific meanings and scale-preserving unit spellings documented by
# the Copernicus Marine product manuals for the three source products.  These
# aliases differ only in notation; units that require a numeric conversion
# (knots, kelvin, percent, kilometres, kg m-3, radians, ...) are deliberately
# absent because the producers do not perform those conversions.
CF_METADATA_CONTRACT: dict[str, dict[str, Any]] = {
    "longitude": {
        "standard_name": "longitude",
        "axis": "X",
        "units": frozenset({"degree_east", "degrees_east"}),
    },
    "latitude": {
        "standard_name": "latitude",
        "axis": "Y",
        "units": frozenset({"degree_north", "degrees_north"}),
    },
    "depth": {
        "standard_name": "depth",
        "axis": "Z",
        "positive": "down",
        "units": frozenset({"m"}),
    },
    "time": {
        "standard_name": "time",
        "axis": "T",
        "units": frozenset(
            {
                "hours since 1950-01-01",
                "hours since 1950-01-01 0:0:0",
                "hours since 1950-01-01 00:00:00",
            }
        ),
    },
    "uo": {
        "standard_name": "eastward_sea_water_velocity",
        "units": frozenset({"m s-1", "m s^-1", "m/s"}),
    },
    "vo": {
        "standard_name": "northward_sea_water_velocity",
        "units": frozenset({"m s-1", "m s^-1", "m/s"}),
    },
    "VHM0": {
        "standard_name": "sea_surface_wave_significant_height",
        "units": frozenset({"m"}),
    },
    "VMDR": {
        "standard_name": "sea_surface_wave_from_direction",
        "units": frozenset({"degree", "degrees"}),
    },
    "thetao": {
        "standard_name": "sea_water_potential_temperature",
        "units": frozenset({"degree_c", "degrees_c", "degree_celsius", "degrees_celsius", "degc"}),
    },
    "chl": {
        "standard_name": "mass_concentration_of_chlorophyll_a_in_sea_water",
        "units": frozenset({"mg m-3", "mg m^-3", "mg/m3", "mg.m-3"}),
    },
    "siconc": {
        "standard_name": "sea_ice_area_fraction",
        "units": frozenset({"1"}),
    },
    "mlotst": {
        "standard_name": "ocean_mixed_layer_thickness_defined_by_sigma_theta",
        "units": frozenset({"m"}),
    },
}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def _strictly_monotonic(values: Any) -> int:
    import numpy as np

    delta = np.diff(values.astype("float64"))
    if np.all(delta > 0):
        return 1
    if np.all(delta < 0):
        return -1
    return 0


def _normalise_unit(value: Any) -> str:
    _require(isinstance(value, str) and value.strip() != "", "CF units attribute is missing")
    # Preserve dimensional syntax while normalising harmless typography and
    # whitespace.  No dimensional conversion happens here.
    return " ".join(value.strip().casefold().replace("−", "-").split())


def validate_cf_metadata(
    name: str,
    attrs: Mapping[str, Any],
    encoding: Mapping[str, Any] | None = None,
) -> None:
    """Fail closed unless a source field has the exact expected CF meaning."""
    _require(name in CF_METADATA_CONTRACT, f"{name}: no trusted CF metadata contract")
    spec = CF_METADATA_CONTRACT[name]
    _require(attrs.get("standard_name") == spec["standard_name"], f"{name}: unexpected standard_name {attrs.get('standard_name')!r}")
    for attribute in ("axis", "positive"):
        if attribute in spec:
            _require(attrs.get(attribute) == spec[attribute], f"{name}: unexpected {attribute} {attrs.get(attribute)!r}")
    # xarray moves the encoded time units from attrs to encoding when it
    # decodes the coordinate to datetime64.  All other fields normally retain
    # units in attrs, but the same lookup order is safe for either form.
    units = attrs.get("units")
    if units is None and encoding is not None:
        units = encoding.get("units")
    normalised = _normalise_unit(units)
    _require(normalised in spec["units"], f"{name}: unexpected units {units!r}")


def require_invariant_finite_mask(reference: Any, candidate: Any, label: str) -> None:
    """Require every variable/time step to share one exact valid-data mask."""
    import numpy as np

    reference_array = np.asarray(reference, dtype=bool)
    candidate_array = np.asarray(candidate, dtype=bool)
    _require(reference_array.shape == candidate_array.shape, f"{label}: finite-mask shape changed")
    drift = int(np.count_nonzero(reference_array != candidate_array))
    _require(drift == 0, f"{label}: finite-mask drifted in {drift} cells; refusing to fabricate ocean values")


# The wave product defines VMDR on a one-cell fringe outside VHM0's wet mask
# (coastlines plus the seasonal sea-ice edge). Measured against the live
# product across 2026-06-01..2026-08-26: 53,093-58,099 extra cells
# (0.60-0.66% of the grid, mid-latitude counts identical across months, polar
# counts moving with the ice edge), and zero cells in the opposite direction
# on every date. The bound is ~3x the observed seasonal maximum so a regrid or
# genuine mask change upstream still fails closed.
FINITE_SUPERSET_MAX_FRACTION = 0.02


def require_finite_superset_of_reference(
    reference: Any, candidate: Any, label: str, max_extra_fraction: float
) -> None:
    """Forbid missing ocean values; tolerate a bounded finite fringe outside the mask.

    The reference variable's mask is the published ocean/land truth. Extra
    finite candidate cells outside it are never encoded (the producers weight
    by the reference field, whose NaN annihilates them, and the land mask
    zeroes those cells), so they cannot fabricate ocean values — but their
    count is still bounded so an upstream grid or mask change fails closed.
    """
    import numpy as np

    reference_array = np.asarray(reference, dtype=bool)
    candidate_array = np.asarray(candidate, dtype=bool)
    _require(reference_array.shape == candidate_array.shape, f"{label}: finite-mask shape changed")
    missing = int(np.count_nonzero(reference_array & ~candidate_array))
    _require(missing == 0, f"{label}: value missing in {missing} ocean cells; refusing to fabricate ocean values")
    extra = int(np.count_nonzero(candidate_array & ~reference_array))
    limit = int(reference_array.size * max_extra_fraction)
    _require(
        extra <= limit,
        f"{label}: finite mask exceeds the reference ocean by {extra} cells (limit {limit}); upstream mask changed",
    )


def validate_cmems_source(
    nc_path: Path,
    *,
    dataset_key: str,
    variables: Sequence[str],
    expected_steps: int,
    cadence_hours: int,
    native_resolution: float,
    finite_superset_variables: Collection[str] = frozenset(),
) -> list[str]:
    """Validate downloaded source before any NaNs are filled or values clipped."""
    import numpy as np
    import xarray as xr

    _require(
        variables[0] not in finite_superset_variables,
        f"{dataset_key}: the reference variable {variables[0]} defines the ocean mask and cannot be a superset variable",
    )
    _require(
        set(finite_superset_variables) <= set(variables),
        f"{dataset_key}: finite_superset_variables must name declared variables",
    )
    _require(nc_path.is_file() and nc_path.stat().st_size > 0, f"{dataset_key}: source NetCDF is missing")
    with xr.open_dataset(nc_path) as ds:
        for coordinate in ("time", "latitude", "longitude"):
            _require(coordinate in ds.coords, f"{dataset_key}: missing {coordinate} coordinate")
            _require(ds[coordinate].ndim == 1, f"{dataset_key}: {coordinate} must be one-dimensional")
            validate_cf_metadata(coordinate, ds[coordinate].attrs, ds[coordinate].encoding)
        if "depth" in ds.coords:
            _require(ds["depth"].ndim == 1, f"{dataset_key}: depth must be one-dimensional")
            validate_cf_metadata("depth", ds["depth"].attrs, ds["depth"].encoding)
        _require(ds.sizes["time"] == expected_steps, f"{dataset_key}: expected exactly {expected_steps} source steps")
        _require(ds.sizes["latitude"] >= 600 and ds.sizes["longitude"] >= 1300, f"{dataset_key}: source grid is too small")

        lat = np.asarray(ds.latitude.values)
        lon = np.asarray(ds.longitude.values)
        _require(np.issubdtype(lat.dtype, np.number) and np.isfinite(lat).all(), f"{dataset_key}: invalid latitude values")
        _require(np.issubdtype(lon.dtype, np.number) and np.isfinite(lon).all(), f"{dataset_key}: invalid longitude values")
        _require(_strictly_monotonic(lat) != 0, f"{dataset_key}: latitude is not strictly monotonic")
        _require(_strictly_monotonic(lon) == 1, f"{dataset_key}: longitude must increase west-to-east")
        _require(float(lat.min()) <= -79.0 and float(lat.max()) >= 89.0, f"{dataset_key}: latitude coverage is not global")
        _require(float(lon.min()) <= -179.0 and float(lon.max()) >= 179.0, f"{dataset_key}: longitude coverage is not global")
        lat_step = float(np.median(np.abs(np.diff(lat.astype("float64")))))
        lon_step = float(np.median(np.abs(np.diff(lon.astype("float64")))))
        tolerance = max(0.002, native_resolution * 0.03)
        _require(abs(lat_step - native_resolution) <= tolerance, f"{dataset_key}: unexpected native latitude resolution {lat_step}")
        _require(abs(lon_step - native_resolution) <= tolerance, f"{dataset_key}: unexpected native longitude resolution {lon_step}")

        times = np.asarray(ds.time.values)
        _require(np.issubdtype(times.dtype, np.datetime64) and not np.isnat(times).any(), f"{dataset_key}: invalid time coordinate")
        nanos = times.astype("datetime64[ns]").astype("int64")
        _require(np.all(np.diff(nanos) > 0), f"{dataset_key}: time is not strictly increasing")
        expected_ns = cadence_hours * 3_600 * 1_000_000_000
        _require(np.all(np.diff(nanos) == expected_ns), f"{dataset_key}: source cadence is not exactly {cadence_hours}h")
        data_times = [np.datetime_as_string(value.astype("datetime64[s]"), unit="s", timezone="UTC") for value in times]

        unexpected = {
            name
            for name, value in ds.data_vars.items()
            if name not in variables and any(dim in value.dims for dim in ("time", "latitude", "longitude"))
        }
        _require(not unexpected, f"{dataset_key}: unexpected gridded variables: {sorted(unexpected)}")
        for variable in variables:
            _require(variable in ds.data_vars, f"{dataset_key}: missing variable {variable}")
            validate_cf_metadata(variable, ds[variable].attrs, ds[variable].encoding)
            dims = set(ds[variable].dims)
            _require({"time", "latitude", "longitude"}.issubset(dims), f"{dataset_key}: {variable} has wrong dimensions")
            _require(dims <= {"time", "latitude", "longitude", "depth"}, f"{dataset_key}: {variable} has unsupported dimensions")
            if "depth" in dims:
                _require("depth" in ds.coords, f"{dataset_key}: {variable} depth dimension has no coordinate")
                _require(ds.sizes["depth"] == 1, f"{dataset_key}: {variable} must contain exactly one surface depth")

        reference = ds[variables[0]]
        if "depth" in reference.dims:
            reference = reference.isel(depth=0, drop=True)
        reference_values = np.asarray(reference.isel(time=0).transpose("latitude", "longitude").values)
        ocean_mask = np.isfinite(reference_values)
        ocean_fraction = float(ocean_mask.mean())
        _require(0.40 <= ocean_fraction <= 0.98, f"{dataset_key}: implausible finite-ocean/land-mask fraction {ocean_fraction:.3f}")

        for variable in variables:
            field = ds[variable]
            if "depth" in field.dims:
                field = field.isel(depth=0, drop=True)
            low, high = PLAUSIBLE_RANGES[variable]
            observed_low = float("inf")
            observed_high = float("-inf")
            for step in range(expected_steps):
                values = np.asarray(field.isel(time=step).transpose("latitude", "longitude").values)
                _require(values.shape == ocean_mask.shape, f"{dataset_key}: {variable} grid shape changed")
                _require(not np.isinf(values).any(), f"{dataset_key}: {variable} contains infinity")
                finite = np.isfinite(values)
                if variable in finite_superset_variables:
                    require_finite_superset_of_reference(
                        ocean_mask, finite, f"{dataset_key}:{variable}:step{step}", FINITE_SUPERSET_MAX_FRACTION
                    )
                else:
                    require_invariant_finite_mask(ocean_mask, finite, f"{dataset_key}:{variable}:step{step}")
                ocean_values = values[finite & ocean_mask]
                _require(ocean_values.size > 0, f"{dataset_key}: {variable} has no finite ocean values")
                observed_low = min(observed_low, float(ocean_values.min()))
                observed_high = max(observed_high, float(ocean_values.max()))
            epsilon = max(1e-6, abs(high - low) * 1e-7)
            _require(observed_low >= low - epsilon and observed_high <= high + epsilon, f"{dataset_key}: {variable} range [{observed_low}, {observed_high}] is implausible")
            _require(observed_high - observed_low > epsilon, f"{dataset_key}: {variable} is unexpectedly constant")
    return data_times


def validate_thcu_payloads(paths: Sequence[Path]) -> None:
    """Validate encoded header, exact size, mask domain and mask plausibility."""
    first_header: dict[str, Any] | None = None
    first_mask_sha256: str | None = None
    for path in paths:
        header = read_thcu_header(path)
        if first_header is None:
            first_header = header
        _require(header == first_header, "encoded grid changed between layers")
        cell_count = header["width"] * header["height"]
        with path.open("rb") as source:
            source.seek(30 + cell_count * 8)
            mask = source.read(cell_count)
        _require(len(mask) == cell_count, f"{path.name}: truncated land mask")
        unique = set(mask)
        _require(unique <= {0, 1}, f"{path.name}: land mask contains values other than 0/1")
        land_fraction = mask.count(1) / cell_count
        _require(0.02 <= land_fraction <= 0.60, f"{path.name}: implausible land-mask fraction {land_fraction:.3f}")
        mask_sha256 = hashlib.sha256(mask).hexdigest()
        if first_mask_sha256 is None:
            first_mask_sha256 = mask_sha256
        _require(mask_sha256 == first_mask_sha256, f"{path.name}: encoded land mask changed between forecast steps")
