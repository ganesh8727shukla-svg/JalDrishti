"""A small U-Net for four-channel Sentinel-1 pre/post flood segmentation."""

from __future__ import annotations

import torch
from torch import nn
from torch.nn import functional as F


class DoubleConv(nn.Module):
    def __init__(self, in_channels: int, out_channels: int) -> None:
        super().__init__()
        self.layers = nn.Sequential(
            nn.Conv2d(in_channels, out_channels, kernel_size=3, padding=1, bias=False),
            nn.BatchNorm2d(out_channels),
            nn.ReLU(inplace=True),
            nn.Conv2d(out_channels, out_channels, kernel_size=3, padding=1, bias=False),
            nn.BatchNorm2d(out_channels),
            nn.ReLU(inplace=True),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.layers(x)


class UNet(nn.Module):
    """Four normalized linear-Sigma0 bands: pre-VV, pre-VH, post-VV, post-VH.

    The three logits correspond to no-water, permanent-water and flood. The
    softmax flood score is not calibrated confidence unless calibration is
    separately demonstrated on held-out validation data. Input normalization
    must match the documented Kuro Siwo per-polarization mean/std contract.
    """

    def __init__(self, in_channels: int = 4, num_classes: int = 3, base_channels: int = 16) -> None:
        super().__init__()
        if in_channels != 4:
            raise ValueError("The JalDrishti model contract requires exactly four input channels.")
        if num_classes != 3:
            raise ValueError("The Kuro Siwo label contract requires three output classes.")
        if base_channels < 4:
            raise ValueError("base_channels must be at least 4.")
        self.in_channels = in_channels
        self.num_classes = num_classes
        self.base_channels = base_channels
        widths = [base_channels, base_channels * 2, base_channels * 4, base_channels * 8]
        self.enc1 = DoubleConv(in_channels, widths[0])
        self.enc2 = DoubleConv(widths[0], widths[1])
        self.enc3 = DoubleConv(widths[1], widths[2])
        self.enc4 = DoubleConv(widths[2], widths[3])
        self.bottleneck = DoubleConv(widths[3], widths[3] * 2)
        self.pool = nn.MaxPool2d(2)
        self.dec4 = DoubleConv(widths[3] * 3, widths[3])
        # Each decoder stage concatenates the upsampled previous decoder output
        # with exactly one encoder skip connection.
        self.dec3 = DoubleConv(widths[3] + widths[2], widths[2])
        self.dec2 = DoubleConv(widths[2] + widths[1], widths[1])
        self.dec1 = DoubleConv(widths[1] + widths[0], widths[0])
        self.classifier = nn.Conv2d(widths[0], num_classes, kernel_size=1)

    @staticmethod
    def _up_to(x: torch.Tensor, reference: torch.Tensor) -> torch.Tensor:
        return F.interpolate(x, size=reference.shape[-2:], mode="bilinear", align_corners=False)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        if x.ndim != 4 or x.shape[1] != self.in_channels:
            raise ValueError("Input must have shape N×4×H×W.")
        if not torch.is_floating_point(x):
            raise ValueError("Model input must use a floating-point tensor.")
        a = self.enc1(x)
        b = self.enc2(self.pool(a))
        c = self.enc3(self.pool(b))
        d = self.enc4(self.pool(c))
        z = self.bottleneck(self.pool(d))
        x = self.dec4(torch.cat((self._up_to(z, d), d), dim=1))
        x = self.dec3(torch.cat((self._up_to(x, c), c), dim=1))
        x = self.dec2(torch.cat((self._up_to(x, b), b), dim=1))
        x = self.dec1(torch.cat((self._up_to(x, a), a), dim=1))
        return self.classifier(x)
