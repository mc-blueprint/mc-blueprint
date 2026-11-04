"""計画済み地形を局所配列で施工する。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Self

import numpy as np

from mcblueprint.blockdata import load_block_data
from mcblueprint.model import AABB, BlockState, Vec3
from mcblueprint.operations.base import BlockSpec, ExecutionContext, Operation, intersect
from mcblueprint.operations.registry import register
from mcblueprint.volume import RegionVolume

if TYPE_CHECKING:
    from mcblueprint.terrain_density import TerrainDensity


@register
class TerrainDensityOperation(Operation):
    type = "terrain_density"

    def __init__(self, path: str, plan: TerrainDensity, minecraft_version: str,
                 comment: str | None = None) -> None:
        super().__init__(path, comment)
        self.plan = plan
        self.blockdata = load_block_data(minecraft_version)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], path: str) -> Self:
        from mcblueprint.terrain_density import TerrainDensity

        return cls(path, TerrainDensity.from_dict(data["plan"]), data["minecraftVersion"],
                   data.get("comment"))

    def bounds(self) -> AABB:
        return self.plan.field.bounds

    @property
    def generation_memory_bytes(self) -> int:
        # 最大32×32×16の密度作業配列と、計画の全体2D場。
        field = self.plan.field
        return 4 * 1024 * 1024 + field.heights.nbytes + field.coarse_delta.nbytes

    def _tops(self, area: AABB) -> np.ndarray:
        bounds = self.bounds()
        tops = np.full((area.size.z, area.size.x), bounds.min.y, dtype=np.int32)
        for y in range(bounds.min.y, bounds.max.y + 1, 16):
            box = AABB(Vec3(area.min.x, y, area.min.z),
                       Vec3(area.max.x, min(y + 15, bounds.max.y), area.max.z))
            solid = self.plan.sample(box).density >= 0
            levels = np.arange(box.min.y, box.max.y + 1)[:, None, None]
            tops = np.maximum(tops, np.max(np.where(solid, levels, bounds.min.y), axis=0))
        return tops

    def _write(self, ctx: ExecutionContext, box: AABB, states: tuple[BlockState, ...],
               choices: np.ndarray, role: str, mask: np.ndarray | None = None) -> None:
        attrs = replace(ctx.attributes, role=ctx.attributes.role or role,
                        element=ctx.attributes.element or "terrain#0",
                        source=ctx.attributes.source or "config.terrain",
                        operation=ctx.operation_id)
        if isinstance(ctx.volume, RegionVolume) and ctx.transform.rows == (
                (1, 0, 0), (0, 1, 0), (0, 0, 1)):
            ctx.volume.set_box(ctx.transform.bounds(box), states, choices, attrs,
                               on_conflict=ctx.on_conflict, mask=mask)
            return
        previous = ctx.attributes
        ctx.attributes = attrs
        try:
            for iy, iz, ix in np.ndindex(choices.shape):
                if mask is None or mask[iy, iz, ix]:
                    ctx.place(box.min + Vec3(ix, iy, iz), BlockSpec(block=states[
                        int(choices[iy, iz, ix])]))
        finally:
            ctx.attributes = previous

    def apply(self, ctx: ExecutionContext) -> None:
        area = ctx.clip_box(self.bounds())
        if area is None:
            return
        field = self.plan.field
        # XZ32×32・Y16まで。全高の密度や完成地形を保持しない。
        for z in range(area.min.z, area.max.z + 1, 32):
            for x in range(area.min.x, area.max.x + 1, 32):
                tile = AABB(Vec3(x, area.min.y, z),
                            Vec3(min(x + 31, area.max.x), area.max.y,
                                 min(z + 31, area.max.z)))
                tops = self._tops(tile)
                columns = [[field.column(px, pz, self.blockdata)
                            for px in range(tile.min.x, tile.max.x + 1)]
                           for pz in range(tile.min.z, tile.max.z + 1)]
                names = ["air", "stone", "dirt", "grass_block", "sand", "sandstone",
                         "snow_block", "water"]
                states = tuple(BlockState.parse(name) for name in names)
                indices = {str(state): i for i, state in enumerate(states)}
                surfaces = np.array([[indices[c.surface] for c in row] for row in columns])
                soils = np.array([[indices[c.soil] for c in row] for row in columns])
                for y in range(tile.min.y, tile.max.y + 1, 16):
                    box = AABB(Vec3(x, y, z),
                               Vec3(tile.max.x, min(y + 15, tile.max.y), tile.max.z))
                    sample = self.plan.sample(box)
                    solid = sample.density >= 0
                    levels = np.arange(box.min.y, box.max.y + 1)[:, None, None]
                    choices = np.where(solid, 1, 0)
                    choices = np.where(solid & (levels >= tops - 3), soils, choices)
                    choices = np.where(solid & (levels == tops), surfaces, choices)
                    choices[sample.support] = 1
                    water = self.plan.water_level
                    if water is not None:
                        choices = np.where(~solid & (levels <= water), 7, choices)
                    self._write(ctx, box, states, choices, "terrain.ground")
                    for role, mask in (("terrain.cavity", sample.cavity),
                                       ("terrain.opening", sample.opening),
                                       ("terrain.support", sample.support),
                                       ("terrain.water", choices == 7)):
                        if np.any(mask):
                            self._write(ctx, box, states, choices, role, mask)
                self._vegetation(ctx, tile)

    def _vegetation(self, ctx: ExecutionContext, tile: AABB) -> None:
        field = self.plan.field
        if not field.settings["vegetation"] or self.plan.main_cave:
            return
        bounds = field.bounds
        expanded = AABB(Vec3(max(bounds.min.x, tile.min.x - 2), bounds.min.y,
                             max(bounds.min.z, tile.min.z - 2)),
                        Vec3(min(bounds.max.x, tile.max.x + 2), bounds.max.y,
                             min(bounds.max.z, tile.max.z + 2)))
        tops = self._tops(expanded)
        for z in range(max(bounds.min.z, tile.min.z - 2), min(bounds.max.z, tile.max.z + 2) + 1):
            for x in range(max(bounds.min.x, tile.min.x - 2),
                           min(bounds.max.x, tile.max.x + 2) + 1):
                column = field.column(x, z, self.blockdata)
                if column.vegetation is None:
                    continue
                if tops[z - expanded.min.z, x - expanded.min.x] != column.top:
                    continue
                if column.vegetation == "grass":
                    canopy = AABB(Vec3(x, column.top + 1, z), Vec3(x, column.top + 1, z))
                else:
                    canopy = AABB(Vec3(x - 2, column.top + 3, z - 2),
                                  Vec3(x + 2, column.top + 5, z + 2))
                trunk = AABB(Vec3(x, column.top + 1, z), Vec3(x, column.top + 4, z))
                if column.vegetation == "tree" and np.any(self.plan.sample(trunk).density >= 0):
                    continue
                previous = ctx.attributes
                ctx.attributes = replace(previous, role=previous.role or "terrain.vegetation",
                                         element=previous.element or "terrain#0",
                                         source=previous.source or "config.terrain.vegetation",
                                         operation=ctx.operation_id)
                try:
                    for box, name in ((canopy, "short_grass" if column.vegetation == "grass"
                                       else "oak_leaves[persistent=true]"),
                                      (trunk, "oak_log[axis=y]")):
                        if box == trunk and column.vegetation == "grass":
                            continue
                        part = intersect(tile, box)
                        if part is not None:
                            sample = self.plan.sample(part)
                            choices = np.zeros(sample.density.shape, dtype=np.uint8)
                            self._write(ctx, part, (BlockState.parse(name),), choices,
                                        "terrain.vegetation",
                                        (sample.density < 0) & ~sample.opening)
                finally:
                    ctx.attributes = previous
