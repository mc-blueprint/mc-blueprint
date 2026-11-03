# 領域の出力

## 完成後 volume の領域分割出力

```bash
mcblueprint build blueprints/house.json --region-size 32 16 32 -o output/house-regions
mcblueprint build blueprints/house.json --region-size 32 16 32 --format litematic -o output/house-litematic
```

`--region-size X Y Z` は各軸の正整数。指定時だけ全生成後の volume を非重複の矩形へ分割し、領域別ファイルと `manifest.json` を新規ディレクトリに出力する。既定先は `output/<入力ファイル名>-regions/`。`-o` は拡張子を含め常にディレクトリ名であり、出力形式は `--format`（既定 `schem`）で決まる。指定しない build のファイル名・形式・挙動は従来どおり。

対象は完成 volume の bounds（両端を含む）。最小コーナーから X/Y/Z 寸法で区切り、端の領域は残り寸法になる。未配置セルも既知の air として出力し、全 air の領域も省略しない。terrain がある場合は施工矩形全体を対象にし、施工外・未取得・保護範囲は成功扱いしない。ブロック状態は一括出力と同じで、地形の省略プロパティを補完しない。

生成・支持検査は全体に対して先に一度行う。これは生成メモリを減らす方式ではなく、並列生成・部分再生成も行わない。分割時は同じ完成 volume を保持し、領域ごとの exporter の作業領域を順番に解放する。

既存ディレクトリは上書きしない。一時ディレクトリを出力先の親に作り、全ファイルと manifest が揃ってから出力先を確定する。通常の出力例外では今回の一時成果だけを片付け、完成ディレクトリを残さない。プロセス強制終了や停電では親ディレクトリに `.出力名-*` の一時成果が残る場合がある。

## manifest v1

正本は `src/mcblueprint/schema/region-manifest.schema.json`、エディタ用コピーは `schema/region-manifest.schema.json`。これは出力のSchemaであり、Blueprint JSON の入力形式は変わらない。

| フィールド | 契約 |
| --- | --- |
| `formatVersion` | `1` |
| `format` / `minecraftVersion` | 全領域の形式 / 有効な Minecraft 版（CLI の上書き後） |
| `seed` | 生成に用いた有効な seed |
| `inputSha256` | 読み込んだ入力 JSON 生バイトの SHA-256（改行も含む） |
| `terrainSha256` | 保持する元地形 snapshot の入力ファイル全体 SHA-256。terrain なしは null |
| `origin` | 全領域で共通の Blueprint origin `[x,y,z]` |
| `bounds` | Blueprint 座標による全体の `min` / `max`（両端を含む） |
| `regionSize` | 指定した最大領域寸法 `[x,y,z]` |
| `regions` | Y、Z、X の順に最小座標を進めた領域配列 |
| `regions[].file` | manifest と同じディレクトリ内の相対ファイル名 |
| `regions[].bounds` | 各領域の Blueprint 座標範囲（両端を含む）。全体を重複・抜けなく被覆 |
| `regions[].offset` | `領域の min - 共通 origin`。各ファイル内の Offset / Position と一致 |
| `regions[].sha256` | 圧縮を含む成果ファイル全体の SHA-256 |

入力 hash は Blueprint 内で参照する部品ファイルの内容まで含む hash ではない。元地形はロード時の snapshot を再利用し、出力時に元ファイルを再読込しない。API `mcblueprint.region_export.export_regions` に完成 volume を渡す場合、呼出側が入力 JSON の SHA-256 と実際に生成した seed を渡す。

## 手動配置と再結合

全ファイルが共通の貼付基準を持つ。WorldEdit では同じ貼付基準を保って各ファイルをロード・貼付する。Litematica では全 schematic の配置 origin を同じワールド位置に設定する。ファイルが保持する offset が各領域を正しい位置に置くため、offset を重ねて手動加算しない。origin 自体は絶対ワールド座標とは限らない。

air による掘削を反映するには air を無視せず貼り付ける。manifest は自動適用コマンドを含まない。適用前に入力と成果の hash、各ファイルの宣言寸法と offset を確認する。

再結合の検証では、各ファイル内のローカル座標に `regions[].bounds.min` を加えた位置を使い、宣言寸法内の air を含む全セルを復元する。重複がないこと、全体 bounds を被覆すること、一括出力と全セルの状態が一致することを確認する。

## 実機確認手順

Minecraft Java 1.21.11 の新規ワールドで、同じ入力の一括出力と分割出力を離れた基準位置に配置する。屋根・地形の分割境界と端数領域、air による掘削箇所を撮影して比較する。貼付前に掘削位置へ確認用の固体ブロックを置き、air が反映されることも確認する。通路・入口を実際に歩いて境界で段差や障害が増えていないか確認する。自動テストの再結合成功だけで実機確認済みとは扱わない。


## 出力段階の比較測定

#96 の確定 commit `1d9cfafa3c4d2703894ad7a06bd309adcb0aeb1d` の fixture を外部から読み、同じ完成 volume を一括/分割出力した。Python 3.14.4 / Windows、schem、分割寸法 `[16,8,16]`。#96 の output 段階と同じく、各試行は別プロセスで全生成を終えてから測定し、時間は3回中央値、Python の追加確保量は別試行の tracemalloc peak を使う。生成時間・完成 volume 自体の保持量・プロセス全体 RSS は含まない。元記録と各 fixture の生バイト hash は [region-output.json](../benchmarks/region-output.json)。

| fixture | 一括 秒 | 分割 秒 | 一括 Python peak bytes | 分割 Python peak bytes |
| --- | ---: | ---: | ---: | ---: |
| sparse-256 | 0.02408 | 0.08757 | 2,533,629 | 2,539,237 |
| terrain-24 | 0.03200 | 0.06264 | 2,547,845 | 2,547,613 |
| air-24 | 0.04925 | 0.15204 | 2,566,328 | 2,551,659 |
| repeat-64 | 0.02305 | 0.11136 | 2,539,919 | 2,549,844 |

この規模では追加メモリの明確な削減は見られず、分割ファイル・hash・manifest の書込みで時間が増えている。生成全体のメモリが減ったことを示す測定ではない。条件を変えて再測定する場合は、#96 の fixture の絶対パスを次のスクリプトへ渡す。

```bash
python scripts/benchmark_regions.py <issue-96-worktree>/benchmarks/fixtures/terrain-24.json --region-size 16 8 16 --output output/region-measurement.json
```

#99 の確定 commit `9205679f9a119d1d38e975d8a509e98c269e26a9` で丘の元地形を別プロセス生成し、#102 で境界をまたぐ屋根と air 掘削を合成した確認も行った。全体 `[17,12,15]` を `[6,5,6]` で27領域へ分割し、schem / litematic の両方で、一括出力の3,060セルと air を含め全セル一致した。これはファイル検証であり、実機配置・撮影・通行確認は未実施。


## 後続の代表領域のユーザー確認（2026-10-20）

Minecraft Java Edition 1.21.11の確認用ワールドで、配布した `manual-review-20261020` のcross-002代表領域 `region-000020`／`region-000021` の同一anchorへの隣接配置と境界一致、`region-000047` の全air領域による消去、配置座標・offsetは正常とのユーザー報告を受けた。一括結果との比較・airを無視しない貼付の手順は配布READMEに従う。

比較画像ファイルは未提出で、AIによる新規撮影・提出画像の照合ではない。この確認は配布した代表領域に限られ、上記3,060セルfixtureの実機配置・撮影・通行未確認という記録を更新しない。

## 領域生成から単一ファイルへの出力

`export_generated_regions()` は #146 の `GeneratedRegion(core, volume)` の列を受け、
単一の `.schem` / `.litematic` を作る。CLIへの接続は #151 の範囲。
旧 `export_regions(volume, ...)` の分割ファイル・manifest v1 と既存の単一出力APIは維持する。

```python
from pathlib import Path

from mcblueprint.generator import GenerationLimits, generate_regions
from mcblueprint.region_export import export_generated_regions

# blueprint はロード済みの Blueprint。
regions = generate_regions(blueprint, limits=GenerationLimits(region=(64, None, 64)))
path = export_generated_regions(regions, blueprint, Path("output/build.schem"), format="schem")
```

- `regions` は一度だけ消費する。Generateの後にWeatherなどでcoreを確定した列も渡せる。
  呼出側でも消費済みvolumeをリストなどに保持しないこと。
- `bounds` は出力矩形（両端を含む）で、省略時は `blueprint.bounds()`。
  coreはこの矩形を重複・欠落なく被覆し、各volumeは対応するcore全体を保持すること。
  領域の到着順・寸法は任意。haloのセルと属性は出力しない。
  未配置と明示airはどちらも既知のairとして出力する。
- 各ファイルのOffset / Positionは `bounds.min - blueprint.origin`。
  ブロックの状態補完とDataVersionは既存exporterと共通。
  明示した出力矩形の端にairがあっても、矩形を縮めない。
- 戻り値は完成ファイルの `Path`。形式・coreの不正とI/O失敗は `BlueprintError`。
  生成側の例外・`KeyboardInterrupt` はそのまま伝播する。

coreのセルを行単位で32bitのパレット添字へ変換し、出力先の親に作る内部spoolへ退避する。
全体セル辞書・全volumeは作らず、消費後のvolumeへの参照を解放する。
spoolは全体で1個の行アドレス付きファイルで、固定したY/Z/X順の読取窓はX方向の1行。
常駐するのは生成側の現在の領域と、ブロック種類のパレット・core矩形の一覧・行バッファ。
生成用coreと出力形式の領域数は独立し、litematicも単一の `Main` 領域になる。

NBTには既存のnbtlibを使う。schemのBlockDataはbytearrayからnumpyのint8ビューへ渡し、
巨大なPython整数listを作らない。coreの読出しはRegionVolumeの内部添字配列
`_blocks`（Y/Z/X、0が未配置）を直接参照し、公開APIの追加やvolume.pyの変更は行わない。
spoolの行も添字配列のまま読み、初出順を保つpalette変換とvarintを配列処理する。
litematicのindicesはuint32配列、packingは64bit配列を
65,536セルの窓で構成する。出力形式の配列は全セル数に比例して保持するため、
NBT配列・シリアライズ時の一時コピー・gzipの作業メモリがゼロになるわけではない。
spoolのディスク使用量は全セル数×4 bytesで、別に圧縮中の出力ファイルの空き容量が必要。
内部spoolは一時物であり、公開データ形式や再開用の保存形式ではない。

圧縮結果を `.出力名-*` 内へ書き終えてから、同じ親ディレクトリの完成パスへ置換する。
gzipの圧縮レベルは1。既定の9よりファイルサイズを増やして書込み時間を短縮する。
圧縮前のNBT配列・palette・配置・origin・DataVersionは維持する。
既存ファイルがある場合も、置換成功まで元の完成品を保つ。書込み・生成失敗や通常の中断では
spoolと一時出力を削除する。強制終了・停電では `.出力名-spool-*` や `.出力名-*` が
残り得るが、完成品とは区別できる。電源断に対する永続化やspoolからの再開は保証しない。

### 自動検証

`tests/test_generated_region_export.py` は全4版×両形式×順方向/逆方向の再読込で、
旧単一出力とairを含む全配置・寸法・origin由来のoffset・DataVersionを照合する。
領域境界・端数・負座標・状態補完・全air・150種類のpalette、halo除外、消費済みvolumeの
解放、coreの重複/欠落/範囲外、生成例外、spoolのI/O失敗、出力中のI/O失敗と中断を検証する。
既存の単一出力とregion manifestの回帰試験も引き続き実行する。
Minecraftでの新たな実画面確認を実施したという意味ではない。

### 出力を含む巨大負荷の初回測定（#150、最適化前）

2026-11-03、このWindows端末で各条件を別プロセス・1回ずつ測定した。
Python 3.12.15、Windows-11-10.0.26200-SP0、numpy 2.5.4、nbtlib 2.0.4。
core `[64, null, 64]`（全高）、halo 16、各負荷の全体範囲は256×128×256＝8,388,608セル。
秒数はモデル読込から領域生成・spool・NBT構築・gzip圧縮・完成品の書込みまで。
生成秒数はイテレータの各 `next()` の実時間の合計で、他の段のピークRSSではない。
RSSは既存 `scripts/benchmark.py` の `GetProcessMemoryInfo / PeakWorkingSetSize` を再利用し、
起動・モデル読込から出力完了までのプロセス全体の上限値を取った。
nbtlibの配列シリアライズ時のbytesコピーと圧縮中の一時確保も含む。

| 負荷 | 形式 | 全体 秒（生値） | 生成 秒（生値） | ピークRSS bytes | 完成品 bytes |
| --- | --- | ---: | ---: | ---: | ---: |
| profile-castle-huge | schem | 17.76083440001821 | 0.02644779987167567 | 87056384 | 17270 |
| profile-castle-huge | litematic | 14.938046900031622 | 0.024814600008539855 | 107700224 | 4462 |
| 混色fill | schem | 70.9380428000004 | 27.798530699918047 | 170582016 | 1202531 |
| 混色fill | litematic | 44.62152159999823 | 27.457637399958912 | 170721280 | 1113067 |

最大は170,721,280 bytes（約162.8 MiB）で、4条件とも2GB以内だった。
これは本端末のGenerate/Export合成負荷の観測値であり、Weather/Criticを接続した全段や、
想定端末でのNFR2/NFR3達成、実建築の品質を証明する測定ではない。
全体volumeは保持しないが、最終出力配列は全セル数に比例する。
任意の寸法・palette数に対する2GBの強制上限は設けていない。

castleは既存fixtureの生バイト、混色fillは測定補助内の入力を
`json.dumps(raw, sort_keys=True, separators=(",", ":"))` でUTF-8化したものをhashした。
混色fillはseed 0、stoneとcobblestoneの重み1:2、Operation ID `wall` の単一fill。

| 入力 | SHA-256 |
| --- | --- |
| castle | `933127800cd14918d0d4c60239502987c175225e84de9fec060c70400f7fe735` |
| mixed | `e8739416059cf7f9557fbf70a2960a9a2c60f1a5f6cea288935ccd5a1df21129` |

実装は起点commit `3201bc976d696ea587d674cbfe1f075a5bc01a3e` に本変更を適用したもの。
測定した実装ファイル（`src/mcblueprint/` 相対）のLF正規化SHA-256は以下。
計測ログの生バイトhashとは改行の扱いが異なる。計測後は担当ファイルの改行だけをLFへ揃えた。

```json
{
  "region_export.py": "d968e8c2ba922d277cef32f78bbfbf4ba94f4915a81c1c2c0b25d4bf5f3ddc58",
  "exporters/_spool.py": "544f3b33c1ba2101fe5e7042bac6df399f59a234aa0d5bf52d4257d50d98cf4f",
  "exporters/base.py": "faadbffb47d3c8d4f142cf2ba116530363edbe61613be0d6d72f92830161c3b0",
  "exporters/schem.py": "52d551d7f9579575a4fe02fdd8d453fede5f8433166646257a514489307cdb81",
  "exporters/litematic.py": "32bc804109b8e7ae2ba69fc3b5e6e1a09b590db73a6c1b9de48efafa4dffe567",
  "generator.py": "826a57ff8606826244580af3eb2952e4c3b5b4b27551e80bf1bdd66fa39cb2ee",
  "volume.py": "205c4d0bc1df4d48699149a34b06508b75424560f29a9f1530bafb0dce90098f"
}
```

再測定はPython 3.12のタスクvenvから、各コマンドを独立に実行する。
測定JSON（入力・実装hash、Python・依存版、時間、RSS、出力サイズ）は標準出力に返る。
通常のコマンド記録ラッパーを使用し、過去の測定と混ぜず新しい系列として記録する。

```bash
python scripts/benchmark_generated_export.py castle --format schem
python scripts/benchmark_generated_export.py castle --format litematic
python scripts/benchmark_generated_export.py mixed --format schem
python scripts/benchmark_generated_export.py mixed --format litematic
```

### プロファイルに基づく最適化と再測定

混色fillの出力処理だけをcProfileで測定し、生成イテレータの実行中はプロファイラを
停止した。修正前は119,673,960回、修正後は1,480,210回の関数呼出しだった。
以下はプロファイラの計測時間で、計測器なしの実時間とは区別する。

| 処理 | 修正前 秒 | 修正後 秒 |
| --- | ---: | ---: |
| coreの状態読出し `state_rows` | 36.064 | セル単位のget呼出しを除去 |
| schemの状態→palette変換 `index_for` | 7.7737468 | 0.0000511 |
| varint生成 | 2.068523（スカラー） | 0.1599205（行配列） |
| `zlib.compress` | 28.5180854 | 0.1196149 |

coreの既存添字配列と行単位のpalette変換を再利用し、セルごとのVec3生成・get・状態の
辞書検索を除去した。schemの1byte添字は一括変換し、複数byteも出力位置を配列で計算する。
litematicも行単位にindicesへ書き込み、非airセル数を配列で数える。
圧縮レベル1への変更により、圧縮時間とファイルサイズを交換する。

同一入力・Python・依存版・core/haloで、最終実装をプロファイラなし・独立プロセスで
各1回再測定した。出力部分は「全体秒数−生成イテレータの実行秒数」で、spoolと
モデル読込・NBT構築・圧縮・確定を含む。前回の70.94秒／44.62秒も生成込みの全体値であり、
生成28秒をさらに加算する値ではない。

| 負荷 | 形式 | 出力 秒 前→後 | 全体 秒 前→後 | ピークRSS bytes 前→後 |
| --- | --- | ---: | ---: | ---: |
| castle | schem | 17.7344 → 2.2337 | 17.7608 → 2.2781 | 87056384 → 86597632 |
| castle | litematic | 14.9132 → 3.5791 | 14.9380 → 3.6437 | 107700224 → 108343296 |
| mixed | schem | 43.1395 → 2.4136 | 70.9380 → 48.3285 | 170582016 → 169959424 |
| mixed | litematic | 17.1639 → 2.7099 | 44.6215 → 52.4948 | 170721280 → 170426368 |

最終測定の生値：

| 負荷 | 形式 | 全体 秒 | 生成 秒 | 出力 秒 | 完成品 bytes |
| --- | --- | ---: | ---: | ---: | ---: |
| castle | schem | 2.2780626000021584 | 0.044354099954944104 | 2.2337085000472143 | 82969 |
| castle | litematic | 3.6437456000130624 | 0.06465009995736182 | 3.5790955000557005 | 44149 |
| mixed | schem | 48.32848440000089 | 45.9149110999424 | 2.413573300058488 | 1818788 |
| mixed | litematic | 52.494800799991935 | 49.78491149988258 | 2.709889300109353 | 1171268 |

両形式とも出力30秒以内、ピークRSS2GB以内だった。生成コードは変更していないが、
今回の生成実時間は45.91秒／49.78秒となった。変動原因はこの測定だけでは確定せず、
生成込み30秒の達成とは扱わない。特にlitematicは生成の増加により全体時間が前回より増えた。
本端末の生成込み2分以内という観測と、想定端末のNFR2達成も区別する。

同じ混色schemの中間実装（配列化済み・gzipレベル9）と最終実装（gzipレベル1）では、
BlockDataとPaletteのhashが一致した。

| 対象 | SHA-256 |
| --- | --- |
| BlockData | `8cd72bfcf1fce80862c6e5344116a17cd6b129d9d81f2073293765261ae6014b` |
| Palette（nbtlib.serialize_tagのUTF-8） | `cf5f9ac67e926d2427a76d986ee3be944ede85529602530b713524b301722916` |

全4版×両形式では生成volumeの各セル・offset・DataVersionを直接照合し、
varintの1〜5byte境界は従来のスカラー実装とバイト単位で比較する。
gzipレベル1/9の比較試験では時刻を固定し、解凍後のNBT全バイトの一致も確認する。
圧縮済みファイルのバイト列・サイズは変わる。

測定補助は `scripts/benchmark_generated_export.py` に配置した。
`--output` で測定JSON、`--profile` で出力処理のcProfileを保存できる。
JSONには生成・出力時間、RSS、入力・実装・NBT配列・paletteのhashを含む。
hash用の再読込は、時間計測とピークRSS取得を終えた後に行う。

```bash
python scripts/benchmark_generated_export.py mixed --format schem --output output/mixed-schem.json
python scripts/benchmark_generated_export.py mixed --format schem --profile output/export.prof
python -m pstats output/export.prof
```
