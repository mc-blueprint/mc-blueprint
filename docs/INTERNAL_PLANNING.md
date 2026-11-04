# 内部計画 v2

Site/Massing/Detail と Blueprint の `metadata.planning` は `formatVersion: 2` を使う。
BuildSpec、Blueprint、scene、edits の外部形式は v1 のまま。
旧 checkpoint は版または engine 不一致で拒否し、BuildSpec から再生成する。移行は行わない。

```text
site#1
  parcels[]: parcel#1 …
    groups[]: group#1 …
      buildings[]: house#1 / tower#1 / building#1 …
        facades[]: <棟ID>.facade.<向き・区間>
```

階層ノードは `element` を持つ。棟は `archetype`、編集可能な `params`、
`offset`、`entrance`、`arrivals`、`columns`、`beams`、`facades`、`decorations`、
`operations` を持つ。`operations` は回転・平行移動前の構造形状、`offset` と
`params.angle` はその世界変換。期待柱・梁・外壁・入口・到着点は世界座標。
外壁の向き・投影・開口は既存 `facade_projection` を共有する。
平坦な集計計画は併記せず、`plan_contract.plan_buildings` で棟を走査する。
単独住宅・塔は単一区画・単一建物群の一棟として収め、Operation・座標・抽選を維持する。

ID の番号は種類ごとの安定した候補順に付ける。同じ入力・seed では再現し、
領域の処理順や生成後のセル走査から採番しない。下位 ID は棟 ID の接尾辞とする。
scene のセル出典は棟へ解決し、棟の `params` を編集対象として提示する。
セルを持たない棟の親出典も `_spec_sources` が追加する。
edits は scene の許可一覧と現在の計画の両方で ID とパラメータを検証する。

## 接続と接合

`connections` は `nodes` と `edges` を持つ。

- node: `{id, element, position: [x,y,z]}`。`element` は階層中の要素を参照する。
- edge: `{id, kind, from, to}`。`kind` は `road/cave/walk`、両端は node ID。
- 単独棟の入口・階到着点は `<棟ID>.walk#N`、経路は `<棟ID>.route#N`。
  棟の入口と全到着点はグラフ上で接続している必要がある。

Critic は期待構造・外壁を棟ごとに変換し、各面・柱の不合格を保持する。
一棟の欠損を他棟の平均で合格にしない。歩行は接続辺ごとに完成 spool から検査する。
棟が必須でない archetype は `terrain` と `cave`。この二種類の棟なし計画は建物指標の n/a を許す。
house/tower/castle/town/city/underground_city の棟なし計画は拒否する。
建物課題の棟・期待柱・外壁・入口接続の欠落は計画エラー。

`junctions` は後続 #239 へ渡す内部接合レコードの配列。

```json
{
  "element": "junction#1",
  "owner": "junction#1",
  "parts": [
    {"element": "house#1", "shape": "rectangle"},
    {"element": "tower#1", "shape": "round"}
  ],
  "contacts": [
    {"first": "house#1", "second": "tower#1", "from": [10, 1, 10], "to": [10, 4, 12]}
  ],
  "passages": [{"from": "house#1.walk#1", "to": "tower#1.walk#1"}],
  "roofPolicy": "envelope"
}
```

`parts.element` は安定した棟・部材 ID、`shape` は `rectangle/round/square`。
部材 ID は計画中の柱・梁・外壁、または棟の Operation に実在する element を参照する。
`contacts.first/second` も同じ実在 ID を使う。存在しない接尾辞を前方一致で受理しない。
`contacts` は接触する二部材と両端を含む世界座標区間、`passages` は接続節点の対。
接合帯の予約 owner は接合 ID と一致させる。`roofPolicy` は既存 `join` または
屋根包絡の `envelope`。`materialize_plan` が棟別配色後に `building_junction` へ写す。
施工とCriticは同じ接合幾何を使い、内部接触面だけを除外する。
低棟の上の高棟外壁、接合の支持・梁・屋根は期待形状として検査する。
接合 passage も完成spoolの歩行検査に含める。
具体的な入力と制限は [OPERATIONS.md](OPERATIONS.md#building_junction) を参照。

## 配色・地形と後続 planner

複数棟は `materialize_plan` が既存 `materialize` へ世界 seed・棟 ID・建物種別を渡し、
配色選択元・関係診断・面積上限を棟ごとに保持する。Palette 名も棟ごとに分離する。
Operation は敷地分を含めて入力順を維持し、固定役割は棟別に Critic へ渡す。
配色パックの語彙・役割・条件を追加しない。単独棟は従来の抽選 seed を維持する。
snapshot・`groundHeight` は既存 Site の契約をそのまま使用する。

castle/town/city/terrain/cave/underground_city の特徴語彙は Resolve と雛形で扱うが、
各 planner は未実装であり Site/Massing 段で明示的に拒否する。
複数棟検証は既存住宅・塔の合成固定ケースであり、街並み生成の実装ではない。
