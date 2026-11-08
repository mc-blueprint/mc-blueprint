"""整数seedのRandom.random()初回値を、上限付きの配列でまとめて求める。"""

import json
import random
from collections import OrderedDict
from collections.abc import Iterator
from itertools import islice

import numpy as np

from mcblueprint.model.palette import Palette
from mcblueprint.model.vec import AABB, Vec3

# MT19937の整数seed初期化・53bit出力に合わせる。旧経路は標準Randomを使う。
# https://github.com/python/cpython/blob/3.12/Modules/_randommodule.c
_initial = [19650218]
for _i in range(1, 624):
    _previous = _initial[-1]
    _initial.append((1812433253 * (_previous ^ (_previous >> 30)) + _i) & 0xffffffff)
_INITIAL = tuple(_initial)
del _initial, _i, _previous


def first_draws(digests: np.ndarray) -> np.ndarray:
    """SHA-256の各32バイトから、Random(int.from_bytes(..., 'big')).random()を返す。

    digestsは(N, 8)のbig-endian uint32。呼出し側でバッチを制限する。
    先頭0ワードも標準Randomと同じseed長として扱う。
    """
    # 小さいタイルでは配列演算の固定費より標準実装の初期化が安い。
    if len(digests) <= 2048:
        return np.fromiter((random.Random(int.from_bytes(row.tobytes(), 'big')).random()
                            for row in digests), dtype=np.float64, count=len(digests))
    keys = digests.astype(np.uint32).T[::-1]
    lengths = np.maximum(1, 8 - np.argmax(digests != 0, axis=1))
    lengths[~np.any(digests, axis=1)] = 1
    result = np.empty(len(digests), dtype=np.float64)
    for width in np.unique(lengths):
        selected = lengths == width
        result[selected] = _first_draws(keys[:width, selected])
    return result


def _first_draws(keys: np.ndarray) -> np.ndarray:
    width, count = keys.shape
    offsets = keys + np.arange(width, dtype=np.uint32)[:, None]
    state = np.empty((624, count), dtype=np.uint32)
    previous = np.full(count, _INITIAL[0], dtype=np.uint32)
    for i in range(1, 624):
        j = (i - 1) % width
        current = state[i]
        np.right_shift(previous, 30, out=current)
        np.bitwise_xor(previous, current, out=current)
        np.multiply(current, np.uint32(1664525), out=current)
        np.bitwise_xor(current, np.uint32(_INITIAL[i]), out=current)
        np.add(current, offsets[j], out=current)
        previous = state[i]
    j = 623 % width
    previous = (state[1] ^ ((previous ^ (previous >> 30)) * np.uint32(1664525)))
    state[1] = previous + offsets[j]
    previous = state[1].copy()
    scratch = np.empty_like(previous)
    needed = {}
    for i in range(2, 624):
        np.right_shift(previous, 30, out=scratch)
        np.bitwise_xor(previous, scratch, out=previous)
        np.multiply(previous, np.uint32(1566083941), out=previous)
        np.bitwise_xor(previous, state[i], out=previous)
        np.subtract(previous, np.uint32(i), out=previous)
        if i in (2, 397, 398):
            needed[i] = previous.copy()
    first = (state[1] ^ (
        (previous ^ (previous >> 30)) * np.uint32(1566083941)
    )) - np.uint32(1)
    joined = np.uint32(0x80000000) | (first & np.uint32(0x7fffffff))
    a = needed[397] ^ (joined >> 1) ^ ((first & 1) * np.uint32(0x9908b0df))
    joined = (first & np.uint32(0x80000000)) | (needed[2] & np.uint32(0x7fffffff))
    b = needed[398] ^ (joined >> 1) ^ ((needed[2] & 1) * np.uint32(0x9908b0df))
    for value in (a, b):
        value ^= value >> 11
        value ^= (value << 7) & np.uint32(0x9d2c5680)
        value ^= (value << 15) & np.uint32(0xefc60000)
        value ^= value >> 18
    return ((a >> 5).astype(np.float64) * 67108864 + (b >> 6)) / 9007199254740992


class PaletteSampler:
    """32立方の抽選結果だけをLRUに保持し、haloの重複抽選を避ける。

    BlockVolumeや属性は保存しない。作業配列とキャッシュは指定された予算内に制限する。
    """

    def __init__(self, bounds: AABB, memory_bytes: int) -> None:
        self.bounds = bounds
        self.batch_size = max(1, min(32768, memory_bytes // (4 * 2600)))
        self.cache_limit = min(24 * 1024 * 1024, memory_bytes // 8)
        self.tile_size = 32
        while self.tile_size > 1 and self.tile_size**3 * 4 > self.cache_limit:
            self.tile_size //= 2
        self._cache: OrderedDict[tuple, np.ndarray] = OrderedDict()
        self._cache_bytes = 0
        template = (json.dumps('[[%d, %d, %d], "mix"]') + ']').encode()
        self._head, self._tail = template.replace(b'%d', b'%s', 1).split(b'%s')

    def tiles(self, box: AABB, prefix, palette: Palette) -> Iterator[tuple[AABB, np.ndarray]]:
        """世界座標のbox内のタイルと、Palette内の抽選添字を返す。"""
        width = self.tile_size
        for ty in range(box.min.y // width, box.max.y // width + 1):
            for tx in range(box.min.x // width, box.max.x // width + 1):
                for tz in range(box.min.z // width, box.max.z // width + 1):
                    low = Vec3(tx * width, ty * width, tz * width)
                    tile = AABB(Vec3.max(low, box.min), Vec3.min(
                        low + Vec3(width - 1, width - 1, width - 1), box.max,
                    ))
                    key = prefix.digest(), palette, tile
                    choices = self._cache.pop(key, None)
                    if choices is None:
                        choices = self._sample(tile, prefix, palette)
                        entry_bytes = choices.nbytes + 1024
                        if entry_bytes <= self.cache_limit:
                            while self._cache_bytes + entry_bytes > self.cache_limit:
                                _, expired = self._cache.popitem(last=False)
                                self._cache_bytes -= expired.nbytes + 1024
                            self._cache_bytes += entry_bytes
                    if choices.nbytes + 1024 <= self.cache_limit:
                        self._cache[key] = choices
                    yield tile, choices

    def _sample(self, box: AABB, prefix, palette: Palette) -> np.ndarray:
        size = box.size
        result = np.empty(box.volume, dtype=np.uint32)
        cumulative = np.cumsum([entry.weight for entry in palette.entries])
        total = float(cumulative[-1])
        hashes = self._hashes(box, prefix)
        for start in range(0, box.volume, self.batch_size):
            end = min(start + self.batch_size, box.volume)
            words = np.frombuffer(b''.join(islice(hashes, end - start)), dtype='>u4').reshape(-1, 8)
            draws = first_draws(words)
            # random.choicesは最後の累積値を探索範囲から除く（丸めによる末尾超過を防ぐ）。
            result[start:end] = np.searchsorted(cumulative[:-1], draws * total, side='right')
        return result.reshape(size.y, size.z, size.x)

    def _hashes(self, box: AABB, prefix):
        xs = [self._head + str(x).encode() for x in range(box.min.x, box.max.x + 1)]
        for y in range(box.min.y, box.max.y + 1):
            for z in range(box.min.z, box.max.z + 1):
                tail = self._tail % (y, z)
                for x in xs:
                    digest = prefix.copy()
                    digest.update(x + tail)
                    yield digest.digest()
