"""明示した足元セル間を、限定した歩行・直進階段モデルで検査する。"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Literal

from mcblueprint import blockdata
from mcblueprint.errors import BlueprintError
from mcblueprint.model.block import BlockState
from mcblueprint.model.vec import AABB, Vec3
from mcblueprint.operations.building import BuildingLayout
from mcblueprint.volume import BlockVolume

WOODS = (
    "oak", "spruce", "birch", "jungle", "acacia", "dark_oak", "mangrove", "cherry",
    "bamboo", "pale_oak", "crimson", "warped", "poplar",
)
FULL_BLOCKS = frozenset({
    "stone", "cobblestone", "mossy_cobblestone", "stone_bricks", "mossy_stone_bricks",
    "cracked_stone_bricks", "chiseled_stone_bricks", "bricks", "dirt", "grass_block",
    "glass", "deepslate", "cobbled_deepslate", "deepslate_bricks", "polished_andesite",
    "andesite", "granite", "diorite", "sandstone", "smooth_sandstone", "quartz_block",
    "smooth_quartz", "iron_block", "gold_block", "diamond_block", "emerald_block",
    *(f"{wood}_planks" for wood in WOODS),
})
DOORS = frozenset({
    *(f"{wood}_door" for wood in WOODS),
    *(f"{wax}{age}copper_door" for wax in ("", "waxed_")
      for age in ("", "exposed_", "weathered_", "oxidized_")),
})
DIRECTIONS = (("north", Vec3(0, 0, -1)), ("east", Vec3(1, 0, 0)),
              ("south", Vec3(0, 0, 1)), ("west", Vec3(-1, 0, 0)))
OPPOSITE = {"north": "south", "south": "north", "east": "west", "west": "east"}
UP = Vec3(0, 1, 0)


@dataclass(frozen=True, slots=True)
class TraversalIssue:
    position: Vec3
    reason: str
    block: str | None

    def to_dict(self) -> dict:
        return {"position": self.position.to_list(), "reason": self.reason, "block": self.block}


@dataclass(frozen=True, slots=True)
class TraversalResult:
    """reachable はこの限定モデルの到達性であり、実機全動作の保証ではない。

    path は各床・階段上端の足元セル列。blocked / unsupported では空。
    issues は階段の進行を妨げる原因を優先し、終了点に近い順で返す。
    探索境界の報告であり、最短修復箇所とは限らない。
    """

    start: Vec3
    end: Vec3
    status: Literal["reachable", "blocked", "unsupported"]
    path: tuple[Vec3, ...]
    issues: tuple[TraversalIssue, ...]

    def to_dict(self) -> dict:
        return {"start": self.start.to_list(), "end": self.end.to_list(), "status": self.status,
                "path": [p.to_list() for p in self.path],
                "issues": [issue.to_dict() for issue in self.issues]}


class _Walker:
    def __init__(self, volume: BlockVolume, minecraft_version: str, max_nodes: int | None = None):
        self.volume = volume
        self.max_nodes = max_nodes
        self.blocks = blockdata.load_block_data(minecraft_version)
        bounds = volume.bounds()
        self.bounds = AABB(bounds.min + UP, bounds.max + UP) if bounds is not None else None
        self.kinds: dict[BlockState | None, str] = {}
        self.stands: dict[Vec3, TraversalIssue | None] = {}
        self.cache_limit = 4096 if max_nodes is None else min(16384, max_nodes // 8)
        self.facings: dict[BlockState, str | None] = {}

    def kind(self, state: BlockState | None) -> str:
        if state in self.kinds:
            return self.kinds[state]
        if state is None:
            kind = "air"
        elif self.blocks.check_state(state) is not None or not state.id.startswith("minecraft:"):
            kind = "unsupported"
        else:
            name = state.id.removeprefix("minecraft:")
            complete = self.blocks.complete(state)
            if name in ("air", "cave_air", "void_air"):
                kind = "air"
            elif name in FULL_BLOCKS or (
                name.endswith("_slab") and complete.get("type") == "double"
            ):
                kind = "full"
            elif name.endswith("_stairs") and all(
                complete.get(key) == value
                for key, value in (("half", "bottom"), ("shape", "straight"),
                                   ("waterlogged", "false"))
            ):
                kind = "stairs"
            elif name in DOORS:
                kind = "door"
            else:
                kind = "unsupported"
        self.kinds[state] = kind
        return kind

    def issue(self, pos: Vec3, reason: str) -> TraversalIssue:
        state = self.volume.get(pos)
        return TraversalIssue(pos, reason, state.to_string() if state else None)

    def door_issue(self, pos: Vec3, state: BlockState) -> TraversalIssue | None:
        complete = self.blocks.complete(state)
        other_pos = pos + (UP if complete.get("half") == "lower" else -UP)
        other = self.volume.get(other_pos)
        if other is None or other.id != state.id:
            return self.issue(pos, "unsupported_door_pair")
        other = self.blocks.complete(other)
        if other.get("half") == complete.get("half") or any(
            other.get(key) != complete.get(key) for key in ("facing", "hinge", "open")
        ):
            return self.issue(pos, "unsupported_door_pair")
        return None

    def clearance(self, pos: Vec3) -> TraversalIssue | None:
        state = self.volume.get(pos)
        kind = self.kind(state)
        if kind == "door":
            assert state is not None
            return self.door_issue(pos, state)
        if kind == "unsupported":
            return self.issue(pos, "unsupported_shape")
        if kind != "air":
            return self.issue(pos, "body_or_head_blocked")
        return None

    def stand(self, feet: Vec3) -> TraversalIssue | None:
        if feet in self.stands:
            return self.stands[feet]
        issue = self._stand(feet)
        if self.cache_limit:
            if len(self.stands) == self.cache_limit:
                self.stands.clear()
            self.stands[feet] = issue
        return issue

    def _stand(self, feet: Vec3) -> TraversalIssue | None:
        if self.bounds is None or not self.bounds.contains(feet):
            return self.issue(feet, "outside_search_bounds")
        for pos in (feet, feet + UP):
            issue = self.clearance(pos)
            if issue is not None:
                return issue
        below = feet - UP
        kind = self.kind(self.volume.get(below))
        if kind == "unsupported" or kind == "door":
            return self.issue(below, "unsupported_support")
        if kind not in ("full", "stairs"):
            return self.issue(below, "missing_floor")
        return None

    def direction(self, feet: Vec3) -> str | None:
        state = self.volume.get(feet - UP)
        if self.kind(state) != "stairs":
            return None
        assert state is not None
        if state not in self.facings:
            self.facings[state] = self.blocks.complete(state).get("facing")
        return self.facings[state]

    def route(self, start: Vec3, end: Vec3) -> TraversalResult:
        for feet in (start, end):
            issue = self.stand(feet)
            if issue:
                status = "unsupported" if issue.reason.startswith("unsupported") else "blocked"
                return TraversalResult(start, end, status, (), (issue,))
        queue = deque([start])
        previous: dict[Vec3, Vec3 | None] = {start: None}
        issues: set[TraversalIssue] = set()
        stairs_issues: set[TraversalIssue] = set()
        while queue:
            if self.max_nodes is not None and (
                len(previous) + len(issues) + len(stairs_issues) > self.max_nodes
            ):
                raise BlueprintError("traversalの探索状態が limits.max_memory_mb に収まりません")
            current = queue.popleft()
            if current == end:
                path = []
                point: Vec3 | None = end
                while point is not None:
                    path.append(point)
                    point = previous[point]
                return TraversalResult(start, end, "reachable", tuple(reversed(path)), ())
            source_direction = self.direction(current)
            for direction, offset in DIRECTIONS:
                for dy in (0, 1, -1):
                    target = current + offset + UP * dy
                    if target in previous:
                        continue
                    issue = self.stand(target)
                    if issue:
                        issues.add(issue)
                        if self.kind(self.volume.get(target - UP)) == "stairs":
                            stairs_issues.add(issue)
                        continue
                    target_direction = self.direction(target)
                    if (dy == 1 and target_direction != direction) or (
                        dy == -1 and source_direction != OPPOSITE[direction]
                    ) or any(
                        facing is not None and facing not in (direction, OPPOSITE[direction])
                        for facing in (source_direction, target_direction)
                    ):
                        issue = self.issue(target - UP, "stairs_direction_or_step")
                        issues.add(issue)
                        if source_direction is not None or target_direction is not None:
                            stairs_issues.add(issue)
                        continue
                    if dy:
                        lower = current if dy > 0 else target
                        issue = self.clearance(lower + UP * 2)
                        if issue is not None:
                            issues.add(issue)
                            stairs_issues.add(issue)
                            continue
                    previous[target] = current
                    queue.append(target)
        ordered = tuple(sorted(issues, key=lambda issue: (
            issue not in stairs_issues,
            sum(abs(a - b) for a, b in zip(issue.position, end, strict=True)),
            tuple(issue.position), issue.reason,
        )))
        status = (
            "unsupported" if any(i.reason.startswith("unsupported") for i in ordered) else "blocked"
        )
        return TraversalResult(start, end, status, (), ordered)


def check_route(
    volume: BlockVolume, start: Vec3, end: Vec3, minecraft_version: str,
    *, max_nodes: int | None = None,
) -> TraversalResult:
    """立位の足元セル間の歩行と直進階段を検査する。volume は変更しない。

    通常の木・銅ドアは開閉可能とみなす。未知形状・半slab等は通行可能と推測しない。
    足場なし・頭上2セルの障害・階段方向の不一致はblocked。未知形状が探索境界に
    残り既知経路が見つからなければunsupported。未対応版はBlueprintError。
    max_nodes指定時は探索・境界状態の件数を制限し、超過はBlueprintError。
    """
    return _Walker(volume, minecraft_version, max_nodes=max_nodes).route(start, end)


def check_building(
    volume: BlockVolume, layout: BuildingLayout, minecraft_version: str,
) -> tuple[TraversalResult, ...]:
    """入口→一階到着点→次階到着点の順で検査する。layoutはvolumeと同じ座標系。

    外側のネスト変換がある場合は、呼出し元が変換済みlayoutを渡す。
    """
    points = (layout.entrance, *layout.arrivals)
    return tuple(check_routes(volume, zip(points, points[1:], strict=False), minecraft_version))


def check_routes(
    volume: BlockVolume, pairs: Iterable[tuple[Vec3, Vec3]], minecraft_version: str,
    *, max_nodes: int | None = None,
) -> Iterator[TraversalResult]:
    """不変のvolumeに対する複数経路を入力順に遅延検査する。

    check_routeと同じ判定・経路・診断順を保ち、立位判定を上限付きで再利用する。
    反復中はvolumeを変更しない。max_nodesは各経路の探索状態上限であり、
    キャッシュはその1/8件（最大16384件）まで。上限超過はBlueprintError。
    """
    walker = _Walker(volume, minecraft_version, max_nodes=max_nodes)
    for start, end in pairs:
        yield walker.route(start, end)
