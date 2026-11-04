# 地表道路の高さ解決

任意のトップレベル項目 `surfacePath` は、明示した経路を造成後の地表と入口に接続する。
terrainが必須で、単一路を扱う。経路探索、建物・地形全体の移動、水横断、橋、トンネル、
急勾配は扱わない。既存Operation `path` の入力・意味・配置は変更しない。

```json
"surfacePath": {
  "points": [[0, -8], [0, -4]],
  "entrance": [0, 3, -3],
  "width": 3, "block": "cobblestone", "stairs": "stone_brick_stairs",
  "clearance": 2, "scan": {"minY": -4, "maxY": 15},
  "ground": ["stone", "dirt", "grass_block", "cobblestone"],
  "maxCut": 3, "maxFill": 3, "maxSupportDepth": 3
}
```

すべて必須。`points` は高さを指定しない整数の **[x, z]** を2点以上並べる。
各区間はXまたはZの一方だけが変わる軸平行の直線。中間点は90度の曲がり角だけとし、
直線途中の重複指定、折り返し、中心線の自己交差は拒否する。
`entrance` は接続先の足元 **[x, y, z]**。最終経由点から最後の進行方向へ1セル進むと
入口XZになるよう指定し、終点路面Yは入口Y-1に固定する。最後の接続1セルも検査・造成する。
道路本体は全幅、入口への最後の1セルだけは明示した入口の中心を検査する。
座標はterrainと同じBlueprint座標。建物は自動で移動しない。

`width` は1以上、`clearance` は2以上。幅の偏り・角の幅square・階段向きは既存path規則を使う。
幅方向は区間方向 `(dx,dz)` に対して `(dz,dx)`、横のindexは
`-((width-1)//2)..width//2`（両端を含む）で、偶数幅では片側が1セル多い。
`block` / `ground` は [FOUNDATION.md](FOUNDATION.md) の明示対応する通常完全地盤材IDのみ。
造成で追加した基礎材もgroundに含める。砂・砂利・水・植生・未知形状を安定と推測しない。
`stairs` は対象版にある `*_stairs`。facing / half / shapeは自動設定のため指定不可、
水没状態も拒否する。Palette、縁石、照明はこの自動解決の対象外。

## 検査範囲と高さの決定

元snapshotへfoundationの変更列を適用した状態を読む。snapshotとfoundationは変更しない。
通常Operation（建物など）の前に道路を計画し、適用後にはそれらが道路を妨げていないかも検査する。
make用snapshotでは造成の列区間計画を参照し、道路writesも列内の同材区間で保持する。
領域生成ではcore内の路面・支持・頭上を必要haloとともに検査するため、領域分割や列挙順で
判定は変わらない。完成後の支持・路面・頭上はWeatherの通行保護域になる。

1. 道路全幅、曲がり角square、最後の入口1セルの全列を `scan.minY..maxY` で検査する。
   探索帯全体が施工・取得内で、施工矩形は保護矩形と交差しない必要がある。
   下端から明示groundが連続し、上が通常airだけ、上端はairである単純な地盤が対象。
   空洞、水、砂礫、植生、浮いた障害物、未取得セルは座標・原因付きで拒否する。
2. 各経由点の幅方向断面の地表Yを並べ、小さい側の中央値を高さ候補にする。
   曲がり角では両方向の断面と角squareの全列を重複なく含める。終点だけは入口Y-1。
3. 各区間の高さを既存 `path.segment_cells` の丸め線形補間（半端は上側）で決める。
   1セル当たりの高低差は最大1。別の高さ候補や迂回路は探索しない。
4. 曲がり角の前後 `ceil(width/2)` セルの中心線は角と同じYで平坦にする必要がある。
   階段上で曲がらない。幅の重複部分に高さや階段向きの矛盾があっても拒否する。
   角squareの全候補を既存pathの配置省略と独立に照合し、非隣接区間との競合も拒否する。
5. 中間断面も含む全幅の各路面Y=R、地表Y=Sで必要切土 `max(0,S-R)`、
   必要盛土 `max(0,R-S)`、必要支持深さ `max(1,R-S)` を上限と比較する。
   等号は許容。支持深さと盛土の床層を含む定義はfoundationと同じ。
   `maxCut` / `maxFill` は0以上、`maxSupportDepth` は1以上。
6. 全路面で探索帯がR-1からR+clearance+1までを含むことを要求する。
   上端より上、下端より下、道路外の通行は保証しない。

全列を通過してから、支持の盛土をblock、路面を既存pathの通常材または向きを解決した階段にする。
路面より上の地盤は地表まで全て切除し、トンネルを残さない。
例: S=3、R=1なら切土深さ2でY=2,3をairへ、R=1を路面材へ置換する。
S=-1、R=2なら盛土・支持深さ3で、Y=0,1が支持層、Y=2が路面となる。

## 頭上・入口と失敗時

通常Operationの後にも、全幅の路面が予定状態であること、直下の支持、
路面より上のclearanceセルを再検査する。上り・下りの遷移では、隣接する高低両断面の頭上を
`max(両路面Y)+clearance` まで確認し、低い断面側の追加1セルも見落とさない。
道路終点と入口床は同じ層で、上り終端の階段は入口へ向かう側が高くなる。
最後の入口セルも床・直下支持・頭上を検査する。

入口はair、または対応する木製・銅製ドアの正常な上下段を許容する。ドアの面は道路の接近軸に
一致する必要があり、開けて通る前提。対象は oak / spruce / birch / jungle / acacia /
dark_oak / mangrove / cherry / bamboo / pale_oak / crimson / warped のドア。
銅製は copper / exposed_copper / weathered_copper / oxidized_copper のdoorと
各waxed版の8状態。木製同様に手で開閉できることは
[公式1.21リリースノート](https://feedback.minecraft.net/hc/en-us/articles/27547857163917-Minecraft-Java-Edition-1-21-Tricky-Trials) に従う。
同じIDのlower/upperを対にし、対象Minecraft版の既定値で省略プロパティを補完してから
facing / open / hinge / poweredが上下で一致することを確認する。
鉄ドアや未知の開閉物を通れるとは推測しない。入口床の通常木材planksは許容する。
ドア以外の道路上の装飾・建物干渉、支持の破壊も拒否する。

計画失敗では変更列を返さず、後段検査の失敗でも生成volumeを返さず出力を更新しない。
元snapshot、造成計画、既存出力ファイルは保全する。CLIは検証エラーの終了コード1を返す。
実機の全衝突・開閉・落下挙動のシミュレーションではなく、往復歩行は別途確認する。

## APIと報告

- `SurfacePathSpec.from_dict(raw)` はSchema検証済み項目を変換する。
- `plan_surface_path(spec, terrain, foundation=None) -> SurfacePathPlan` は不変の計画を返す。
  対応外条件は `SurfacePathError.issues` のBlueprint座標 `position` と原因 `reason`。
  上限超過には必要cut/fill/support、頭上境界をメッセージへ含める。
- `SurfacePathPlan.writes` はBlueprint座標の重複しない不変tupleで、造成後状態からの実変更だけ。
  状態はair、支持・通常路面材、向きを設定した階段。`surfaces` は道路本体の期待路面、
  `head_tops` は路面ごとの確認上端、`result` は集計、`minecraft_version` は検査対象版。
- `result.points` は高さ解決済みVec3経由点。入力のwidth/block/stairs/clearanceと合わせて
  既存pathへ具体化した結果であり、既存path本体の規則は変更しない。
  `to_json()` はCLIと同じ辞書を返す。
- `result.surface_cells` は全幅の路面と入口1セルの重複しない列数。
  `changed_cells` は実変更数。`cut_cells` は路面より上をairにした数、
  `fill_cells` は路面より下の支持盛土、`surfaceChangedCells` は路面層の置換数。
  3つの和がchangedCellsで、foundationや後続Operationの変更数は含めない。
- loaderは地形を一度読み、造成計画の後で道路計画を `Blueprint.surface_path` に保持する。
  generatorは造成→道路→通常Operation→道路再検査の順で同じ計画を使う。
  `generate()` / `generate_with_report()` の既存戻り値は変わらない。
- `validate` / `build` は指定時だけ `SurfacePath: {...}` を出し、解決経由点・入口・幅・
  セル数と必要深さの最大値を報告する。指定なしの既存CLI出力は変わらない。
