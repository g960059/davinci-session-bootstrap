"""``.cube`` 3D LUT generation for the Milestone 5 escape hatch.

A ``.cube`` file (Adobe / Resolve format) is a plain-text grid of RGB
output values sampled at a regular input grid. For our richer-transform
pipeline it lets Resolve apply exposure + offset (and later matrix + 1D
shaper) as a single node LUT, decoupling us from CDL's slope clipping.

File format (simplified):

    TITLE "my-lut"
    LUT_3D_SIZE 33
    DOMAIN_MIN 0.0 0.0 0.0
    DOMAIN_MAX 1.0 1.0 1.0
    r0 g0 b0     # sample 0: (0, 0, 0) grid point
    r1 g1 b1     # sample 1: (0, 0, step) grid point
    ...
    (SIZE**3 lines total, iterating B fastest, then G, then R)

The B-fastest iteration order is what Resolve expects (the Adobe spec).
We write ``LUT_3D_SIZE=33`` which gives 35,937 samples — plenty of
resolution for a linear exposure+offset transform without bloating the
file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np


DEFAULT_LUT_SIZE = 33
"""Grid size for the .cube LUT. 33^3 = 35,937 samples, standard for
Resolve/ACES LUTs. Larger (65) is excessive for a linear transform;
smaller (17) quantizes visible banding in shadows."""


def build_richer_transform_lut(
    gain_rgb: tuple[float, float, float],
    offset_rgb: tuple[float, float, float],
    *,
    matrix_3x3: np.ndarray | None = None,
    size: int = DEFAULT_LUT_SIZE,
) -> np.ndarray:
    """Build a 3D LUT grid for the richer transform.

    Returns a ``(size, size, size, 3)`` float32 array where
    ``grid[b, g, r, :]`` is the RGB output for the input RGB
    ``(r / (size-1), g / (size-1), b / (size-1))``.

    The axis order matches ``.cube`` file iteration (B-fastest, R-slowest),
    so when we serialize we can flatten in this natural order.

    In v1 the transform is ``out = clip(gain*in + offset, 0, 1)``. If
    ``matrix_3x3`` is non-identity it is applied after the gain/offset:
    ``out = clip(matrix @ (gain*in + offset), 0, 1)``.
    """
    if size < 2:
        raise ValueError(f"LUT size must be ≥ 2, got {size}")

    gain = np.asarray(gain_rgb, dtype=np.float32)
    offset = np.asarray(offset_rgb, dtype=np.float32)
    if gain.shape != (3,) or offset.shape != (3,):
        raise ValueError(
            f"gain_rgb and offset_rgb must be length-3 tuples, "
            f"got gain={gain.shape}, offset={offset.shape}"
        )

    axis = np.linspace(0.0, 1.0, size, dtype=np.float32)
    # Build the (size, size, size, 3) input grid with B varying fastest,
    # G next, R slowest — matches the .cube file layout.
    r_grid, g_grid, b_grid = np.meshgrid(axis, axis, axis, indexing="ij")
    inp = np.stack([r_grid, g_grid, b_grid], axis=-1)  # (size, size, size, 3)

    out = inp * gain + offset

    if matrix_3x3 is not None:
        m = np.asarray(matrix_3x3, dtype=np.float32)
        if m.shape != (3, 3):
            raise ValueError(f"matrix_3x3 must be 3x3, got {m.shape}")
        # Apply: out[..., :] = m @ out[..., :]  (per-pixel 3-vec multiply)
        out = out @ m.T

    return np.clip(out, 0.0, 1.0)


def write_cube_file(
    path: Path,
    lut_grid: np.ndarray,
    *,
    title: str = "piano-guard-richer-transform",
    domain_min: tuple[float, float, float] = (0.0, 0.0, 0.0),
    domain_max: tuple[float, float, float] = (1.0, 1.0, 1.0),
) -> Path:
    """Serialize a 3D LUT grid to a ``.cube`` file.

    The grid is expected in ``(size, size, size, 3)`` shape with axes
    indexed as ``[r, g, b, :]``. The file written iterates B fastest,
    then G, then R, matching Resolve's expectation.
    """
    if lut_grid.ndim != 4 or lut_grid.shape[-1] != 3:
        raise ValueError(
            f"lut_grid must be (size, size, size, 3); got {lut_grid.shape}"
        )
    size = lut_grid.shape[0]
    if lut_grid.shape[1] != size or lut_grid.shape[2] != size:
        raise ValueError(
            f"lut_grid must be a cube; got {lut_grid.shape}"
        )

    path.parent.mkdir(parents=True, exist_ok=True)

    # Build file body. Resolve's .cube parser accepts any reasonable
    # float precision; 6 decimals matches our other CDL writers.
    lines: list[str] = [
        f'TITLE "{title}"',
        f"LUT_3D_SIZE {size}",
        f"DOMAIN_MIN {domain_min[0]:.6f} {domain_min[1]:.6f} {domain_min[2]:.6f}",
        f"DOMAIN_MAX {domain_max[0]:.6f} {domain_max[1]:.6f} {domain_max[2]:.6f}",
    ]
    # Iterate B fastest, G middle, R slowest.
    for r in range(size):
        for g in range(size):
            for b in range(size):
                triple = lut_grid[r, g, b]
                lines.append(
                    f"{float(triple[0]):.6f} "
                    f"{float(triple[1]):.6f} "
                    f"{float(triple[2]):.6f}"
                )
    # Atomic-ish write via tmp + rename
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
        handle.write("\n")
    tmp.replace(path)
    return path


def read_cube_file(path: Path) -> tuple[np.ndarray, dict[str, Any]]:
    """Read a ``.cube`` 3D LUT file back into a numpy grid.

    Returns ``(grid, header)`` where ``grid`` is
    ``(size, size, size, 3)`` float32 and ``header`` is a dict of
    metadata (``title``, ``size``, ``domain_min``, ``domain_max``).

    Used for round-trip testing. Tolerant to extra whitespace / comments
    starting with ``#``.
    """
    title = ""
    size = 0
    domain_min = (0.0, 0.0, 0.0)
    domain_max = (1.0, 1.0, 1.0)
    samples: list[list[float]] = []

    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("TITLE"):
                # TITLE "something" — parse the quoted string
                start = stripped.find('"')
                end = stripped.rfind('"')
                title = stripped[start + 1 : end] if 0 <= start < end else ""
            elif stripped.startswith("LUT_3D_SIZE"):
                size = int(stripped.split()[1])
            elif stripped.startswith("DOMAIN_MIN"):
                parts = stripped.split()
                domain_min = (float(parts[1]), float(parts[2]), float(parts[3]))
            elif stripped.startswith("DOMAIN_MAX"):
                parts = stripped.split()
                domain_max = (float(parts[1]), float(parts[2]), float(parts[3]))
            else:
                parts = stripped.split()
                if len(parts) == 3:
                    samples.append([float(p) for p in parts])

    if size == 0 or len(samples) != size**3:
        raise ValueError(
            f"malformed .cube file {path}: expected {size**3 if size else '?'} "
            f"samples, got {len(samples)}"
        )

    # Reshape with B-fastest, G-middle, R-slowest back into (r, g, b, 3)
    flat = np.asarray(samples, dtype=np.float32)
    grid = flat.reshape(size, size, size, 3)
    return grid, {
        "title": title,
        "size": size,
        "domain_min": domain_min,
        "domain_max": domain_max,
    }


def write_richer_transform_cube(
    path: Path,
    gain_rgb: tuple[float, float, float],
    offset_rgb: tuple[float, float, float],
    *,
    matrix_3x3: np.ndarray | None = None,
    size: int = DEFAULT_LUT_SIZE,
    title: str | None = None,
) -> Path:
    """Convenience: build the grid and write it in one call.

    ``title`` defaults to the filename stem so the LUT is self-identifying
    when the operator opens it in a .cube viewer.
    """
    grid = build_richer_transform_lut(
        gain_rgb, offset_rgb, matrix_3x3=matrix_3x3, size=size
    )
    return write_cube_file(path, grid, title=title or path.stem)


__all__ = [
    "DEFAULT_LUT_SIZE",
    "build_richer_transform_lut",
    "write_cube_file",
    "read_cube_file",
    "write_richer_transform_cube",
]
