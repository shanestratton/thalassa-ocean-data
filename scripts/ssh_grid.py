"""THSS1: one daily DUACS map, with an explicit unknown/ice mask.

Cell centres, S->N/W->E; 0.5 degree display grid. ADT/SLA are metres;
ugos/vgos are m/s of geostrophic current, never tidal or total current.
The separate codec deliberately cannot be read as an old THCU forecast.
"""
from __future__ import annotations

import math
import struct
import sys
from array import array
from pathlib import Path

HEADER = struct.Struct('<4sBBBBHHddddq12x')
WIDTH, HEIGHT = 720, 360
BOUNDS = {'west': -179.75, 'south': -89.75, 'east': 179.75, 'north': 89.75}
VARIABLES = ('adt', 'sla', 'ugos', 'vgos')
LIMITS = (5.0, 3.0, 5.0, 5.0)
CELLS = WIDTH * HEIGHT
FILE_BYTES = HEADER.size + CELLS * 17
DATASET_ID = 'cmems_obs-sl_glo_phy-ssh_nrt_allsat-l4-duacs-0.125deg_P1D'
DATASET_VERSION = '202506'
PRODUCT_ID = 'SEALEVEL_GLO_PHY_L4_NRT_008_046'
SSH_METADATA = {
    'product': PRODUCT_ID,
    'dataset_version': DATASET_VERSION,
    'variables': 'adt,sla,ugos,vgos',
    'units': 'm,m,m/s,m/s',
    'kind': 'mapped-altimetry-observation',
    'display_grid': '0.5 degree; 4x4 mean, every contributor finite and ice-free',
    'references': 'ADT above geoid; SLA above 1993-2012 mean; geostrophic currents',
    'credit': 'Generated using E.U. Copernicus Marine Service Information; DOI 10.48670/moi-00149; copyright Mercator Ocean',
}


def _require(ok: bool, message: str) -> None:
    if not ok:
        raise ValueError('SSH: ' + message)


def read_ssh_header(path: Path) -> dict:
    _require(path.is_file() and not path.is_symlink() and path.stat().st_size == FILE_BYTES,
             'exact regular global grid required')
    with path.open('rb') as source:
        raw = source.read(HEADER.size)
    magic, version, channels, flags, reserved, width, height, west, south, east, north, epoch = HEADER.unpack(raw)
    _require((magic, version, channels, flags, reserved, width, height) ==
             (b'THSS', 1, 4, 0, 0, WIDTH, HEIGHT), 'header identity/dimensions')
    _require(raw[-12:] == bytes(12), 'reserved header bytes')
    _require((west, south, east, north) == tuple(BOUNDS.values()), 'global display grid bounds')
    _require(1_577_836_800_000 <= epoch <= 4_102_444_800_000 and epoch % 86_400_000 == 0,
             'original daily UTC source time')
    return {'width': WIDTH, 'height': HEIGHT, 'bounds': dict(BOUNDS), 'source_time_ms': epoch}


def validate_ssh_payload(path: Path) -> dict:
    header = read_ssh_header(path)
    raw = path.read_bytes()
    mask = raw[HEADER.size + CELLS * 16:]
    _require(all(value in (0, 1) for value in mask), 'unknown mask value')
    wet = sum(mask)
    _require(CELLS // 10 <= wet <= CELLS * 9 // 10, 'global wet coverage outside bounds')
    for channel, limit in enumerate(LIMITS):
        values = array('f')
        start = HEADER.size + channel * CELLS * 4
        values.frombytes(raw[start:start + CELLS * 4])
        if sys.byteorder != 'little':
            values.byteswap()
        _require(all(math.isfinite(value) and abs(value) <= limit and (valid or value == 0)
                     for value, valid in zip(values, mask)), 'masked or nonphysical channel')
    return {**header, 'valid_cells': wet}


def write_ssh_grid(path: Path, fields, valid, source_time_ms: int) -> dict:
    """The caller has already decoded/scaled CF data. Never serialize NaNs."""
    import numpy as np
    _require(len(fields) == 4, 'four source channels required')
    mask = np.asarray(valid)
    _require(mask.shape == (HEIGHT, WIDTH) and mask.dtype == np.bool_, 'boolean valid-mask shape')
    encoded = []
    for field, limit in zip(fields, LIMITS):
        values = np.asarray(field)
        _require(values.shape == mask.shape, 'source channel shape')
        _require(bool(np.all(np.isfinite(values[mask]))) and bool(np.all(np.abs(values[mask]) <= limit)),
                 'nonphysical source values')
        encoded.append(np.where(mask, values, 0).astype('<f4').tobytes(order='C'))
    header = HEADER.pack(b'THSS', 1, 4, 0, 0, WIDTH, HEIGHT, *BOUNDS.values(), source_time_ms)
    with path.open('xb') as output:
        output.write(header)
        for field in encoded:
            output.write(field)
        output.write(mask.astype('u1').tobytes(order='C'))
    return validate_ssh_payload(path)


def coarsen_duacs(fields, ice):
    """Mean each 4x4 block only when EVERY contributor is finite and ice-free.

    This conservative mask keeps coast/ice gaps; it never fills/interpolates
    land or invents zero sea height/current. Source rows must already S->N.
    """
    import numpy as np
    _require(len(fields) == 4, 'four native fields required')
    planes = [np.asarray(field) for field in fields]
    _require(all(plane.shape == (1440, 2880) for plane in planes), 'exact 0.125 degree native dimensions')
    flag = np.asarray(ice)
    _require(flag.shape == planes[0].shape, 'native ice mask dimensions')
    valid = np.isfinite(flag) & (flag == 0)
    for plane, limit in zip(planes, LIMITS):
        valid &= np.isfinite(plane) & (np.abs(plane) <= limit)
    coarse_mask = valid.reshape(HEIGHT, 4, WIDTH, 4).all(axis=(1, 3))
    coarse = [np.where(coarse_mask, np.where(valid, plane, 0).reshape(HEIGHT, 4, WIDTH, 4).mean(axis=(1, 3)), 0)
              for plane in planes]
    return coarse, coarse_mask
