# アーキテクチャ

mc-blueprint の内部構成と各モジュールの責務を定義する。実装は本書に従う。
Blueprint JSON の仕様は [FORMAT.md](FORMAT.md)、Operation の仕様は [OPERATIONS.md](OPERATIONS.md) を参照。

## 1. 処理フロー

```text
blueprints/house.json
        │
        ▼
   Loader        JSON → Blueprint モデル（ブロック文字列の正規化、Operation オブジェクト化）
        │
        ▼
   Validator     JSON Schema 検証 + 意味検証（ブロック ID、Palette 参照、上限 …）
        │
        ▼
   Generator     Operation を順に実行し BlockVolume を組み立てる
        │
        ▼
   BlockVolume   (x, y, z) → BlockState の疎な集合
        │
        ▼
   Exporter      Sponge Schematic v2 (.schem) へ書き出し
        │
        ▼
output/house.schem
```

AI エージェントは Blueprint JSON を書くところまでを担当し、Minecraft 固有のデータ形式は Validator / Generator / Exporter が担当する。この境界を崩さない。

## 2. モジュール構成

```text
src/mcblueprint/
├─ __init__.py          __version__
├─ __main__.py          python -m mcblueprint
├─ cli.py               argparse。validate / build
├─ errors.py            BlueprintError, ValidationError
├─ loader.py            JSON 読み込み → Blueprint
├─ validator.py         Schema 検証 + 意味検証
├─ generator.py         Blueprint → BlockVolume
├─ volume.py            BlockVolume
├─ blockdata.py         同梱ブロックデータの読み込み
├─ components.py        部品ファイルの探索・読み込み・循環検出
├─ support.py           支持チェック
├─ preview.py           PNG プレビュー（任意依存 Pillow）
├─ importers.py         .schem / .litematic の読み込みと Blueprint への変換
├─ diffing.py           2 つの BlockVolume の比較
├─ model/
│  ├─ vec.py            Vec3, AABB
│  ├─ block.py          BlockState
│  ├─ palette.py        Palette, PaletteEntry
│  └─ blueprint.py      Blueprint
├─ operations/
│  ├─ base.py           Operation, BlockSpec, ExecutionContext
│  ├─ transform.py      Transform（平行移動・反転・回転とブロック状態の変換）
│  ├─ nested.py         mirror / repeat / translate / rotate の共通基底
│  ├─ registry.py       type 文字列 → Operation クラス
│  ├─ set.py fill.py box.py wall.py floor.py line.py
│  ├─ circle.py cylinder.py sphere.py
│  ├─ mirror.py repeat.py translate.py rotate.py
│  ├─ replace.py copy.py
│  ├─ composite.py      高レベル Operation の基底（基本 Operation の列へ展開）
│  ├─ stairs.py spiral_stairs.py roof.py pillar.py doorway.py window.py arch.py room.py tower.py bridge.py gate.py garden.py path.py
│  └─ component.py      部品の配置
├─ exporters/
│  ├─ base.py           Exporter
│  ├─ schem.py          Sponge Schematic v2
│  └─ litematic.py      Litematica
├─ schema/
│  └─ blueprint.schema.json   パッケージ同梱の JSON Schema（リポジトリの schema/ と同一）
└─ data/
   ├─ lock.json         同梱データの出所・版・commit・SHA-256
   └─ blockdata/        blockdata-<minecraftVersion>.json と固定版スキーマ
```

依存の向き: `cli` → `loader` / `validator` / `generator` / `exporters` → `operations` / `volume` / `model` / `blockdata`。下位モジュールは上位を import しない。

## 3. データモデル（`model/`）

### Vec3

不変の整数 3 次元ベクトル。`+`, `-`, スカラー倍、要素ごとの `min` / `max`。`dict` のキーにできる（hashable）。

### AABB

両端を含む直方体 `min: Vec3`, `max: Vec3`。`size`（各辺の長さ）、`volume`、`union(other)`、`contains(pos)`。

### BlockState

```python
@dataclass(frozen=True)
class BlockState:
    id: str                       # "minecraft:oak_stairs"（名前空間付き）
    properties: tuple[tuple[str, str], ...]   # 名前順にソート済み
```

- `BlockState.parse(text)`: [FORMAT.md §4](FORMAT.md#4-ブロック指定) の文法で解析し正規化する。文法違反は `BlueprintError`。
- `to_string()`: 正規化済み文字列。
- `with_defaults(block_data)`: 未指定プロパティをデフォルト値で補完した `BlockState` を返す（Exporter が使う）。
- `with_property(name, value)`: プロパティを差し替えた新しい `BlockState`（`mirror` が使う）。

### Palette / PaletteEntry

`Palette(name, entries)`、`PaletteEntry(block: BlockState, weight: float)`。`Palette.choose(rng)` が重み付きで 1 つ選ぶ（`random.Random.choices` を使用）。

### Blueprint

トップレベル項目を保持する dataclass。`operations: list[Operation]`、`palettes: dict[str, Palette]`。既定値の補完（`seed = 0`, `origin = Vec3(0, 0, 0)`）はここで行う。

## 4. Loader（`loader.py`）

`load_blueprint(path) -> Blueprint`

1. ファイルを UTF-8 で読み、`json.loads` する。構文エラー・ファイル未存在は `BlueprintError`。
2. Palette、Operation（`registry.build_operation`）をオブジェクト化する。

Loader は Schema 検証済みのデータを前提とし、それでも見つかった形状の問題は `BlueprintError` で報告する。CLI は `validator.validate()` → `load_blueprint()` の順に呼ぶ（Validator が内部で Loader を使って `bounds()` を求めるため、逆方向の依存は持たない）。

`load_blueprint_dict(data: dict) -> Blueprint` も提供し、テストや将来の API 利用から使えるようにする。

## 5. Operation 実行モデル（`operations/base.py`）

```python
class Operation(ABC):
    type: ClassVar[str]

    @classmethod
    def from_dict(cls, data: dict, path: str) -> Self: ...
    def bounds(self) -> AABB: ...
    def apply(self, ctx: ExecutionContext) -> None: ...
```

- `path` は `operations[3].operations[0]` のような JSON パスで、エラー報告に使う。
- `bounds()` は実行せずに範囲を返す。Validator の `size` / 最大寸法チェックと、将来の `inspect` が使う。

### BlockSpec

`block: BlockState | None` と `palette: str | None` のどちらか一方を持つ。配置系 Operation は `from_dict` で共通項目から生成する。

### Transform（`operations/transform.py`）

座標変換。符号付き置換行列とオフセットの合成 `p' = rows · p + offset` で表し、平行移動・軸反転・鉛直軸まわりの 90° 回転とそれらの合成を扱う。

```python
@dataclass(frozen=True)
class Transform:
    rows: Matrix        # 3×3、各行に ±1 が 1 つ
    offset: Vec3

    @classmethod identity() / translation(offset) / mirror(axis, at) / rotation(angle, center)
    def apply(self, pos: Vec3) -> Vec3
    def apply_state(self, state: BlockState) -> BlockState   # facing / axis / 接続 / half / type / shape / hinge
    def then(self, outer: Transform) -> Transform            # self を適用してから outer を適用する合成
    def bounds(self, box: AABB) -> AABB
```

- `mirror(axis, at)` は該当軸の対角成分を −1、オフセットを `2 × at`（`.5` でも整数）にする。
- `rotation(angle, center)` は上から見て時計回り。90° は `(x, z) → (−z, x)` で、`offset = center − rows · center` により中心を固定する。
- 合成順序は「内側の Operation の座標 → 内側の Transform → 外側の Transform」。`repeat` の中の `mirror` では、鏡面の位置も `repeat` の平行移動を受ける。
- `apply_state` は方向を持つプロパティを行列で写す（`docs/OPERATIONS.md` の「座標変換とブロック状態」）。`shape` / `hinge` の左右入れ替えは行列式が負（鏡像）のときだけ行う。

### ExecutionContext

```python
class ExecutionContext:
    volume: BlockVolume
    rng: random.Random
    palettes: dict[str, Palette]
    transform: Transform
    depth: int

    def place(self, pos: Vec3, spec: BlockSpec) -> None
    def child(self, transform: Transform) -> ExecutionContext
```

- `place`: `spec` が Palette なら `rng` で 1 つ選び、`transform.apply(pos)` と `transform.apply_state(state)` を適用して `volume.set` する。配置系 Operation はセルを列挙して `place` を呼ぶだけにする。
- `child`: `transform` を合成し `depth + 1` にした子コンテキスト。`volume` / `rng` / `palettes` は共有する。
- `rng` は Blueprint 全体で 1 本。Palette 解決時のみ消費する。

### CompositeOperation（`operations/composite.py`）

高レベル建築 Operation の基底。`expand()` が基本 Operation の JSON（dict）の列を返し、registry で通常どおり組み立てる。`bounds()` は展開結果の合成、`apply()` は展開結果を同じコンテキストで順に実行する（ネスト深さは増えない）。展開結果の JSON パスは `operations[3]<stairs>` のように元の Operation を示す。

### ComponentOperation（`operations/component.py`, `components.py`）

`components/<name>.json` を読み、その Operation 群を回転 → 平行移動の Transform と部品ローカルの Palette（`ExecutionContext.child(transform, palettes=...)` で呼び出し元の Palette を ChainMap で覆う）で実行する。探索パスは `components.component_search_paths()` の contextvar で与え、`load_blueprint(path)` と CLI が「Blueprint の階層の `components/` → cwd の `components/`」を設定する。循環参照は読み込み中の名前のスタック（contextvar）で検出する。Validator は部品ファイルに対して部品用 Schema（Blueprint Schema から `minecraftVersion` / `origin` / `size` / `seed` を除いたもの）と意味検証を行い、`operations[3]<name>.…` のパスで報告する。

### join_buildings（`operations/join_buildings.py`）

既存building二棟の面・床高・階高・階数・開口と入口/階段の干渉を構築時に検証し、共有面の配置責務を一か所へ置く。BuildingOperationは`apply_without_face`で実共有面への全placementを抑止し、接続操作が共有セルをwall/先頭floor材/airで一度ずつ書く。ネスト深さは増やさず、外側Transformを通常どおり合成する。不変のJoinLayoutは両BuildingLayout、共有面、floor_y、開口AABBを公開し、外側変換は含まない。異階数は未対応。joinRoof=trueでは同条件の全幅妻面接触だけを受け付け、各buildingのroof生成を止め、共通のRoofOperationと外周妻壁だけを生成する。外周妻壁は既存roofの段形状を再利用して所有元wall材を保つ。入力契約は[OPERATIONS.md](OPERATIONS.md#join_buildings)。

### registry

`OPERATIONS: dict[str, type[Operation]]` と `build_operation(data, path) -> Operation`。未知の `type` は Schema 検証で弾かれる前提だが、registry でも `BlueprintError` にする。

## 6. Generator（`generator.py`）

```python
def generate(blueprint: Blueprint, *, seed: int | None = None) -> BlockVolume
```

`ExecutionContext` を `Transform.identity()`、`depth = 1` で初期化し、`blueprint.operations` を順に `apply` する。`seed` 引数は CLI の `--seed` による上書き用。

## 7. BlockVolume（`volume.py`）

```python
class BlockVolume:
    def set(self, pos: Vec3, state: BlockState) -> None
    def get(self, pos: Vec3) -> BlockState | None
    def bounds(self) -> AABB | None          # 空なら None
    def count_by_state(self) -> dict[str, int]
    def __len__(self) -> int
    @property
    def palette(self) -> list[BlockState]
```

- 内部は `dict[Vec3, int]`（値は `palette` の index）。同じ `BlockState` は同じ index を共有する。
- `set` は上書き。未設定の位置は `get` で `None`。

## 8. Validator（`validator.py`）

```python
def validate_schema(data: dict) -> list[ValidationError]
def validate(data: dict, *, max_dimension: int = 1024) -> list[ValidationError]
def format_errors(errors: list[ValidationError]) -> str
```

### 手順

1. **Schema 検証**: `jsonschema.Draft202012Validator(schema).iter_errors(data)` で全件収集する。`error.absolute_path` を `operations[4].radius` 形式に整形する。
2. **意味検証**（Schema 検証にエラーがなかった場合のみ）:

| 検証項目 | 内容 |
|---|---|
| `minecraftVersion` | `blockdata.supported_versions()` に含まれる |
| ブロック指定 | 文法、ID の存在、プロパティ名・値の妥当性（`operations` 内と `palettes` 内の両方） |
| `block` / `palette` の排他 | 配置系 Operation で必ず一方のみ |
| Palette 参照 | `palette` で指定した名前が `palettes` に存在する |
| `wall` / `floor` | `from[1] == to[1]` |
| ネスト深さ | ≤ 8 |
| `size` | 全 Operation の `bounds()` の合成が `size` 以下 |
| 最大寸法 | `bounds()` 合成の各辺 ≤ `max_dimension`、体積 ≤ 100,000,000 |

### ValidationError

```python
@dataclass(frozen=True)
class ValidationError:
    path: str              # "operations[4]" / "palettes.stone_wall[1].block" / "" (トップレベル)
    message: str           # 英語。1 文で原因を述べる
    op_type: str | None    # Operation に関するエラーなら type
    value: object | None   # 問題の値
```

### 出力形式

AI が読んで修正しやすいよう、1 エラーを 1 ブロックで表示する。

```text
ERROR operations[4] (cylinder)
  radius must be greater than 0.
  Current value: -5

ERROR palettes.stone_wall[1].block
  Unknown block id.
  Current value: minecraft:stone_brick

2 errors found.
```

- エラーがなければ `Blueprint is valid.`
- 単数形は `1 error found.`

## 8.5 支持チェック（`support.py`）

Validator が扱うのは JSON の妥当性であり、「そのブロックがその場所で物理的に成立するか」は扱わない。代わりに、生成後の `BlockVolume` を走査して代表的な付着ブロックの支持を検査し、**警告**として報告する。

```python
def check_support(volume: BlockVolume) -> list[SupportWarning]
def format_warnings(warnings: list[SupportWarning]) -> str
```

| 対象ブロック | 条件 |
|---|---|
| 吊りランタン（`hanging=true`） | 真上のブロックの下面が支持面（完全ブロック、下付きハーフ、通常向きの階段、鎖、鉄格子、フェンス、壁） |
| 床置きランタン・松明・ろうそく・感圧板 | 真下のブロックの上面が支持面（完全ブロック、上付きハーフ、逆さ階段、閉じた上付きトラップドア、フェンス、壁、8 層の雪） |
| 壁松明・壁看板・壁旗・はしご | `facing` の反対側のブロックの側面が支持面（完全ブロック、または `facing` が一致する階段） |
| ボタン・レバー | `face` に応じて上記の床・天井・壁の条件 |
| ドア | 下段の下が支持面で、上段／下段が同じ `facing` / `hinge` で対になっている |
| ベッド | `facing` 方向に `part` の対（foot / head）がある |
| カーペット・雪 | 真下が空気でない |
| 草花・苗木 | 真下が土系（`grass_block`, `dirt`, `podzol`, `moss_block`, `farmland`, `mud` など） |
| 作物 | 真下が `farmland`（`nether_wart` は `soul_sand`） |

- 「完全ブロック」の判定はブロック名のパターン（`_slab`, `_stairs`, `_pane`, `_door`, `_carpet`, … と液体・植物・装飾の一覧）で「支持しないもの」を列挙し、それ以外を完全ブロックとみなす。Mojang のデータには形状情報がないため、この分類は近似である。
- 未設定セルは `minecraft:air` として扱い、構造の bounds の外（貼り付け先のワールド）は不明として警告しない。
- CLI では `validate` と `build` が生成後にこのチェックを行い、警告があれば表示する。終了コードは `0` のままだが、`--strict` を付けると `1` になり、`build` は書き出さない。

```text
WARNING [3, 5, 2] minecraft:lantern[hanging=true]
  Needs a solid block above; found minecraft:oak_slab[type=top].

1 warning found.
```

## 9. Exporter（`exporters/`）

```python
class Exporter(ABC):
    format: ClassVar[str]        # "schem"
    extension: ClassVar[str]     # ".schem"
    def export(self, volume: BlockVolume, blueprint: Blueprint, out_path: Path) -> None
```

### Sponge Schematic v2（`schem.py`）

`nbtlib` で書き出す。ファイル全体を gzip 圧縮する。

| タグ | 型 | 値 |
|---|---|---|
| ルート | Compound（名前 `Schematic`） | |
| `Version` | Int | `2` |
| `DataVersion` | Int | `blockdata.data_version(minecraftVersion)` |
| `Width` / `Height` / `Length` | Short | バウンディングボックスの X / Y / Z 方向の長さ |
| `Offset` | IntArray[3] | `bounds.min − origin` |
| `Palette` | Compound | 完全な状態文字列 → Int（index） |
| `PaletteMax` | Int | Palette のエントリ数 |
| `BlockData` | ByteArray | 各セルの Palette index を varint で連結 |
| `Metadata` | Compound | `WEOffsetX` / `WEOffsetY` / `WEOffsetZ`（Int、`Offset` と同値） |

- `BlockData` のセル順序は `index = x + z × Width + y × Width × Length`（`x, y, z` は `bounds.min` からの相対）。
- `BlockVolume` に存在しないセルは `minecraft:air` にする（未設定セルがある場合のみ Palette に `minecraft:air` を追加する）。
- Palette の状態文字列は `BlockState.with_defaults()` で全プロパティを補完したもの。
- varint: 7 bit ごとに下位から出力し、続きがあれば最上位 bit を立てる。
- 空の `BlockVolume`（Operation が 1 つもセルを生成しなかった場合）はエラーにする。

### Litematica（`litematic.py`）

Litematica 用の `.litematic`。ルート Compound は無名、gzip 圧縮。1 リージョン（`Main`）のみで、エンティティ・タイルエンティティは含めない。

| タグ | 値 |
|---|---|
| `MinecraftDataVersion` | `blockdata.data_version(minecraftVersion)` |
| `Version` | `6` |
| `Metadata` | `Name` / `Author`（省略時 `mc-blueprint`）/ `Description` / `RegionCount: 1` / `TotalVolume` / `TotalBlocks`（air 以外）/ `EnclosingSize` / `TimeCreated` / `TimeModified` |
| `Regions.Main.Position` | `bounds.min − origin` |
| `Regions.Main.Size` | バウンディングボックスの各辺 |
| `Regions.Main.BlockStatePalette` | `{Name, Properties}` の List。index 0 は必ず `minecraft:air` |
| `Regions.Main.BlockStates` | LongArray。`bits = max(2, ceil(log2(パレット数)))` ビットのエントリを long 境界をまたいで詰める（Litematica の `LitematicaBitArray` 方式） |
| `TileEntities` / `Entities` / `PendingBlockTicks` / `PendingFluidTicks` | 空の List |

- セル順序は `.schem` と同じ `index = x + z × Width + y × Width × Length`。
- 未設定セルは index 0（air）。

### 検証方法

テストでは書き出した `.schem` を `nbtlib.load` で読み戻し、`Width` / `Height` / `Length` / `Offset` / `Palette` / `BlockData` を検証する。手動確認では WorldEdit の `//schem load` → `//paste` を使う。

## 9.5 Preview（`preview.py`）

生成した `BlockVolume` を PNG に描画する。任意依存 `Pillow`（`pip install -e ".[preview]"`）が必要で、未導入なら `BlueprintError` で案内する。

| ビュー | 内容 |
|---|---|
| `top` | 平面図。各 (x, z) の最上段のブロック。高いほど明るい |
| `north` / `south` / `east` / `west` | 立面図。その方向から見て最も手前のブロック |
| `isometric` | 2:1 のピクセルアイソメトリック（南東上空から）。上面・+x 面・+z 面を塗り分ける。通常 stairs と slab は状態に応じた半ブロック形状、それ以外は立方体近似。隣接形状で覆われる面は描かず、`scale` 6 以上では輪郭線 |
| 断面（`--layer Y` / `--layers A..B` / `--layers all`） | 高さ y の水平断面（`render_layer`）。その段のブロックを描き、1 つ下の段で覆われていないブロックを薄く重ねる。出力は `<stem>-y<NN>.png`（0 埋めで並び順を保つ） |

- 色は Blueprint の Minecraft 版の blockdata を `load_block_data()` で取得し、面ごとの OKLab 50 パーセンタイルを sRGB に変換する。平面図・水平断面は top、立面図は side、等角図は上面に top、南面・東面に side を使う。状態による色の回転・変化は扱わず、blockdata の既定状態の向きを使う。
- 着色は plains を既定とし、fixed はブロックIDの着色値を使う。`tintedOklab` の素材色と着色値をそれぞれ線形 sRGB に変換して乗算する。`untintedOklab` が併存するときは、面積比が提供されないため未着色層と等重みで合成する。`tint: none` または層別色がない面は `oklab` を未着色で使う。変換は [OKLab 作者の公開式](https://bottosson.github.io/posts/oklab/)と標準 sRGB 伝達関数を用い、依存は追加しない。
- air・cave_air・void_air は描かない。要求面がなければ同ブロックの最初の既存面を使う。全方面がない水系は plains の water 着色値、それ以外は同版・同材質の各ブロックの面色を線形 sRGB 成分ごとに中央値集約して補完する。全方面欠損の非空気IDを `uncoloured_blocks()` が返し、CLI は補完方法を WARNING に表示する。透明ブロックや特殊描画の正確な外観・染色差は再現できない。
- 全方面欠損は 1.21.11 / 26.1.2 で各200件、26.2 / 26.3 で各136件（各版とも空気3件、水系2件を含む）。空気を除く197 / 197 / 133 / 133件は着色値または材質中央値で描ける。要求面だけが欠ける場合の別面補完を含め、全版・全ブロックの色解決をテストする。
- Python API の `Options.version` と `block_color(..., version=...)` は既定で 1.21.11。CLI は `--minecraft-version` の上書きも含めた入力版を渡す。未知のIDは灰色で隠さず BlueprintError。ローダーは版一覧取得と実際の情報取得で同じJSONキャッシュを共有する。
- 形状対応は `minecraft:*_stairs` の `facing`（4 方位）・`half`（`bottom` / `top`）・`shape`（`straight` / `inner_left` / `inner_right` / `outer_left` / `outer_right`）と `minecraft:*_slab` の `type`（`bottom` / `top` / `double`）。省略時はそれぞれ `north` / `bottom` / `straight`、`bottom` を使う。隣接状態から stairs の角を再計算せず、入力の `shape` をそのまま描く。
- アイソメトリックは半ブロック占有セルから露出面を求め、同じブロックの同一平面を結合して内部線を除く。未対応形状・不明な形状状態値は立方体近似。独自名前空間も形状判定単体では立方体だが、同梱 blockdata にないIDの色解決はエラーとなる。テクスチャ・光伝播・透過・任意モデルには対応せず、色と形状の精度は別である。対応範囲と固定 fixture の比較方法は [PREVIEW_SHAPES.md](PREVIEW_SHAPES.md)。
- 陰影: 立面図は上面が露出するブロックの上辺に明るい線、隣が空いている（または奥にある）辺に暗い線。平面図と断面は右（+x）と手前（+z）の隣が低い・無い辺に暗い線。
- `--grid N`: N ブロックごとの濃い罫線と、余白（14 px）に世界座標のラベル。Blueprint の `origin` を赤い × で示す（立面図は origin の列と高さ、isometric は上面の輪郭）。北・東の立面図は左右が反転しているので、ラベルの数値は減る向きに並ぶ。
- `--from X Y Z --to X Y Z` / `Options.region` は元の Blueprint 座標で両端を含む矩形範囲を選ぶ。全生成後に元範囲との交差だけを描き、元座標を移動しない。範囲外の遮蔽・ghost を取り除いた切口で、画像寸法は同じ設定の全体図を超えない。選択 Y 外の layer、完全範囲外、可視ブロックのない選択はエラー。範囲画像は `<stem>-region-<view>.png` / `-region-y<NN>.png` とし、PNG 情報と `<stem>-region.json` に元入力参照・元範囲・要求/実描画範囲・view/layer を保存する。API・切口・比較方法の正本は [PREVIEW_REGIONS.md](PREVIEW_REGIONS.md)。
- 出力は `preview/<stem>-<view>.png`（`--scale` は 1 ブロックあたりのピクセル数、既定 8）。`--layer` / `--layers` を指定し `--views` を省略すると断面だけを出力する。

## 10. ブロックデータ（`blockdata.py`, `data/`）

- `data/blockdata/blockdata-<version>.json` は mc-blueprint-blockdata の GitHub Release 資産をバイト列のまま同梱する。blockdata v1 の `blocks` にIDごとの `properties`・`default`、トップレベルに `minecraftVersion`・`dataVersion` を持つ。色・材質などの追加属性も含むが、ブロック検証は従来の情報だけを使う。
- `data/lock.json` の `blockdata` に repository・version・commit・各ファイルの `sha256` を記録する。生成は提供元で行い、利用者に Java を要求しない。
- スキーマの正本は提供元の `src/mcblockdata/blockdata.schema.json`。固定commitのコピーを `data/blockdata/blockdata.schema.json` に1部だけ置き、lockで固定する。テストでは既存の jsonschema を使い、スキーマ・出所・SHA-256をオフラインで検証する。
- API: `supported_versions() -> list[str]`, `load_block_data(version) -> BlockData`, `data_version(version) -> int`, `version_for_data_version(int) -> str | None`, `nearest_version(int) -> str`（DataVersion がちょうど一致しないファイルの読み込み先）。`BlockInfo` は従来どおりID・許可プロパティ・既定値を返す。読み込みは `functools.cache` で 1 回だけ行う。
- 版一覧はlockのファイル一覧と各JSONの `minecraftVersion`・`dataVersion` から導き、DataVersionの昇順にする。新しいバージョンが古いバージョンのブロックをすべて含むことをテストで保証する（[VERSIONS.md](VERSIONS.md)）。

## 11. CLI（`cli.py`）

```text
mcblueprint validate <blueprint.json> [--strict] [--max-dimension N] [--minecraft-version V]
mcblueprint build    <blueprint.json> [-o DIR|FILE] [--format schem|litematic] [--seed N] [--strict] [--max-dimension N] [--minecraft-version V]
mcblueprint inspect  <blueprint.json> [--json] [--max-dimension N] [--minecraft-version V]
mcblueprint stats    <blueprint.json> [--json] [--seed N] [--max-dimension N] [--minecraft-version V]
mcblueprint preview  <blueprint.json> [-o DIR] [--views top,north,east,isometric] [--scale N] [--layer Y ...] [--layers A..B|all] [--grid N] [--from X Y Z --to X Y Z] [--seed N] [--minecraft-version V]
mcblueprint import   <file.schem|.litematic> [-o FILE] [--name NAME] [--minecraft-version V] [--component] [--description TEXT]
mcblueprint diff     <a.json> <b.json> [--json] [--max-dimension N]
mcblueprint traverse <blueprint.json> (--building INDEX | --start X Y Z --end X Y Z) [--json]
mcblueprint versions [--json]
mcblueprint check    [PATH ...] [--strict] [--minecraft-version V]
mcblueprint components [--json] [--minecraft-version V]
mcblueprint design   <file.schem|.litematic|.json> [-o FILE] [--name NAME] [--part NAME=FILE ...] [--reference] [--no-views] [--no-parts] [--minecraft-version V]
```

`--minecraft-version` は読み込んだ JSON の `minecraftVersion` を置き換えてから検証・生成する（ブロックデータと出力の DataVersion が切り替わる。ファイルは書き換えない）。`versions` は同梱バージョンと DataVersion・ブロック数を表示する。

| 終了コード | 意味 |
|---|---|
| `0` | 正常終了 |
| `1` | 検証エラー |
| `2` | 引数・入出力エラー（ファイル未存在、JSON 構文エラー、書き込み失敗など） |

- `build` は必ず `validate` を先に行い、エラーがあれば書き出さずに `1` で終了する。
- `validate` / `build` は生成後に支持チェック（§8.5）を行い、警告を表示する。`--strict` で警告を終了コード `1` として扱う（`build` は書き出さない）。
- 出力先の既定は `output/<入力ファイルの stem>.schem`。`-o` にディレクトリを渡せばその中に既定名で、ファイルパスを渡せばそのパスに書く。ディレクトリは自動作成する。
- 成功時の表示:

```text
Wrote output/house.schem
Bounds: X -5..5  Y 0..7  Z -5..5  (11 x 8 x 11)
Blocks: 612
```

- `inspect` は生成せずに、名前・Minecraft バージョン・`bounds()` の合成による範囲とサイズ・Operation 数（ネスト込みとトップレベル）・Palette 数・seed・origin・宣言 `size`・説明を表示する。`--json` で機械可読な JSON。
- `stats` は生成して、総ブロック数（`minecraft:air` は除外し別枠で表示）、範囲、ブロック状態ごとの個数を多い順に表示する。`--json` で機械可読な JSON、`--seed` で seed を上書き。
- どちらも先に validate を行い、エラーがあれば表示して終了コード `1`。

## 11.4 check と components（`checking.py`）

- `check_preset()`: Markdown の ```json ブロックを順に読み、`palettes` を持つものは各 Palette を `set` で使う Blueprint に、`type` を持つ Operation や `operations` を持つ抜粋はそのまま Blueprint に包んで `validate()` する。エラーのパスは `json[<ブロック番号>].` を前置する。JSON として読めないブロックもエラーにして続行する。
- `check_component()`: `{"type": "component", "name": <stem>, "position": [0,0,0]}` だけの Blueprint を、そのファイルのディレクトリを先頭にした探索パスで `validate()` → `generate()` → `check_support()` する。エラーのパスから `operations[0]<name>.` を取り除き、部品ファイル内のパスとして表示する。生成した大きさと `description` は `components` コマンドの一覧に使う。
- 既定の対象は `designs/` と `components/`（再帰。`README.md` は除く）。ブロックデータは指定がなければ最も古い同梱バージョン（どのバージョンでも使えることを保証するため）。

## 11.4.5 design（`analysis.py`, `design_preset.py`）

既存の建築からデザインプリセットを推定する。

1. `analysis.analyze()` が `BlockVolume` の各ブロックに役割を付ける。名前で決まるもの（窓 = ガラス板・ガラス・鉄格子、ドア = ドア・トラップドア・フェンスゲート、照明、装飾 = 柵・カーペット・草花など）を先に分け、残りの構造ブロックは
   - 屋根の帯: 階段・ハーフブロックが構造ブロックの半分以上を占める最下層から上。帯の階段・ハーフは `roof`、階段より上の完全ブロックは `ridge`、それ以外は `gable`。帯の直下で同じ階段が続く層は軒として `roof` / `gable` を拾う
   - 最下層で外に面するものは `foundation`
   - 水平方向に空きと接する（外周）ものは `wall`、原木・木材なら `post`（水平の原木は `beam`）
   - 内側で上が空いていれば `floor`、下が空いていれば `ceiling`、それ以外は `interior`
2. 床の高さ（`floor` が最も広い層の半分以上ある層）、階高、壁の高さ、壁の厚さ（各辺から内側へ歩いた連続数の最頻値を軸ごとに取り、小さいほう）を求める。
3. 屋根は帯の階段の `facing` の分布から `gable`（南北または東西が 85% 以上。棟の向きも決まる）/ `hip`（4 方向）を判定し、張り出し（屋根の外形と壁の外形の差）、高さ、棟と妻壁のブロックを取る。
4. 窓は 6 近傍で連結成分にまとめ、幅・高さ・床からの高さの最頻値と個数、同じ面・同じ階の隣どうしの間隔の最頻値を取る。
5. 窓と入口の周りの装飾を部品として切り出す（`extract_opening_parts()`）。開口の連結成分ごとに、壁が伸びる軸（開口の長い方向、または隣接ブロックのある方向）と外側（壁の法線方向に歩いて建物の外に抜ける側）を決め、壁面内で上下左右 1（入口は上 2）、法線方向に前後 1 の範囲を取る。範囲内のうち、壁の本体（`wall` / `post` / `beam` 役割で、出現の半分以上が開口の近くにないブロック）と床・屋根などの構造は除き、枠・窓台・戸・アーチの階段などを残す。外側が北（-z）を向くよう `Transform.rotation` で回して（`facing` や接続も変換）最小コーナーを原点にし、同じ形をまとめて出現回数を数える。装飾のない素の開口は部品にしない。
6. 壁面の装飾を読む（`analyze_walls()`）。壁の列（高さごと）で壁の主ブロック以外の 1 種類が 70% 以上を占めれば帯、壁の外形の外側に置かれたブロック（屋根・土台・窓・ドア・照明・梁・柱を除く）を高さごとに集計し、周囲の 50% 以上に並ぶものを張り出しの帯、それ以外を個別の装飾とする。1 面に 3 本以上ある柱の間隔の最頻値、水平の原木（`beam`）のある高さも取る。トラップドアは戸ではなく装飾として扱う。
7. 階段・ハーフブロックの帯がないときは、上から見て水平の広がりが縮み続ける層（とその下に続く同じ幅の胴）を屋根とみなす（`_shrinking_roof_base()`）。最下層の外形が円形なら `dome`（各層の半径の平均が最下層の 62% 以上）/ `cone`、矩形なら `stepped` として、ブロック・高さ・半径・張り出しを取る。外形は壁の層から `rectangle`（外周の 90% 以上が壁）/ `round`（同じ半径の環との一致 80% 以上、半径も記録）/ `irregular` を判定する（`_footprint_shape()`）。
8. `design_preset.render_preset()` が `designs/README.md` の書式で md を組み立てる。Palette の weight は役割内の出現比率（8% 未満は落とす）、向きのプロパティは落とす（水平の原木の `axis` だけ残す）。参考図は役割を 1 文字にした各階の平面図（床の 2 段上。ドアは 1 段上から）、屋根の平面図（各列の最上段）、南と東からの立面図（幅 60・高さ 40 まで）。

推定なので、生成物は冒頭にその旨を書き、人が直す前提とする。

## 11.5 Import と diff（`importers.py`, `diffing.py`）

### import

```text
mcblueprint import <file.schem|.litematic> [-o FILE] [--name NAME] [--minecraft-version V] [--component] [--description TEXT]
```

1. `read_schematic()` が `.schem`（Sponge v1 / v2 / v3）または `.litematic`（単一リージョン）を読み、`BlockVolume`・貼り付けオフセット・`DataVersion` を得る。air は未設定として扱う。
2. `greedy_boxes()` が同一ブロック状態のセルを x → z → y の順に貪欲にまとめ、直方体の列にする。
3. `to_blueprint()` が `fill`（1 セルなら `set`）の Blueprint JSON を作る。座標は「ローカル座標 + オフセット」、`origin` は `[0, 0, 0]` なので、build すると元と同じ位置に貼り付く。プロパティはブロックデータのデフォルトと同じものを省いて短くする。
4. `minecraftVersion` は `DataVersion` から同梱blockdataの版一覧を逆引きする。一致するものがなければ `blockdata.nearest_version()`（その DataVersion より新しくない最も近い同梱バージョン。どれより古ければ最も古いもの）を使い、その旨と `--minecraft-version` の案内を表示する。`DataVersion` 自体がないファイルは `--minecraft-version` を要求する。
5. 書き出し後に validate し、MOD ブロックなど未知の ID があれば表示して終了コード `1`（ファイルは書き出す）。

出力は既定で `blueprints/<stem>.json`。高レベル Operation への復元（`wall` や `cylinder` として認識する）は行わない。

`--component` は同じ直方体まとめの結果を部品の書式（`formatVersion` / `name` / `description` / `operations`。`minecraftVersion` / `origin` は書かない）で `components/<name>.json` に書き出す（`to_component()`）。座標は体積の最小コーナーを `[0, 0, 0]` とする相対座標で、ファイルの貼り付けオフセットは無視する。書き出し後に `check_component()` で検証し、支持の警告も表示する。`--name` は部品名の規則（`^[a-z0-9_]+$`）に従う必要がある。

### diff

```text
mcblueprint diff <a.json> <b.json> [--json] [--max-dimension N]
```

両方を validate → generate し、`normalize()`（`origin` 基準の座標、明示 air の除外、プロパティのデフォルト補完）で貼り付け結果として等価な形にそろえてから比較する。追加・削除・変更のセル数、ブロック状態ごとの増減、各カテゴリの先頭 10 件の座標を表示する。差分がなければ `No differences.`。

## 12. エラー型（`errors.py`）

- `BlueprintError(Exception)`: 入出力・構文・内部整合性のエラー。CLI は終了コード `2`。
- `BlueprintValidationError(BlueprintError)`: `errors: list[ValidationError]` を持つ。CLI は `format_errors` で表示し終了コード `1`。

## 13. テスト方針

- 各 Operation: 既知の小さな入力に対するセル集合の完全一致、`bounds()` の一致、`hollow` の内部が空であること。
- Generator: 同じ Blueprint と seed で `BlockVolume` が完全一致すること。
- Validator: 検証項目ごとの異常系と、複数エラーの同時報告。
- Exporter: `nbtlib` での読み戻し。128 種以上の Palette で varint が 2 byte になるケース。
- examples: すべて validate / build が通ること。
- Skill: `.agents/skills/` と `.claude/skills/` の内容が同一であること。

## 明示通行検査（`traversal.py`）

`traverse` は最終生成volumeと明示したbuilding/足元座標を対象にする。通常validateの成功条件は変更しない。限定移動モデル、既知形状と未対応形状、CLI/APIの契約は [TRAVERSAL.md](TRAVERSAL.md) を正とする。
