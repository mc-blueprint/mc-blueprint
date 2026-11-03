"""blockdata の面色と線形 sRGB / OKLab の変換。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from mcblueprint.blockdata import BlockData, BlockInfo


def oklab_to_linear(lab: Iterable[float]) -> tuple[float, float, float]:
    """OKLab を線形 sRGB に変換する。"""
    lightness, a, b = lab
    long = (lightness + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m = (lightness - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s = (lightness - 0.0894841775 * a - 1.2914855480 * b) ** 3
    return (
        4.0767416621 * long - 3.3077115913 * m + 0.2309699292 * s,
        -1.2684380046 * long + 2.6097574011 * m - 0.3413193965 * s,
        -0.0041960863 * long - 0.7034186147 * m + 1.7076147010 * s,
    )


def linear_to_oklab(rgb: Iterable[float]) -> tuple[float, float, float]:
    """線形 sRGB を OKLab に変換する。"""
    r, g, b = rgb
    values = (
        0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b,
        0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b,
        0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b,
    )
    long, m, s = (abs(v) ** (1 / 3) * (-1 if v < 0 else 1) for v in values)
    return (
        0.2104542553 * long + 0.7936177850 * m - 0.0040720468 * s,
        1.9779984951 * long - 2.4285922050 * m + 0.4505937099 * s,
        0.0259040371 * long + 0.7827717662 * m - 0.8086757660 * s,
    )


def face_linear(
    info: BlockInfo, face: dict[str, Any], data: BlockData, biome: str = "plains",
    quantile: int = 2,
) -> tuple[float, ...]:
    """指定分位点の素材色を線形RGBで着色する。混在層は面積比がないため等分する。"""
    def color(key: str) -> tuple[float, float, float]:
        return oklab_to_linear(face[key][axis][quantile] for axis in ("L", "a", "b"))

    if info.tint == "none" or "tintedOklab" not in face:
        return color("oklab")
    tint = data.tints[info.tint][info.id if info.tint == "fixed" else biome]
    result = tuple(x * y for x, y in zip(color("tintedOklab"), oklab_to_linear(tint), strict=True))
    if "untintedOklab" in face:
        result = tuple((x + y) / 2 for x, y in zip(result, color("untintedOklab"), strict=True))
    return result
