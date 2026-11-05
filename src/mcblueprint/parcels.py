"""道路に面した矩形帯を、地上・地下共通の区画へ分割する。"""

from mcblueprint.errors import BlueprintError


def split_frontage(west, north, east, south, widths, depths):
    """閉区間の矩形を北面間口で等分し、(西,北,東,南)の区画を返す。

    widths/depthsはlayoutの許容整数範囲。奥行きや最小間口が不足すれば
    BlueprintError。最大間口以下になる最小個数へ分割し、端数は先頭から配る。
    各区画の接道・水域・保護域・建物外形の検査は呼出し側が行う。
    """
    length, depth = east - west + 1, south - north + 1
    count = (length + widths[1] - 1) // widths[1]
    if count < 1 or length < count * widths[0] or not depths[0] <= depth <= depths[1]:
        raise BlueprintError("Site: 狭小区画またはlayoutの区画寸法範囲外です")
    size, extra = divmod(length, count)
    parcels = []
    for index in range(count):
        end = west + size + (index < extra) - 1
        parcels.append((west, north, end, south))
        west = end + 1
    return parcels
