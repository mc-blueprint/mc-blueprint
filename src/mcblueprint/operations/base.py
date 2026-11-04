"""Operation base class and execution model (see docs/ARCHITECTURE.md section 5)."""

from __future__ import annotations

import hashlib
import json
import random
from abc import ABC, abstractmethod
from collections import ChainMap
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any, ClassVar, Self

from mcblueprint.errors import BlueprintError
from mcblueprint.model.block import BlockState
from mcblueprint.model.palette import Palette
from mcblueprint.model.vec import AABB, Vec3
from mcblueprint.operations.randomness import PaletteSampler
from mcblueprint.operations.transform import Transform, mirror_state
from mcblueprint.volume import EMPTY_ATTRIBUTES, BlockVolume, CellAttributes, RegionVolume

MAX_DEPTH = 8

__all__ = [
    "MAX_DEPTH",
    "BlockSpec",
    "ExecutionContext",
    "Operation",
    "PlacementOperation",
    "Transform",
    "derive_seed",
    "mirror_state",
    "parse_choice",
    "parse_int",
    "parse_vec",
]


def derive_seed(seed: int, stage: str, element: str) -> int:
    """正規化したJSONのSHA-256から段・要素のseedを導出する。"""
    raw = json.dumps([seed, stage, element], ensure_ascii=False, separators=(",", ":"))
    return int.from_bytes(hashlib.sha256(raw.encode("utf-8")).digest(), "big")


@dataclass(frozen=True, slots=True)
class BlockSpec:
    """具体ブロック、Palette名、またはMaterialize前の役割参照。"""

    block: BlockState | None = None
    palette: str | None = None
    role: str | None = None
    shape: str = "full"
    state: tuple[tuple[str, str], ...] = ()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], path: str) -> BlockSpec:
        has_block = "block" in data
        has_palette = "palette" in data
        if has_block == has_palette:
            raise BlueprintError(f"{path}: exactly one of 'block' or 'palette' must be given")
        if has_block:
            if isinstance(data["block"], Mapping):
                ref = data["block"]
                if set(ref) != {"role", "shape", "state"}:
                    raise BlueprintError(f"{path}.block: role / shape / state が必要です")
                return cls(role=ref["role"], shape=ref["shape"],
                           state=tuple(sorted(ref["state"].items())))
            try:
                return cls(block=BlockState.parse(data["block"]))
            except BlueprintError as exc:
                raise BlueprintError(f"{path}.block: {exc}") from None
        return cls(palette=data["palette"])


class ExecutionContext:
    """Operation実行時のvolume・抽選・変換・属性。

    generation_seed は通常生成の実効seedを新Operationへ渡し、既存の共有Randomは維持する。
    """

    def __init__(
        self,
        volume: BlockVolume | RegionVolume,
        rng: random.Random,
        palettes: Mapping[str, Palette],
        transform: Transform | None = None,
        depth: int = 1,
        *,
        seed: int | None = None,
        attributes: CellAttributes = EMPTY_ATTRIBUTES,
        operation_id: str = "",
        on_conflict: str = "error",
        sampler: PaletteSampler | None = None,
        generation_seed: int | None = None,
    ) -> None:
        self.volume = volume
        self.rng = rng
        self.palettes = palettes
        self.transform = transform or Transform.identity()
        self.depth = depth
        self.seed = seed
        self.generation_seed = seed if generation_seed is None else generation_seed
        self.attributes = attributes
        self.operation_id = operation_id
        self.on_conflict = on_conflict
        self._draw_hash = None
        self.sampler = sampler

    def _mix_prefix(self):
        if self._draw_hash is None:
            stage_seed = derive_seed(self.seed, "generate", self.attributes.element or "")
            prefix = json.dumps(
                [stage_seed, self.operation_id], ensure_ascii=False, separators=(",", ":"),
            )[:-1] + ','
            self._draw_hash = hashlib.sha256(prefix.encode('utf-8'))
        return self._draw_hash

    def execute(
        self, operation: Operation, *, apply: Callable[[ExecutionContext], None] | None = None,
    ) -> None:
        """入力属性を継承して実行する。領域経路は無関係なOperationを飛ばす。

        apply指定時は同じ属性スコープで専用の実行処理を呼ぶ（接合の共有面除外用）。
        """
        if isinstance(self.volume, RegionVolume):
            if intersect(self.transform.bounds(operation.bounds()), self.volume.region) is None:
                return
        previous = self.attributes, self.operation_id, self.on_conflict, self._draw_hash
        self.operation_id = f"{self.operation_id}/{operation.id or operation.path}"
        values = operation.attributes
        if self.seed is not None or values or self.attributes != EMPTY_ATTRIBUTES:
            self.attributes = replace(self.attributes, **values, operation=self.operation_id)
        if self.attributes.reserved and self.attributes.owner is None:
            self.attributes = replace(
                self.attributes, owner=self.attributes.element or self.operation_id,
            )
        self.on_conflict = operation.on_conflict or self.on_conflict
        self._draw_hash = None
        try:
            (apply or operation.apply)(self)
        finally:
            self.attributes, self.operation_id, self.on_conflict, self._draw_hash = previous

    def resolve(self, spec: BlockSpec) -> BlockState:
        if spec.block is not None:
            return spec.block
        assert spec.palette is not None
        try:
            palette = self.palettes[spec.palette]
        except KeyError:
            raise BlueprintError(f"Unknown palette: {spec.palette!r}") from None
        return palette.choose(self.rng)

    def place(self, pos: Vec3, spec: BlockSpec) -> None:
        world = self.transform.apply(pos)
        if isinstance(self.volume, RegionVolume) and not self.volume.region.contains(world):
            return
        if self.seed is None or spec.block is not None:
            state = self.resolve(spec)
        else:
            digest = self._mix_prefix().copy()
            digest.update((json.dumps(json.dumps([world.to_list(), "mix"])) + ']').encode())
            draw_seed = int.from_bytes(digest.digest(), 'big')
            palette = self.palettes.get(spec.palette)
            if palette is None:
                raise BlueprintError(f"Unknown palette: {spec.palette!r}")
            state = palette.choose(random.Random(draw_seed))
        attrs = self.attributes
        if attrs.face is not None:
            attrs = replace(attrs, face=self.transform.apply_direction(attrs.face))
        state = self.transform.apply_state(state)
        if attrs == EMPTY_ATTRIBUTES and self.on_conflict == "error":
            self.volume.set(world, state)
        else:
            self.volume.set(world, state, attrs, on_conflict=self.on_conflict)

    def place_all(self, positions: Iterable[Vec3], spec: BlockSpec) -> None:
        for pos in positions:
            self.place(pos, spec)

    def fill(self, box: AABB, spec: BlockSpec) -> None:
        """領域経路の矩形を、世界座標の配列へ配置する。旧経路はセル順を保つ。"""
        from mcblueprint.operations.box_like import iter_box

        if not isinstance(self.volume, RegionVolume):
            self.place_all(iter_box(box), spec)
            return
        area = intersect(self.transform.bounds(box), self.volume.region)
        if area is None:
            return
        attrs = self.attributes
        if attrs.face is not None:
            attrs = replace(attrs, face=self.transform.apply_direction(attrs.face))
        if spec.block is not None:
            states = (self.transform.apply_state(spec.block),)
            self.volume.set_box(area, states, None, attrs, on_conflict=self.on_conflict)
            return
        palette = self.palettes.get(spec.palette)
        if palette is None:
            raise BlueprintError(f"Unknown palette: {spec.palette!r}")
        states = tuple(self.transform.apply_state(entry.block) for entry in palette.entries)
        if len(states) == 1:
            self.volume.set_box(area, states, None, attrs, on_conflict=self.on_conflict)
            return
        assert self.sampler is not None
        for tile, choices in self.sampler.tiles(area, self._mix_prefix(), palette):
            part = intersect(tile, area)
            assert part is not None
            lo, hi = part.min - tile.min, part.max - tile.min
            selected = choices[lo.y:hi.y + 1, lo.z:hi.z + 1, lo.x:hi.x + 1]
            self.volume.set_box(part, states, selected, attrs, on_conflict=self.on_conflict)

    def clip_box(self, box: AABB) -> AABB | None:
        """配置矩形を現在の領域へ切り詰め、ローカル座標で返す。"""
        if not isinstance(self.volume, RegionVolume):
            return box
        rows = self.transform.rows
        inverse = Transform(tuple(tuple(rows[j][i] for j in range(3)) for i in range(3)))
        inverse = Transform(inverse.rows, -inverse.apply(self.transform.offset))
        return intersect(box, inverse.bounds(self.volume.region))

    def child(
        self, transform: Transform, palettes: Mapping[str, Palette] | None = None,
        *, instance: str = "", nesting: bool = True,
    ) -> ExecutionContext:
        """Context for nested operations: composes ``transform`` inside the current one.

        ``palettes`` (e.g. a component's own) take precedence over the parent's.
        """
        if nesting and self.depth >= MAX_DEPTH:
            raise BlueprintError(f"Operation nesting deeper than {MAX_DEPTH} levels")
        merged = self.palettes if not palettes else ChainMap(dict(palettes), self.palettes)
        return ExecutionContext(
            self.volume,
            self.rng,
            merged,
            transform.then(self.transform),
            self.depth + int(nesting),
            seed=self.seed,
            attributes=self.attributes,
            operation_id=f"{self.operation_id}@{instance}:{transform.rows}:{transform.offset}",
            on_conflict=self.on_conflict,
            sampler=self.sampler,
            generation_seed=self.generation_seed,
        )


class Operation(ABC):
    """One step of a Blueprint. Subclasses set ``type`` and register themselves."""

    type: ClassVar[str]

    def __init__(self, path: str, comment: str | None = None) -> None:
        self.path = path
        self.comment = comment
        self.id: str | None = None
        self.attributes: dict[str, str] = {}
        self.on_conflict: str | None = None

    @classmethod
    @abstractmethod
    def from_dict(cls, data: Mapping[str, Any], path: str) -> Self:
        """Build from schema-valid JSON data; ``path`` is used in error messages."""

    @abstractmethod
    def bounds(self) -> AABB:
        """Box containing every cell this operation can write, without executing it."""

    @abstractmethod
    def apply(self, ctx: ExecutionContext) -> None:
        """Write this operation's cells into ``ctx``."""

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.path}>"


class PlacementOperation(Operation):
    """Operation that writes one block/palette to a set of cells."""

    def __init__(self, path: str, spec: BlockSpec, comment: str | None = None) -> None:
        super().__init__(path, comment)
        self.spec = spec

    @abstractmethod
    def cells(self) -> Iterable[Vec3]:
        """Cells in placement order (see docs/OPERATIONS.md)."""

    def apply(self, ctx: ExecutionContext) -> None:
        ctx.place_all(self.cells(), self.spec)


def intersect(a: AABB, b: AABB) -> AABB | None:
    """交差しない矩形はNoneを返す（境界セルを含む）。"""
    low, high = Vec3.max(a.min, b.min), Vec3.min(a.max, b.max)
    if any(low[i] > high[i] for i in range(3)):
        return None
    return AABB(low, high)


def parse_vec(data: Mapping[str, Any], key: str, path: str) -> Vec3:
    try:
        return Vec3.from_seq(data[key])
    except KeyError:
        raise BlueprintError(f"{path}: missing required '{key}'") from None
    except (TypeError, ValueError):
        raise BlueprintError(f"{path}.{key}: must be an array of 3 integers") from None


def parse_int(
    data: Mapping[str, Any], key: str, path: str, *, minimum: int, default: int | None = None
) -> int:
    if key not in data:
        if default is None:
            raise BlueprintError(f"{path}: missing required '{key}'")
        return default
    value = data[key]
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise BlueprintError(f"{path}.{key}: must be an integer >= {minimum}")
    return value


def parse_choice(
    data: Mapping[str, Any], key: str, path: str, choices: tuple[str, ...], default: str
) -> str:
    value = data.get(key, default)
    if value not in choices:
        raise BlueprintError(f"{path}.{key}: must be one of {', '.join(choices)}")
    return value
