"""明示した矩形敷地を全列検査し、変更を加えずに造成計画を作る。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mcblueprint.errors import BlueprintError
from mcblueprint.model.block import BlockState
from mcblueprint.model.vec import AABB, Vec3
from mcblueprint.terrain import ColumnWrites, RegionTerrainSnapshot, TerrainPlacement
from mcblueprint.volume import MAX_VOLUME

STABLE_GROUND = frozenset(
    "minecraft:" + name for name in (
        "stone", "granite", "diorite", "andesite", "deepslate", "cobbled_deepslate",
        "tuff", "calcite", "dirt", "coarse_dirt", "rooted_dirt", "grass_block",
        "bedrock", "cobblestone", "stone_bricks", "sandstone", "red_sandstone",
    )
)
AIR = BlockState.parse("air")


@dataclass(frozen=True, slots=True)
class FoundationSpec:
    """同Yのfootprint、探索帯と列ごとの上限。地盤材は対応する通常完全ブロックのみ。"""

    footprint: AABB
    scan_min_y: int
    scan_max_y: int
    max_cut: int
    max_fill: int
    max_support_depth: int
    ground: frozenset[str]
    block: BlockState

    def __post_init__(self) -> None:
        floor_y = self.footprint.min.y
        if self.footprint.max.y != floor_y:
            raise BlueprintError("foundation.from/to は同じ床Yにしてください")
        if self.scan_min_y > floor_y - 1 or self.scan_max_y < floor_y + 1:
            raise BlueprintError("foundation.scan は床Y-1から床Y+1を含む必要があります")
        if self.max_cut < 0 or self.max_fill < 0 or self.max_support_depth < 1:
            raise BlueprintError("foundation の上限は切土/盛土0以上、支持深さ1以上です")
        if (not self.ground or not self.ground <= STABLE_GROUND
                or self.block.id not in STABLE_GROUND or self.block.properties):
            raise BlueprintError(
                "foundation.ground/block は対応する安定地盤材のIDを指定してください"
            )

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> FoundationSpec:
        """Schema検証済みのfoundation項目を変換する。対応外条件はBlueprintError。"""
        states = [BlockState.parse(text) for text in raw["ground"]]
        if any(state.properties for state in states):
            raise BlueprintError("foundation.ground はプロパティを持たない地盤材IDにしてください")
        return cls(
            AABB.of(Vec3.from_seq(raw["from"]), Vec3.from_seq(raw["to"])),
            raw["scan"]["minY"], raw["scan"]["maxY"],
            raw["maxCut"], raw["maxFill"], raw["maxSupportDepth"],
            frozenset(state.id for state in states), BlockState.parse(raw["block"]),
        )

    @property
    def scan_bounds(self) -> AABB:
        return AABB(
            self.footprint.min.with_axis("y", self.scan_min_y),
            self.footprint.max.with_axis("y", self.scan_max_y),
        )


@dataclass(frozen=True, slots=True)
class FoundationIssue:
    position: Vec3
    reason: str
    required_cut: int | None = None
    required_fill: int | None = None
    required_support_depth: int | None = None


class FoundationError(BlueprintError):
    """造成不可。issuesは拒否座標・原因・算出できた必要深さ。volumeは変更されない。"""

    def __init__(self, issues: tuple[FoundationIssue, ...]) -> None:
        self.issues = issues
        lines = []
        for issue in issues:
            depths = ""
            if issue.required_cut is not None:
                depths = (f" (cut={issue.required_cut}, fill={issue.required_fill}, "
                          f"support={issue.required_support_depth})")
            lines.append(f"{issue.position}: {issue.reason}{depths}")
        super().__init__("foundation を造成できません:\n" + "\n".join(lines))


@dataclass(frozen=True, slots=True)
class FoundationResult:
    """造成だけの実変更セル数と列ごとの必要深さの最大値。後続Operationは含まない。"""

    footprint: AABB
    cut_cells: int
    fill_cells: int
    floor_cells: int
    required_cut: int
    required_fill: int
    required_support_depth: int

    @property
    def floor_y(self) -> int:
        return self.footprint.min.y

    @property
    def entrance_y(self) -> int:
        return self.floor_y + 1

    @property
    def changed_cells(self) -> int:
        return self.cut_cells + self.fill_cells + self.floor_cells

    def to_json(self) -> dict[str, Any]:
        return {
            "from": self.footprint.min.to_list(), "to": self.footprint.max.to_list(),
            "floorY": self.floor_y, "entranceY": self.entrance_y,
            "cutCells": self.cut_cells, "fillCells": self.fill_cells,
            "floorCells": self.floor_cells, "changedCells": self.changed_cells,
            "requiredCut": self.required_cut, "requiredFill": self.required_fill,
            "requiredSupportDepth": self.required_support_depth,
        }


@dataclass(frozen=True, slots=True)
class FoundationPlan:
    """重複のないBlueprint座標の変更列と集計。材質は基礎材または通常air。"""

    writes: tuple[tuple[Vec3, BlockState], ...] | ColumnWrites
    result: FoundationResult


def plan_foundation(
    spec: FoundationSpec, terrain: TerrainPlacement,
) -> FoundationPlan:
    """全footprint列の探索帯を元snapshotで検査し、不変の造成計画を返す。

    scan帯全体が施工/取得内にあり、
    通常airと明示groundだけで、下から地盤→airの順に並ぶことを要求する。
    失敗はFoundationError.issues、成功は実際に状態が変わるセルだけのwrites。
    snapshot・既存volumeには一切書き込まない。
    scan帯外の地表・障害物・支持やワールドの状態は保証しない。
    """
    bounds = spec.scan_bounds
    region = terrain.region
    compact = isinstance(terrain.snapshot, RegionTerrainSnapshot)
    if region.volume > MAX_VOLUME:
        raise FoundationError((FoundationIssue(bounds.min, "施工セル総数の上限を超えます"),))
    if not region.contains(bounds.min) or not region.contains(bounds.max):
        outside = bounds.min if not region.contains(bounds.min) else bounds.max
        raise FoundationError((FoundationIssue(outside, "探索帯が施工/取得範囲外です"),))
    if compact and bounds.size.x * bounds.size.z * 512 > terrain.snapshot.plan_budget_bytes:
        raise BlueprintError("造成の列検査・計画が limits.max_memory_mb に収まりません")
    floor_y = spec.footprint.min.y
    issues: list[FoundationIssue] = []
    writes = ColumnWrites(terrain.snapshot.plan_budget_bytes) if compact else []
    cut_cells = fill_cells = floor_cells = 0
    required_cut = required_fill = 0
    required_support = 1
    for z in range(bounds.min.z, bounds.max.z + 1):
        for x in range(bounds.min.x, bounds.max.x + 1):
            surface = None
            first_air = None
            issue = None
            for y in range(bounds.min.y, bounds.max.y + 1):
                pos = Vec3(x, y, z)
                state = terrain.snapshot.get(terrain.snapshot.to_local(pos))
                if state is None:
                    issue = FoundationIssue(pos, "未取得のセルです")
                    break
                if state == AIR:
                    if first_air is None:
                        first_air = y
                elif state.id not in spec.ground:
                    issue = FoundationIssue(pos, f"対応外の地盤・障害物です: {state}")
                    break
                elif first_air is not None:
                    issue = FoundationIssue(Vec3(x, first_air, z), "地盤下に空洞があります")
                    break
                else:
                    surface = y
            if issue is None and surface is None:
                issue = FoundationIssue(Vec3(x, bounds.min.y, z), "支持地盤がありません")
            if issue is None and first_air is None:
                issue = FoundationIssue(
                    Vec3(x, bounds.max.y, z), "探索上端にairがなく地表未確定です",
                )
            if issue is not None:
                issues.append(issue)
                continue
            assert surface is not None
            cut = max(0, surface - floor_y)
            fill = max(0, floor_y - surface)
            support = max(1, fill)
            required_cut = max(required_cut, cut)
            required_fill = max(required_fill, fill)
            required_support = max(required_support, support)
            if cut > spec.max_cut or fill > spec.max_fill or support > spec.max_support_depth:
                issues.append(FoundationIssue(
                    Vec3(x, surface, z), "切土・盛土・支持深さの上限を超えます", cut, fill, support,
                ))
                continue
            for y in range(min(surface + 1, floor_y), max(surface, floor_y) + 1):
                pos = Vec3(x, y, z)
                state = AIR if y > floor_y else spec.block
                if terrain.snapshot.get(terrain.snapshot.to_local(pos)) == state:
                    continue
                writes.append((pos, state))
                if y > floor_y:
                    cut_cells += 1
                elif y < floor_y:
                    fill_cells += 1
                else:
                    floor_cells += 1
    if issues:
        raise FoundationError(tuple(issues))
    return FoundationPlan(writes if compact else tuple(writes), FoundationResult(
        spec.footprint, cut_cells, fill_cells, floor_cells,
        required_cut, required_fill, required_support,
    ))
