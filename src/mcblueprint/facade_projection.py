"""住宅・塔の向き付き輪郭と開口・外壁rayの内部計画の正本。"""

from __future__ import annotations

import math
from dataclasses import dataclass

from mcblueprint.model import AABB, Vec3
from mcblueprint.operations.box_like import iter_box
from mcblueprint.operations.registry import build_operation
from mcblueprint.operations.shapes import disc_offsets
from mcblueprint.operations.transform import Transform

NORMALS = {"north": (0, 0, -1), "south": (0, 0, 1), "east": (1, 0, 0), "west": (-1, 0, 0)}


@dataclass(frozen=True)
class FacadeCell:
    """被覆を検査する基準セル。depthは座標原点から外向きの符号付き深度。"""

    pos: Vec3
    u: int
    v: int
    depth: int
    opening: bool = False
    segment: int = 0

    @property
    def window(self) -> tuple[int, int]:
        """面のローカル原点に固定した4×4評価窓。端は部分窓になる。"""
        return self.u // 4, self.v // 4


@dataclass(frozen=True)
class Facade:
    """一つの建物・向き付き面に属する外壁投影。"""

    element: str
    face: str
    cells: tuple[FacadeCell, ...]
    role: str = "wall_main"
    building: str | None = None

    @property
    def ray_axis(self) -> int:
        return 0 if self.face in ("north", "south") else 2

    def ray_key(self, pos: Vec3) -> tuple[int, int]:
        return pos[self.ray_axis], pos.y

    def depth_at(self, pos: Vec3) -> int:
        """最外深度の比較用。各基準セルのdepthとの差が外装の凹凸になる。"""
        normal = NORMALS[self.face]
        return pos.x * normal[0] + pos.z * normal[2]

    def wall_projection(self) -> dict[tuple[int, int], Vec3]:
        """開口以外のrayと基準セル。同じrayの輪郭段差はcells側に残す。"""
        return {self.ray_key(cell.pos): cell.pos for cell in self.cells if not cell.opening}


def _projection(box, origin, axis):
    values = [((x - origin.x) * axis.x + (z - origin.z) * axis.z)
              for x in (box.min.x, box.max.x) for z in (box.min.z, box.max.z)]
    return {"u": [min(values), max(values)],
            "v": [box.min.y - origin.y, box.max.y - origin.y]}


def plan_facades(root, body, detail, transform, params, layout):
    """ローカルの構造・開口から向き付き面と開口マスクを計画座標へ写す。"""
    rotation = Transform(transform.rows)
    frame = next(op for op in body if op["element"] == f"{root}.frame")
    frame_ops = frame["operations"]
    opening_boxes = [(opening, transform.bounds(build_operation(opening, root).bounds()))
                     for opening in detail if opening["type"] in ("window", "doorway")]
    facades = []
    for op in body:
        if "face" not in op:
            continue
        bounds = build_operation(op, root).bounds()
        bounds = transform.bounds(bounds)
        face = rotation.apply(Vec3(*NORMALS[op["face"]]))
        origin = transform.apply(build_operation(op, root).bounds().min)
        axis = rotation.apply(Vec3.from_seq(
            [1, 0, 0] if op["face"] in ("north", "south") else [0, 0, 1]
        ))
        openings = []
        for opening, box in opening_boxes:
            if opening.get("face") != op["face"]:
                continue
            openings.append({"id": opening["element"], "from": box.min.to_list(),
                             "to": box.max.to_list(), "projection": _projection(box, origin, axis)})
        facades.append({"id": op["element"], "normal": face.to_list(),
                        "origin": origin.to_list(), "uAxis": axis.to_list(),
                        "vAxis": [0, 1, 0], "from": bounds.min.to_list(),
                        "to": bounds.max.to_list(), "openings": openings})
    if not layout:
        points = sorted({(op["from"][0], op["from"][2]) for op in frame_ops
                         if op["type"] == "fill" and op["from"][0] == op["to"][0]
                         and op["from"][2] == op["to"][2]},
                        key=lambda p: math.atan2(p[1], p[0]))
        if params["shape"] == "square":
            radius = params["radius"]
            points = sorted(set(points) | {(x, z) for x in (-radius, radius)
                                           for z in (-radius, radius)},
                            key=lambda p: math.atan2(p[1], p[0]))
        for index, start in enumerate(points):
            end = points[(index + 1) % len(points)]
            dx, dz = end[0] - start[0], end[1] - start[1]
            arc = []
            if params["shape"] == "round":
                start_angle = math.atan2(start[1], start[0])
                sweep = (math.atan2(end[1], end[0]) - start_angle) % math.tau
                arc = [p for p in disc_offsets(params["radius"], "hollow", "y")
                       if (math.atan2(p.z, p.x) - start_angle) % math.tau <= sweep]
                nx, nz = start[0] + end[0], start[1] + end[1]
                scale = max(1, math.gcd(abs(nx), abs(nz)))
                normal = Vec3(nx // scale, 0, nz // scale)
            elif dx:
                normal = Vec3(0, 0, 1 if (start[1] + end[1]) // 2 > 0 else -1)
            else:
                normal = Vec3(1 if (start[0] + end[0]) // 2 > 0 else -1, 0, 0)
            for level in range(params["floors"]):
                lo = Vec3(start[0], level * params["floor_height"] + 1, start[1])
                hi = Vec3(end[0], min((level + 1) * params["floor_height"],
                                      params["height"]), end[1])
                local_box = AABB.of(lo, hi)
                if arc:
                    local_box = AABB.from_points([
                        Vec3(p.x, y, p.z) for p in arc for y in (lo.y, hi.y)
                    ])
                box = transform.bounds(local_box)
                openings = []
                for opening, opening_box in opening_boxes:
                    center = Vec3((opening_box.min.x + opening_box.max.x) // 2,
                                  (opening_box.min.y + opening_box.max.y) // 2,
                                  (opening_box.min.z + opening_box.max.z) // 2)
                    segment_center = transform.apply(Vec3((start[0] + end[0]) // 2,
                                                           lo.y, (start[1] + end[1]) // 2))
                    if (center.x - segment_center.x) ** 2 + (center.z - segment_center.z) ** 2 < 16:
                        origin = transform.apply(lo)
                        u_axis = rotation.apply(Vec3(dx, 0, dz))
                        openings.append({"id": opening["element"],
                                         "from": opening_box.min.to_list(),
                                         "to": opening_box.max.to_list(),
                                         "projection": _projection(opening_box, origin, u_axis)})
                facades.append({"id": f"{root}.facade.segment#{index + 1}.floor#{level + 1}",
                                "normal": rotation.apply(normal).to_list(),
                                "origin": transform.apply(lo).to_list(),
                                "uAxis": rotation.apply(Vec3(dx, 0, dz)).to_list(),
                                "vAxis": [0, 1, 0],
                                "from": box.min.to_list(), "to": box.max.to_list(),
                                "openings": openings})
    return facades


def _box(item):
    return AABB.of(Vec3.from_seq(item["from"]), Vec3.from_seq(item["to"]))


def _project_rays(points, face, *, keep_steps=False):
    direction = NORMALS[face]
    axis = 0 if direction[2] else 2
    rays = {}
    for pos in points:
        key = pos if keep_steps else (pos[axis], pos.y)
        depth = pos.x * direction[0] + pos.z * direction[2]
        if key not in rays or depth > rays[key][0]:
            rays[key] = depth, pos
    return rays


def plan_facade_runs(params, face):
    """ローカル輪郭の最外rayを水平区間へまとめ、u昇順で返す。

    各区間は(start, end, fixed)で両端を含む。fixedは符号付き深度でなく
    面に直交する座標値。開口・柱間では分割せず、Detailの配置順を保持する。
    """
    axis = 0 if face in ("north", "south") else 2
    if "radius" not in params:
        w, d = params["width"], params["depth"]
        fixed = {"north": 0, "south": d - 1, "west": 0, "east": w - 1}[face]
        return [(0, (w if axis == 0 else d) - 1, fixed)]
    radius = params["radius"]
    if params["shape"] == "square":
        return [(-radius, radius, -radius if face in ("north", "west") else radius)]
    rays = _project_rays(disc_offsets(radius, "hollow", "y"), face)
    runs = []
    for (u, _), (_, point) in sorted(rays.items()):
        fixed = point[2 if axis == 0 else 0]
        if runs and runs[-1][2] == fixed and runs[-1][1] == u - 1:
            runs[-1] = (runs[-1][0], u, fixed)
        else:
            runs.append((u, u, fixed))
    return runs


def project_facades(planning, *, offset=(0, 0, 0)) -> tuple[Facade, ...]:
    """計画座標の面を被覆セルへ展開する。offsetは円塔のSite移動に使う。

    面の原点・軸・開口マスクを保持し、円塔では同じray上の輪郭段差も残す。
    入力を変更せず、不正なメタデータはKeyError/TypeError/ValueErrorで通知する。
    """
    params = planning.get("params", {})
    rotation = Transform.rotation(params.get("angle", 0), Vec3(0, 0, 0))
    reverse = Transform.rotation(-params.get("angle", 0), Vec3(0, 0, 0))
    transform = rotation.then(Transform.translation(Vec3.from_seq(offset)))
    contour = None
    if params.get("shape") == "round":
        contour = [transform.apply(p) for p in disc_offsets(params["radius"], "hollow", "y")]
    facades = []
    used = set()
    for item in planning["facades"]:
        normal = Vec3.from_seq(item["normal"])
        origin = Vec3.from_seq(item["origin"])
        axis = Vec3.from_seq(item["uAxis"])
        local_normal = reverse.apply(normal)
        face = rotation.apply_direction(max(
            NORMALS,
            key=lambda name: sum(
                a * b for a, b in zip(NORMALS[name], local_normal.to_list(), strict=True)
            ),
        ))
        direction = Vec3.from_seq(NORMALS[face])
        bounds = _box(item)
        points = list(iter_box(bounds))
        if contour is not None:
            points = [
                Vec3(p.x, y, p.z)
                for p in contour
                if bounds.min.x <= p.x <= bounds.max.x and bounds.min.z <= p.z <= bounds.max.z
                for y in range(bounds.min.y, bounds.max.y + 1)
            ]
        else:
            points = [
                p for p in points if (p - origin).x * normal.x + (p - origin).z * normal.z == 0
            ]
        # roundの輪郭セルは同じrayに並ぶ段差も被覆対象に残す。
        rays = _project_rays(points, face, keep_steps=contour is not None)
        projected = []
        for depth, pos in rays.values():
            if (face, pos) in used:
                continue
            used.add((face, pos))
            u = (pos - origin).x * axis.x + (pos - origin).z * axis.z
            v = pos.y - origin.y
            opening = False
            for entry in item["openings"]:
                if "projection" in entry:
                    mask = entry["projection"]
                    opening |= mask["u"][0] <= u <= mask["u"][1] and (
                        mask["v"][0] <= v <= mask["v"][1]
                    )
                else:
                    opening |= _box(entry).contains(pos)
            # 評価窓の幅はuAxisの長さによらず1ブロック単位にする。
            local_u = ((pos.x - origin.x) * (1 if axis.x > 0 else -1) if direction.z
                       else (pos.z - origin.z) * (1 if axis.z > 0 else -1))
            projected.append(FacadeCell(pos, local_u, v, depth, opening, segment=len(facades)))
        facades.append(Facade(item["id"], face, tuple(projected), building=planning.get("element")))
    return tuple(facades)
