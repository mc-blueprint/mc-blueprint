# 地形計画とオフライン地形スナップショット

## 生成地形の2D計画（H04）

`terrain_field.plan_terrain(bounds, kind, settings, seed, clearance=0)` は
`flat / hill / mountain / plateau / valley / island` の高さ場を計画する。
settingsは合成済みのterrain設定（部分指定も可）。boundsは世界座標の閉区間。
clearanceは呼出し側が建築最高部・表層・頭上などに予約する高さの合計で、
地形帯は `H=size.y-1-clearance`。H<1、負の余裕、地形帯外の明示水位は拒否する。
地形単独ではclearance=0。複合敷地の余裕の決定はSite側の責務。

| 地形 | 最大形状起伏/H | 形状 |
|---|---|---|
| flat | 0 | ノイズなし |
| hill | 0.35×amplitude | 楕円の丘 |
| mountain | 0.85×amplitude | 中央の尖った山 |
| plateau | 0.60×amplitude | 中央が平坦な台地 |
| valley | 0.50×amplitude | X中央の谷 |
| island | 0.55×amplitude | 周縁が水位以下の島 |

基準面は地形帯の0.15。形状起伏の0.25倍の符号付きノイズを加え、最終高さを
地形帯内へ制限する。台地の平坦部はノイズを抑え、島の周縁は底面へ下げる。
amplitude=0とflatは平坦で、侵食でも起伏を追加しない。
量子化は世界座標で `floor(v+0.5)`。領域ごとのmin/max正規化はしない。

ノイズは直接依存 `pyfastnoiselite==0.0.7` のOpenSimplex2とnumpy一括APIを利用する。
MIT、Python 3.12 Windows wheelで導入・2D/3D分割/逆順/再生成一致を確認した。
波長は `max(2, scale×(2−1.5×roughness))`、オクターブ間の周波数比は2、
振幅比はpersistence。振幅和で[-1,1]へ正規化し、座標はfloat32を使う。
依存の選定はD61に従い、独自ノイズやdomain warpは追加しない。

侵食は世界の端点を含む最大65×65の等間隔全体格子で一度だけ計算する。
水滴数は `round(erosion×格子点数×2)`、寿命32、慣性/蒸発0.05、侵食/堆積率0.3。
双線形勾配、段と地形から導出したseed、固定順の逐次更新を使う。
平坦時の方向も同じ乱数列から決め、境界で終了する。運搬容量係数4、最小勾配0.01、
初期速度/水量1、重力4を固定する。終了時の残土は最後の有効位置へ堆積する。
侵食前後の粗格子の差を双線形補間して元の全解像度高さへ加える。
島は侵食後にも周縁の高さ制約を適用し、堆積によって周縁が水位を超えることを防ぐ。
erosion=0は更新なし。全体3D密度やボクセル配列は作らない。

`TerrainField.sample(region)` はXZ閉区間の読み取り専用2D viewを返す（Yは不使用）。
`to_dict()` は全体高さ・粗格子差・解決済み設定・依存版/方式・SHA-256を返し、
`TerrainField.from_dict()` で復元する。hash不一致・方式違いは再生成が必要。
JSONへ保存して再開した場も、任意分割・逆順サンプルで全体場と一致する。

`column(x,z,blockdata)` は充填層と水面・植生候補を返す。傾斜2以上は石、
水域または水平L1距離2以内の水際は砂、地形帯の0.8以上は雪、それ以外は草。
表層下3層は土（水際は砂岩）、それ以下は石とする。必要材の存在・full形状・
blockdata材質分類を確認する（現行blockdataの砂はglass分類）。水位以下の窪地には
地表の上から水位まで水を計画する。水位nullは島で地形帯の0.25、他は0.15。

terrain.vegetationは敷地の草/木候補の確率に使う。草は高さ1、木は高さ5・半径2。
5格子の木候補と頭上・外形余裕を固定し、水面・急斜面・砂・雪には配置しない。
Weatherの植生とは独立。候補をブロックへ展開しcoreへ切り出すのは後続の領域生成側。
このモジュールの導入では既存landform、makeの対応アーキタイプやSiteは変更しない。
局所3D密度・必須空洞・アーチは次節の計画とOperationで施工する。

## 局所3D密度と必須形状（H05）

`terrain_density.plan_density(field, overhangs=True, arches=False, main_cave=False,
entrances=1)` はH04の `TerrainField` を共有し、密度計画を返す。
基礎密度は `height(x,z)−y`、0以上が地盤。overhangs指定時はH04と同じ固定版の
3D世界座標ノイズを地形帯の `0.12×amplitude` 倍で加える。
波長・オクターブ・振幅比は2D場と同じ換算を使う。変位と掘削は別々で、
`caves=0` は追加枝洞だけを止める。枝洞は別seedのノイズの絶対値が
`0.14×caves` 未満、元の地表下4ブロック以上・底面上3ブロック以上で掘る。
底面は必ず地盤として残す。amplitude=0やoverhangs=Falseでも必須形状は消えない。

main_caveは中央の楕円体と入口までの楕円体列を作り、隣接中心の間隔を
小さい半径以下にして内部を重ねる。中心・整数半径・安定IDと接続辺を保持する。
入口は西・東・北・南の順で1〜4個。楕円体の外側2ブロックの殻も地盤にし、
主空洞と入口の和集合を最後に掘る。枝洞ノイズで必須空間や殻を失わない。
archesは底面から立つ2本の支持部と楕円形の開口・上部を確保する。
開口は地形のZ両端まで掘り抜き、前後の地盤に閉じ込められないようにする。
主空洞とアーチは別計画で扱う。いずれもXZ各15以上・地形帯16以上を要求し、
収まらない形状や入口数を黙って縮小しない。

`TerrainDensity.sample(region)` は世界座標の閉区間に対して `(y,z,x)` の
density/cavity/support/opening配列を返す。openingは外部までの必須開口全体。
計画外の照会はBlueprintError。
計画には3D配列・全体連結ラベルを保持せず、2D場と形状・接続グラフだけを持つ。
`to_dict()` / `TerrainDensity.from_dict()` は2D場を含むJSONとSHA-256で再開する。
未知版、hash・形状・接続グラフの不一致は再生成を要求する。

Blueprintの `terrain_density` Operationには `plan`（上記JSON）と
`minecraftVersion`（Blueprintと同じ版）が必須。Operationのseedはplanのfield.seedで
確定し、build時の乱数seedで変更しない。旧landformやトップレベルterrainとは独立し、
通常のemitされたBlueprintをvalidate/buildして同じ地形を再現できる。
make・Siteへの接続と造成はH06/H07で行う。

施工時の作業領域は最大XZ32×32・Y16（樹冠候補の照会はXZ各2の余裕）に分ける。
高さ方向の走査で2Dの最上面だけを保持し、表層下3層を土/砂岩、支持部を石にする。
表層材と植生候補はH04のcolumnを再利用する。水は非地盤セルの世界座標水位以下へ
置き、主空洞の既定水位はなし。明示した水位だけ主空洞にも適用する。
木・草は最上面がcolumn.topと一致するときだけ配置し、樹冠も隣接領域へ切り出す。
木の幹が地盤と衝突する候補は採用しない。
樹冠も密度が空気のセルだけに置き、隣接列の地盤や必須支持部を上書きしない。

roleは `terrain.ground / terrain.cavity / terrain.opening / terrain.support / terrain.water /
terrain.vegetation`、既定elementはterrain#0、sourceはconfig.terrain
（植生はconfig.terrain.vegetation）。Operationの明示属性を継承する。
必須空洞のairや地形支持部を建物用のstructure予約にはせず、既存予約の競合判定を守る。
`check_density_region(plan, volume, core)` は完成coreの主空洞・アーチ開口の閉塞と石の支持部の欠落を
照合する。水は計画水位以下だけ許し、欠落・未知セルを成功扱いにしない。
通常の落下ブロックsupport検査をこの形状照合の代用にはしない。
領域生成のメモリ予算には2D場と局所作業配列を加算し、予算不足は拒否する。

## オフライン地形スナップショット

`importers.read_terrain(path) -> TerrainSnapshot` は `.schem`（Sponge v1 / v2 / v3）と単一領域 `.litematic` を地形として読み取る。Minecraft への接続、回転・自動保護判定は行わない。入力ファイルは変更しない。Blueprintとの合成は後述の `terrain` 指定を使う。

既存の `read_schematic()` は建築用 import の API で、従来どおり air を省いた可変の `BlockVolume` を返す。地形の取得範囲をその非 air セルの bounds から求めてはいけない。

## 保持する情報

`terrain.TerrainSnapshot` は frozen dataclass。`Vec3`・`AABB`・`BlockState` も不変で、`blocks` は渡された表のコピーを読み取り専用にして保持する。

| 項目 | 契約 |
|---|---|
| `bounds: AABB` | ファイルが宣言する全領域。最小コーナーは `[0, 0, 0]`、最大コーナーを含む。全 air や air の余白も含む |
| `blocks: Mapping[Vec3, BlockState]` | ローカル座標の元ブロック状態。通常の `minecraft:air` は省略する。`cave_air` / `void_air` とブロックの元プロパティは保持し、デフォルト補完や版変換は行わない |
| `offset: Vec3` | ファイルの貼り付け基準に対する宣言領域の最小コーナー |
| `source_bounds: AABB` | 貼り付け基準で表した取得領域。`bounds` の両端に offset を加えた値 |
| `data_version: int | None` | 入力の DataVersion（`.litematic` は MinecraftDataVersion）。未記載は `None` |
| `minecraft_version: str | None` | DataVersion が同梱版と完全一致するときの Minecraft 版。未知の版や未記載を近似しない |
| `input_sha256: str` | 読み取った入力ファイル全体（gzip 圧縮を含む）の SHA-256、小文字 16 進文字列 |

`.schem` の宣言寸法は `Width` / `Height` / `Length`、offset は `Offset`（未記載時はゼロ）を使う。`.litematic` の寸法は `abs(Size)`。負の Size を持つ軸では最小コーナーを `Position + Size + 1`、正の軸では `Position` とする。セルの格納順は両形式とも x → z → y。

offset は絶対ワールド座標を保証しない。ファイルの貼り付け基準に対する変換として扱い、実際にどこへ配置するかは呼び出し元が決める。

## 既知の air と未取得の照会

```python
from mcblueprint.importers import read_terrain
from mcblueprint.model.vec import Vec3

terrain = read_terrain("terrain.schem")
local = Vec3(0, 0, 0)
state = terrain.get(local)
# None は未取得。air を含む取得済みセルには BlockState が返る。
assert state is not None
assert terrain.to_local(terrain.to_source(local)) == local
```

`get(pos)` の引数はローカル座標。`bounds` 内で表に存在しない位置には `minecraft:air` を返し、領域外には `None` を返す。`blocks.get()` だけではこの区別ができないので、照会には `TerrainSnapshot.get()` を使う。

`to_source(local_pos)` は offset を加え、`to_local(source_pos)` は offset を引く。どちらも領域外の位置を変換でき、往復して元位置へ戻る。全 air の入力も取得済み領域として読み取れる。

## 拒否条件

不正な NBT、整数項目（寸法・座標・版・palette index）の小数や文字列、非正の宣言寸法、セル数や packed 配列長の不一致、ByteArray 以外の BlockData / Blocks.Data、途中で切れた・32 bit を超える varint、空・重複 index・負 index・構文不正の palette、palette にないセル index は `BlueprintError` とする。`.litematic` の複数領域は拒否する。

ブロック状態だけで保持できない非空の `BlockEntities` / `TileEntities` / `Entities` / `PendingBlockTicks` / `PendingFluidTicks` / `BlockTicks` / `FluidTicks` があるときも `BlueprintError` とし、対象（ルート、Schematic.Blocks、リージョン名など）と種類をメッセージに含める。空のリストは許容する。これらを通常の建築用 import に合わせて黙って省略してはいけない。

## Blueprintの矩形施工

任意のトップレベル項目 `terrain` を指定すると、その矩形内の元地形へOperationを適用する。
指定しなければ従来どおり空のvolumeから生成する。例（元ファイルの取得範囲内に収める）：

```json
{
  "formatVersion": 1,
  "minecraftVersion": "1.21.11",
  "name": "excavation",
  "origin": [0, 0, 0],
  "terrain": {
    "source": "terrain.schem",
    "region": {"from": [-4, 7, -2], "to": [-1, 8, -1]},
    "protected": [{"from": [0, 7, -2], "to": [2, 8, -1]}]
  },
  "operations": [
    {"type": "set", "position": [-4, 7, -2], "block": "air"}
  ]
}
```

- `source` は必須で、Blueprintファイルの親ディレクトリから解決する。絶対パスも使える。
- `region` は必須の施工・出力矩形、`protected` は任意の保護矩形配列（既定は空）。
  各矩形の `from` / `to` は整数3要素で、両端を含み順序は問わない。任意maskは扱わない。
- 矩形とOperationの座標は元ファイルの貼付基準に対する座標で、snapshotの
  `source_bounds` と同じ系。ファイル内の0始まりのローカル座標や絶対ワールド座標とは限らない。
  元のoffsetを引くとsnapshotのローカル座標になる。
- `origin` は従来どおり、出力を貼るときの基準となるBlueprint座標。
  出力offsetは `region.min - origin`。元ファイルと同じ貼付基準に置くには `[0,0,0]` を使う。

生成は元snapshotの施工矩形の全セルを、airも含め新しい作業volumeへコピーしてから
通常のOperationを配列順に適用する。未変更セルを保持、配置は置換、明示airは削除となる。
`copy` / `replace` は元snapshotではなく、先行Operationを反映した作業volumeを読む。
`copy` の読取元も施工矩形内に収める。全airの完成結果も施工寸法とoffsetを保持して保存する。

施工矩形の一部でも取得範囲外、または保護矩形と交差する場合は拒否する。
Operationの解析範囲（copy元を含む）が施工矩形外の場合も検証エラーとなり、
実配置が矩形外に出た場合は生成を拒否する。最大寸法・最大体積・`size` の検査は
非air数ではなく施工矩形全体に適用する。最大体積は既存どおり100,000,000セルで、
生成APIから直接呼ぶ場合もコピー前に体積上限を確認する。

元DataVersionはBlueprintのMinecraft版と完全一致する必要があり、未知・未記載・版違いは拒否する。
施工範囲内の未知ブロック、不正なプロパティ、前述の保持不能データも拒否する。
地形合成の出力では元セルと新規配置の両方で現在のstateをそのまま保存し、
省略プロパティの既定値をファイルへ補完しない（意味はMinecraft既定値のまま）。
明示されたプロパティはcopy・replace・読戻しでも保持する。
terrain指定なしの既存exportは従来どおり省略プロパティを補完する。

## API・CLIと再生成

### makeと領域生成

make用の内部読取経路 `read_terrain_regions(path, max_memory_mb=...)` は旧importerと
NBTの版・offset・パレット解釈を共有する。`nbtlib` の入力配列から1行ずつ密なuint32添字へ
変換し、一時spoolへ保存する。全セルの座標辞書・BlockVolume・Python添字リストは作らない。
通常の `read_terrain()` / `TerrainSnapshot` の公開APIは変更しない。

圧縮解除は固定長窓で事前走査し、入力配列・変換行・パレットを予算へ含める。
入力の圧縮解除サイズの3倍と1MiB、パレット1状態あたり2048bytes、行幅あたり128bytesを
準備時の保守的な見積りとし、予算超過は領域生成前に資源不足として拒否する。
spoolの読取行キャッシュは8行に限定する。生成予算にはこの窓・パレット・施工列計画と
道路の路面/頭上索引も含める。予算値はプロセス全体のRSSと同一ではない。

`generate_regions()` は施工矩形内の元状態をhalo付き領域へ初期化し、造成→道路→通常Operationの
順に適用する。道路の支持・路面・頭上はWeatherから保護する。
元地形の通常airも未配置セルと同じ植生候補にするが、予約・通行域の保護は維持する。
地面距離は造成前の元地形で、通常air以外の最上面Yを使う。
全air列の基準は取得下端Y。建物の上面を地面にしない。
`metadata.generation.groundHeight` の一定値/地形場の判別形式は [BUILDSPEC.md](BUILDSPEC.md) を参照。
入力hashは圧縮ヘッダーも含み、makeの再開context、emitしたBlueprint、公開直前に照合する。
hash違いの旧出力は更新しない。施工範囲・保護域・版不一致の拒否条件は旧施工と共通。
makeの出力先が正規化したパスで元地形入力と一致する場合は、書込み前に終了コード2で拒否する。

### 従来の全体施工

元地形の明示位置に平坦な床・基礎を造成する場合は、任意の `foundation` 項目を使う。
全列の事前検査、探索帯、切土・盛土・支持深さの契約は [FOUNDATION.md](FOUNDATION.md)。
造成後の地形と明示した入口を道路で結ぶ場合は [SURFACE_PATH.md](SURFACE_PATH.md) の
`surfacePath` を使う。既存 `path` の手動高さ指定は変わらない。

`load_blueprint_dict(data, base_dir=...)` と `validate(data, base_dir=...)` の `base_dir` は
source相対パスの基準（省略時cwd）。`load_blueprint(path)` とCLIはBlueprintの親を渡す。
`metadata.generation` がobject以外の場合は指定なしとして従来の施工経路を使う。
`validate_and_load(data, *, base_dir=None, max_dimension=1024)` は成功時に検証に使った
同じBlueprintと空のエラー列、失敗時に `None` と検証エラー列を返す。
CLIは検証と生成で同じsnapshotを使い、同一実行中にsourceを再読込しない。
参照不能・保持不能も検証エラーであり、CLIは終了コード1を返す。

`generate(blueprint)` は呼ぶたびに保持した元snapshotから全再生成する。
生成したvolumeやディスク上の元ファイルが後で変わっても、読み込んだBlueprintのsnapshotは変わらない。
元ファイルの更新を取り込むにはBlueprintを読み直す。旧成果物へ重ねて生成しないため、
Operationを削除して再生成すればその場所は元地形へ戻る。

CLIの新しいフラグは不要で、通常のvalidate→buildを使う。出力は差分maskではなく
施工矩形全体の完成snapshotなので、取得後にワールド上で変更されたセルも上書きする。
貼付時にairを無視すると掘削が反映されない。実機適用やScene管理は行わない。
