"""Validation for the JalDrishti two-date Sentinel-1 model input contract.

This module checks raster compatibility and reads metadata/statistics only. It
does not certify scientific analysis readiness or run model inference.
"""

from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from typing import Any

import numpy as np
import rasterio

from ml.src.dataset import LINEAR_SIGMA0_CLIP_MAX


EXPECTED_BANDS = ("Sigma0_VV", "Sigma0_VH")
EXPECTED_UNITS_TAG = "linear_sigma0_power"


class InputContractError(ValueError):
    """Raised when a pre/post raster pair violates the declared model contract."""


def validate_sar_pair(pre_path: str | Path, post_path: str | Path) -> dict[str, Any]:
    """Check bands, units, grid, masks, and finite positive linear backscatter."""
    pre_path = Path(pre_path)
    post_path = Path(post_path)
    if not pre_path.is_file() or not post_path.is_file():
        raise InputContractError("Both pre-event and post-event raster files must exist.")

    try:
        with ExitStack() as stack:
            pre = stack.enter_context(rasterio.open(pre_path))
            post = stack.enter_context(rasterio.open(post_path))
            return _validate_open_pair(pre, post)
    except rasterio.errors.RasterioError as exc:
        raise InputContractError("Could not open or read one of the Sentinel-1 rasters.") from exc


def _validate_open_pair(pre: rasterio.io.DatasetReader, post: rasterio.io.DatasetReader) -> dict[str, Any]:
    if pre.count != 2 or post.count != 2:
        raise InputContractError("Each date must contain exactly VV and VH bands.")
    if tuple(pre.descriptions) != EXPECTED_BANDS or tuple(post.descriptions) != EXPECTED_BANDS:
        raise InputContractError("Band descriptions must be ordered Sigma0_VV, Sigma0_VH.")
    if pre.tags().get("BACKSCATTER_UNITS") != EXPECTED_UNITS_TAG or post.tags().get("BACKSCATTER_UNITS") != EXPECTED_UNITS_TAG:
        raise InputContractError("Both inputs must declare linear Sigma0 power units.")
    if pre.crs is None or post.crs is None or pre.crs != post.crs:
        raise InputContractError("Pre/post rasters must have the same defined CRS.")
    if pre.width != post.width or pre.height != post.height or pre.transform != post.transform:
        raise InputContractError("Pre/post rasters must use the same dimensions and affine grid.")

    pixel_size_x = abs(float(pre.transform.a))
    pixel_size_y = abs(float(pre.transform.e))
    if pixel_size_x <= 0 or pixel_size_y <= 0:
        raise InputContractError("Raster pixel spacing must be positive and non-zero.")

    stats_count = np.zeros(4, dtype=np.int64)
    stats_min = np.full(4, np.inf, dtype=np.float64)
    stats_max = np.full(4, -np.inf, dtype=np.float64)
    stats_sum = np.zeros(4, dtype=np.float64)
    above_clip_count = np.zeros(4, dtype=np.int64)
    valid_pixel_count = 0
    total_pixel_count = pre.width * pre.height

    for _, window in pre.block_windows(1):
        pre_data = pre.read((1, 2), window=window, out_dtype="float32")
        post_data = post.read((1, 2), window=window, out_dtype="float32")
        masks = np.concatenate((pre.read_masks((1, 2), window=window), post.read_masks((1, 2), window=window)))
        all_values = np.concatenate((pre_data, post_data))
        valid = np.all(masks > 0, axis=0) & np.all(np.isfinite(all_values), axis=0) & np.all(all_values > 0, axis=0)
        valid_count = int(np.count_nonzero(valid))
        valid_pixel_count += valid_count
        for offset, data in ((0, pre_data), (2, post_data)):
            for band_index in range(2):
                slot = offset + band_index
                values = data[band_index][valid]
                if values.size:
                    stats_count[slot] += values.size
                    stats_min[slot] = min(stats_min[slot], float(values.min()))
                    stats_max[slot] = max(stats_max[slot], float(values.max()))
                    stats_sum[slot] += float(values.sum(dtype=np.float64))
                    above_clip_count[slot] += int(np.count_nonzero(values > LINEAR_SIGMA0_CLIP_MAX))

    if valid_pixel_count == 0:
        raise InputContractError("The pair has no common finite, positive pixels.")

    labels = ("pre_vv", "pre_vh", "post_vv", "post_vh")
    statistics = {
        name: {
            "valid_count": int(stats_count[index]),
            "minimum": float(stats_min[index]),
            "maximum": float(stats_max[index]),
            "mean": float(stats_sum[index] / stats_count[index]),
            "fraction_above_published_training_clip": float(above_clip_count[index] / stats_count[index]),
        }
        for index, name in enumerate(labels)
    }
    valid_fraction = valid_pixel_count / total_pixel_count
    warnings = [
        "This validates raster contract and common valid pixels only; it does not independently verify radiometric accuracy.",
        "The source manifest marks these processed rasters analysis_ready=false; no model-approved input pair is present.",
    ]
    clip_exceedances = {
        name: statistics[name]["fraction_above_published_training_clip"]
        for name in labels
        if statistics[name]["fraction_above_published_training_clip"] > 0.1
    }
    if clip_exceedances:
        warnings.append(
            "More than 10% of common valid pixels in these channels exceed the Kuro Siwo training clip ceiling "
            f"({LINEAR_SIGMA0_CLIP_MAX} linear Sigma0): "
            + ", ".join(f"{name}={fraction:.1%}" for name, fraction in clip_exceedances.items())
            + ". Review the preprocessing/domain gap before inference."
        )
    return {
        "raster_pair_contract": "jaldrishti_s1_two_date_v1",
        "format_contract_valid": True,
        "inference_eligible": False,
        "analysis_ready": False,
        "crs": pre.crs.to_string(),
        "width": pre.width,
        "height": pre.height,
        "transform": list(pre.transform)[:6],
        "bounds": [pre.bounds.left, pre.bounds.bottom, pre.bounds.right, pre.bounds.top],
        "pixel_spacing": [pixel_size_x, pixel_size_y],
        "band_order": list(EXPECTED_BANDS),
        "backscatter_units": EXPECTED_UNITS_TAG,
        "published_training_clip_max_linear_sigma0": LINEAR_SIGMA0_CLIP_MAX,
        "valid_pixel_count": valid_pixel_count,
        "valid_pixel_fraction": valid_fraction,
        "statistics": statistics,
        "warnings": warnings,
    }
