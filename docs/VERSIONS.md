# 対応 Minecraft バージョン

`mcblueprint versions` で同梱ブロックデータの一覧を表示できる。`minecraftVersion` にはこの一覧のバージョンだけを指定できる（それ以外はエラー）。Blueprint を書き換えずに別のバージョンで検証・出力するには `--minecraft-version` を使う。

```bash
mcblueprint versions
mcblueprint validate examples/house.json --minecraft-version 26.3
mcblueprint build examples/house.json --minecraft-version 26.3
```

| バージョン | DataVersion | ブロック数 | 備考 |
|---|---|---|---|
| `1.21.11` | 4671 | 1166 | 2025-12 リリース。1.21 系の最終 |
| `26.1.2` | 4790 | 1168 | 2026-04。年ベース採番（`26.x`）の最初のライン |
| `26.2` | 4903 | 1196 | 2026-06 |
| `26.3` | 5023 | 1286 | 2026-09。現時点の最新 |

DataVersion は `.schem` / `.litematic` に書き込まれる値で、`import` 時にファイルのバージョンを判定するのにも使う。同梱データにない DataVersion のファイルは、それより新しくない最も近いバージョン（どれより古ければ最も古いバージョン）で読み込み、その旨を表示する。

## バージョン間の差分

ブロックの削除はなく、新しいバージョンは古いバージョンのブロックをすべて含む（`tests/test_blockdata.py` で検証）。古いバージョン向けに書いた Blueprint は、そのまま新しいバージョンでも検証が通る。

### 1.21.11 → 26.1.2

- 追加: `golden_dandelion`, `potted_golden_dandelion`
- 変更: 看板（`*_sign`, `*_hanging_sign`）と旗（`*_banner`）の `rotation` のデフォルトが `0` から `8` に変わった（`rotation` を書かないと向きが変わる。明示すれば影響なし）

### 26.1.2 → 26.2

- 追加: 辰砂（`cinnabar`）と硫黄（`sulfur`）系のブロック 28 種（`*_bricks`, `polished_*`, それぞれの `_slab` / `_stairs` / `_wall`、`sulfur_spike`, `potent_sulfur`）

### 26.2 → 26.3

- 追加: 羊毛とコンクリートの `_slab` / `_stairs`（16 色 × 2 × 2 = 64 種）、ポプラ（`poplar_*`: 原木・板材・ドア・フェンス・看板・葉（`orange` / `red` / `yellow_poplar_leaves`）など）、`red_shrub`, `shelf_mushroom`, `straw_bed`, `poplar_shelf`

## バージョンの追加

[DEVELOPMENT.md](DEVELOPMENT.md#ブロックデータの更新) の手順で mc-blueprint-blockdata の Release を検証して同梱し、`src/mcblueprint/data/lock.json` を更新する。版一覧は同梱JSONとlockから導く。追加したら本書の表と差分を更新する。

## blockdata v1 への切替

提供元 `mc-blueprint/mc-blueprint-blockdata` の `v1.0.0`（commit `88ac6cb2fa90a99c65edacd1713904649c17d1a6`）を同梱した。旧同梱データと比べ、全4版でブロックID・許可プロパティ（値の順序を含む）・既定値・DataVersionは一致する。状態数は各ブロックの許可プロパティの直積の合計（プロパティなしは1状態）。

| 版 | ブロック数（旧→新） | 状態数（旧→新） | 追加/削除状態 | 既定値変更ブロック数 |
|---|---|---|---|---|
| `1.21.11` | 1166 → 1166 | 29671 → 29671 | 0 / 0 | 0 |
| `26.1.2` | 1168 → 1168 | 29873 → 29873 | 0 / 0 | 0 |
| `26.2` | 1196 → 1196 | 32366 → 32366 | 0 / 0 | 0 |
| `26.3` | 1286 → 1286 | 35723 → 35723 | 0 / 0 | 0 |
