from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

import numpy as np
import rasterio
from rasterio.transform import from_origin

from ml.src.input_contract import InputContractError, validate_sar_pair


def write_raster(path: Path, *, x_origin: float = 500000.0, values: np.ndarray | None = None) -> None:
    values = values if values is not None else np.ones((2, 8, 9), dtype=np.float32)
    profile = {
        "driver": "GTiff",
        "width": 9,
        "height": 8,
        "count": 2,
        "dtype": "float32",
        "crs": "EPSG:32645",
        "transform": from_origin(x_origin, 3100000, 10, 10),
        "nodata": np.nan,
    }
    with rasterio.open(path, "w", **profile) as raster:
        raster.write(values)
        raster.set_band_description(1, "Sigma0_VV")
        raster.set_band_description(2, "Sigma0_VH")
        raster.update_tags(BACKSCATTER_UNITS="linear_sigma0_power", ANALYSIS_READY="false")


class InputContractTests(unittest.TestCase):
    def test_valid_pair_is_reported_as_format_valid_but_not_inference_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pre = root / "pre.tif"
            post = root / "post.tif"
            write_raster(pre)
            write_raster(post)
            result = validate_sar_pair(pre, post)
            self.assertTrue(result["format_contract_valid"])
            self.assertFalse(result["inference_eligible"])
            self.assertEqual(result["valid_pixel_count"], 72)
            self.assertEqual(result["crs"], "EPSG:32645")
            self.assertEqual(result["published_training_clip_max_linear_sigma0"], 0.15)
            self.assertEqual(result["statistics"]["pre_vv"]["fraction_above_published_training_clip"], 1.0)
            self.assertTrue(any("Review the preprocessing/domain gap" in warning for warning in result["warnings"]))

    def test_rejects_grid_mismatch_and_nonpositive_common_pixels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pre = root / "pre.tif"
            post = root / "post.tif"
            write_raster(pre)
            write_raster(post, x_origin=500010)
            with self.assertRaisesRegex(InputContractError, "same dimensions and affine grid"):
                validate_sar_pair(pre, post)

            invalid = np.zeros((2, 8, 9), dtype=np.float32)
            write_raster(post, values=invalid)
            with self.assertRaisesRegex(InputContractError, "no common finite, positive pixels"):
                validate_sar_pair(pre, post)


if __name__ == "__main__":
    unittest.main()
