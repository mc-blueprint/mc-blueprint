# #243 領域処理の性能と回帰

この記録はNBT出力予算・測定再開の反証指摘へ対応する前の測定値として保全する。対応後の出力段測定・固定課題hash・全件検証は[追加記録](region-performance-243-review.md)を参照。

生値・段開始時刻・RSS・4種hash・cProfile・テスト上位30件は [JSON](region-performance-243.json) を参照。NFR1基準端末の達成認定は未実施。

| 固定入力 | E2E中央値 前→後（秒） | 全3試行の最大RSS 前→後（MiB） | 4種hash |
|---|---:|---:|---|
| house-large | 209.706 → 28.888 | 210.7 → 100.2 | 一致 |
| tower-medium | 42.751 → 23.958 | 198.0 → 116.0 | 一致 |
| tower-large | 315.012 → 206.127 | 341.4 → 240.6 | 一致 |
| junction-medium-1.21.11-schem | 2.256 → 2.873 | 88.0 → 88.2 | 一致 |
| castle-medium-1.21.11-schem | 50.544 → 62.877 | 220.6 → 123.7 | 一致 |
| castle-huge-1.21.11-schem | 678.965 → 288.627 | 261.6 → 157.2 | 一致 |
| town-medium-1.21.11-schem | 71.662 → 49.808 | 216.0 → 120.0 | 一致 |
| town-huge-1.21.11-schem | 705.797 → 293.180 | 258.7 → 178.3 | 一致 |
| cave-medium-1.21.11-schem | 14.357 → 3.555 | 166.1 → 85.5 | 一致 |
| cave-huge-1.21.11-schem | 586.592 → 73.595 | 202.5 → 105.2 | 一致 |
| underground_city-medium-1.21.11-schem | 1.062 → 1.540 | 53.4 → 53.1 | 失敗一致（成果物なし） |
| underground_city-huge-1.21.11-schem | 656.882 → 133.134 | 240.5 → 133.0 | 一致 |
| house-medium | 58.263 → 9.022 | 196.1 → 85.7 | 一致 |

## 全件テスト

- before: 3156件、失敗0、エラー0、skip1、26222.24秒
- after: 3176件、失敗0、エラー0、skip1、7950.43秒

ローカル比は0.303。同じ比を過去のCI約4時間41分へ当てた粗い見込みは85.2分。OS・CPU・並列ジョブが異なるため実測値ではない。

## 検証

- ruff check .、全件pytest、mcblueprint check --strict、wheel作成、git diff --checkを実行。詳細の終了コードはJSONに記録。

## 段別中央値

既存H01のstage_hook境界を使用。GenerateにはWeather・Criticの逐次consumeを含む。Export最終区間の終点はmakeの戻り。独立プロセス起動等はE2Eだけに含む。

| 固定入力 | Materialize 前→後 | Generate 前→後 | Critic 前→後 | Export 前→後 |
|---|---:|---:|---:|---:|
| house-large | 3.360 → 4.478 | 200.665 → 17.855 | 1.904 → 1.245 | 0.655 → 0.804 |
| tower-medium | 6.626 → 8.576 | 28.521 → 6.834 | 4.598 → 4.666 | 0.231 → 0.301 |
| tower-large | 29.069 → 39.157 | 216.174 → 84.118 | 57.882 → 65.947 | 1.398 → 1.735 |
| junction-medium-1.21.11-schem | 0.583 → 0.682 | 0.259 → 0.206 | 0.018 → 0.020 | 0.030 → 0.040 |
| castle-medium-1.21.11-schem | 6.351 → 9.984 | 39.125 → 43.520 | 0.978 → 1.199 | 0.265 → 0.301 |
| castle-huge-1.21.11-schem | 8.907 → 14.993 | 641.389 → 232.013 | 19.149 → 25.888 | 4.727 → 7.432 |
| town-medium-1.21.11-schem | 8.752 → 13.704 | 58.488 → 30.759 | 0.983 → 1.008 | 0.195 → 0.298 |
| town-huge-1.21.11-schem | 17.166 → 28.143 | 658.963 → 222.880 | 20.706 → 27.322 | 4.858 → 7.269 |
| cave-medium-1.21.11-schem | 0.178 → 0.352 | 12.396 → 0.883 | 0.453 → 0.501 | 0.141 → 0.187 |
| cave-huge-1.21.11-schem | 0.328 → 0.572 | 561.142 → 40.896 | 19.236 → 23.249 | 4.585 → 6.817 |
| underground_city-huge-1.21.11-schem | 2.825 → 4.718 | 627.263 → 92.045 | 19.437 → 25.104 | 4.719 → 7.016 |
| house-medium | 2.061 → 2.339 | 53.574 → 3.833 | 0.612 → 0.228 | 0.250 → 0.175 |

## 分離したcProfile

計時3反復とは別実行。以下は累積秒で、関数同士の時間は重複する。

| 固定入力 | 関数 | 前→後（秒） |
|---|---|---:|
| castle-huge-1.21.11-schem | randomness.py:first_draws | 29.444 → 143.033 |
| castle-huge-1.21.11-schem | weather.py:apply_weather | 1057.458 → 1.462 |
| castle-huge-1.21.11-schem | weather.py:_water_distances | 30.361 → 0.120 |
| castle-huge-1.21.11-schem | critic.py:consume | 42.603 → 57.437 |
| castle-huge-1.21.11-schem | traversal.py:route | 1.826 → 1.749 |
| castle-huge-1.21.11-schem | support.py:check_support | 55.636 → 64.253 |
| castle-huge-1.21.11-schem | pipeline.py:_working_budget | 0.000 → 2.340 |
| town-medium-1.21.11-schem | randomness.py:first_draws | 34.058 → 28.953 |
| town-medium-1.21.11-schem | weather.py:apply_weather | 23.617 → 0.061 |
| town-medium-1.21.11-schem | weather.py:_water_distances | 0.496 → 0.004 |
| town-medium-1.21.11-schem | critic.py:consume | 0.810 → 1.135 |
| town-medium-1.21.11-schem | traversal.py:route | 1.318 → 1.253 |
| town-medium-1.21.11-schem | support.py:check_support | 1.263 → 1.587 |
| town-medium-1.21.11-schem | pipeline.py:_working_budget | 0.000 → 1.839 |

## 比較の制約

- NFR1相当端末ではないためNFR2/3の達成認定は行わない。
- 3反復のtimingとcProfile、matrix、全件pytestは直列。静的検査等の短時間管理操作のみ並行。
- hash計算は計時外。workerの全体PeakWorkingSetSizeを記録し、段の瞬間RSSと区別。
- beforeのenvironment.engineHashはディスク上の作業中コードのhashであり、実行コードは起点commitからロードする。
- ロード経路をそろえるためafterも同じgit読取りを行い、現行ソースをコンパイルする。
- ロード数が異なった初回house-mediumはsupersededへ保全し、比較は最終測定器の3反復へ置換。
- matrixの各1試行はhash回帰用であり性能判定には使わない。
- 日中の再測定時に通常の画面描画・セキュリティ処理・Javaプロセスの稼働を観測。他者のプロセスは停止せず、自身の重い検証は直列とした。試行のばらつきは生値に残す。
- 最終matrix後半以降に別タスクのPython稼働も観測し、親子関係で自身の残存測定ではないことを確認。端末全体を無負荷へ隔離した計測ではないため、基準端末での再計測と区別する。

地下街mediumはH01の固定8区画が主空洞に収まらず、前後ともResolveで同じ失敗。成果物なしのためhash成功件数に数えず、巨大入力で4版・両形式の正例を確認する。

## 抽選だけを起点実装に戻した単独比較

同じafter予算で、抽選モジュールだけbeforeへ差し替えた独立3反復。worker内でloaderをassert検査し、全試行の4種hash一致も確認した。

| 固定入力 | 元のタイル抽選中央値 | 採用した矩形抽選中央値 |
|---|---:|---:|
| house-medium | 37.220 | 9.022 |
| tower-medium | 38.017 | 23.958 |
| castle-huge-1.21.11-schem | 214.171 | 288.627 |
| town-medium-1.21.11-schem | 75.513 | 49.808 |

住宅・塔・街並みの時間短縮を優先して矩形抽選を採用した。課題による効果の差も表に残す。単独比較は経路ゼロ件修正前で、選んだ4課題には経路がある。before→afterのcProfileでfirst_drawsが増えた点も隠さず記録する。

初回の単独比較はimport順の不備で差替えが適用されず無効。その結果に基づく棄却判断を撤回し、無効な生値はJSONのsupersededへ保全した。一時的なソース差戻し後は実装・設定・依存hashの完全一致を確認して有効な測定を復元した。

## 中規模城郭の近接した追加比較

主測定で時間が増えたため、他の自身の測定・テストを待機させ、beforeの独立3反復に続けてafterの独立3反復を実施した。cProfileはさらに別実行。

- before: 中央値71.091秒、生値[61.103, 71.091, 80.965]
- after: 中央値50.268秒、生値[57.494, 50.268, 49.118]

追加比較の入力・4種hash・段別時間・プロファイルもJSONに保全する。主測定の不利な結果をこの値へ置換しない。

追加比較の分離cProfile（累積秒）は次のとおり。計画索引・歩行・支持の採用根拠と、抽選の残差を区別する。

| 関数 | 前→後（秒） |
|---|---:|
| apply_weather | 30.319 → 0.034 |
| critic.consume | 1.329 → 1.185 |
| traversal.route | 1.888 → 1.133 |
| check_support | 1.794 → 1.614 |
| first_draws | 22.853 → 36.320 |

## 大規模生成テスト群の合計

| テストファイル群 | 前→後（秒） |
|---|---:|
| test_castle_planning.py | 3723.840 → 2533.347 |
| test_town_planning.py | 8103.975 → 3230.230 |
| test_underground_planning.py | 13680.564 → 1537.215 |

## 全件pytestの上位30件

コマンドは両側とも --durations=30。以下はJUnitの各テスト合計秒で、consoleのcall/setup/teardown別の元出力もJSONに残す。

### before

| テスト | 秒 |
|---|---:|
| tests.test_underground_planning::test_underground_other_seeds_and_branches[101-underground_city] | 803.502 |
| tests.test_underground_planning::test_underground_other_seeds_and_branches[73-underground_city] | 750.667 |
| tests.test_town_planning::test_town_fixed_versions_and_formats[litematic-26.2] | 710.680 |
| tests.test_town_planning::test_town_fixed_versions_and_formats[litematic-1.21.11] | 704.685 |
| tests.test_town_planning::test_town_fixed_other_seeds[73] | 702.829 |
| tests.test_town_planning::test_town_fixed_other_seeds[101] | 702.293 |
| tests.test_underground_planning::test_underground_fixed_versions_formats[litematic-26.2-underground_city] | 701.930 |
| tests.test_town_planning::test_town_fixed_versions_and_formats[schem-26.1.2] | 701.345 |
| tests.test_town_planning::test_town_fixed_versions_and_formats[schem-26.3] | 700.565 |
| tests.test_town_planning::test_town_fixed_versions_and_formats[schem-26.2] | 699.797 |
| tests.test_town_planning::test_town_fixed_versions_and_formats[litematic-26.1.2] | 699.692 |
| tests.test_town_planning::test_town_fixed_versions_and_formats[litematic-26.3] | 699.473 |
| tests.test_town_planning::test_town_fixed_versions_and_formats[schem-1.21.11] | 698.165 |
| tests.test_underground_planning::test_underground_fixed_versions_formats[litematic-26.3-underground_city] | 694.400 |
| tests.test_castle_planning::test_castle_fixed_scales_and_seeds[huge-101] | 676.838 |
| tests.test_castle_planning::test_castle_fixed_scales_and_seeds[huge-73] | 676.630 |
| tests.test_castle_planning::test_castle_fixed_scales_and_seeds[huge-42] | 675.740 |
| tests.test_underground_planning::test_underground_other_seeds_and_branches[101-cave] | 670.296 |
| tests.test_underground_planning::test_underground_fixed_versions_formats[litematic-26.1.2-underground_city] | 661.492 |
| tests.test_underground_planning::test_underground_fixed_versions_formats[schem-1.21.11-underground_city] | 655.423 |
| tests.test_underground_planning::test_underground_fixed_versions_formats[schem-26.1.2-underground_city] | 654.580 |
| tests.test_underground_planning::test_underground_fixed_versions_formats[schem-26.2-underground_city] | 654.089 |
| tests.test_underground_planning::test_underground_fixed_versions_formats[litematic-1.21.11-underground_city] | 653.181 |
| tests.test_underground_planning::test_underground_fixed_versions_formats[schem-26.3-underground_city] | 652.174 |
| tests.test_underground_planning::test_underground_other_seeds_and_branches[73-cave] | 625.626 |
| tests.test_underground_planning::test_underground_fixed_versions_formats[litematic-26.2-cave] | 619.198 |
| tests.test_underground_planning::test_underground_fixed_versions_formats[litematic-26.3-cave] | 611.247 |
| tests.test_underground_planning::test_underground_fixed_versions_formats[litematic-1.21.11-cave] | 585.008 |
| tests.test_underground_planning::test_underground_fixed_versions_formats[schem-26.1.2-cave] | 584.895 |
| tests.test_underground_planning::test_underground_fixed_versions_formats[schem-26.2-cave] | 584.784 |

### after

| テスト | 秒 |
|---|---:|
| tests.test_town_planning::test_town_fixed_versions_and_formats[schem-26.2] | 280.340 |
| tests.test_town_planning::test_town_fixed_versions_and_formats[litematic-26.3] | 271.994 |
| tests.test_town_planning::test_town_fixed_versions_and_formats[schem-26.3] | 270.760 |
| tests.test_town_planning::test_town_fixed_versions_and_formats[litematic-26.1.2] | 270.348 |
| tests.test_town_planning::test_town_fixed_versions_and_formats[schem-26.1.2] | 268.457 |
| tests.test_town_planning::test_town_fixed_versions_and_formats[litematic-1.21.11] | 265.633 |
| tests.test_town_planning::test_town_fixed_versions_and_formats[litematic-26.2] | 264.237 |
| tests.test_castle_planning::test_towers_use_all_four_sides_when_they_fit[14] | 248.903 |
| tests.test_castle_planning::test_castle_fixed_scales_and_seeds[huge-101] | 237.387 |
| tests.test_castle_planning::test_castle_fixed_scales_and_seeds[huge-42] | 235.737 |
| tests.test_castle_planning::test_castle_fixed_scales_and_seeds[huge-73] | 232.351 |
| tests.test_town_planning::test_town_fixed_versions_and_formats[schem-1.21.11] | 230.009 |
| tests.test_castle_planning::test_towers_use_all_four_sides_when_they_fit[12] | 222.037 |
| tests.test_castle_planning::test_towers_use_all_four_sides_when_they_fit[11] | 203.888 |
| tests.test_town_planning::test_town_fixed_other_seeds[73] | 193.777 |
| tests.test_town_planning::test_town_emit_resume_matches_weather_output_quality_and_scene | 182.150 |
| tests.test_town_planning::test_town_fixed_other_seeds[101] | 181.427 |
| tests.test_castle_planning::test_castle_blueprint_resume_preserves_terrain_quality_and_scene | 98.045 |
| tests.test_underground_planning::test_underground_other_seeds_and_branches[73-underground_city] | 91.481 |
| tests.test_underground_planning::test_underground_other_seeds_and_branches[101-underground_city] | 91.315 |
| tests.test_town_planning::test_town_diverted_roads_preserve_buildings_and_reachability[hill-73] | 88.444 |
| tests.test_town_planning::test_town_diverted_roads_preserve_buildings_and_reachability[valley-3] | 88.269 |
| tests.test_underground_planning::test_underground_fixed_versions_formats[litematic-26.2-underground_city] | 87.805 |
| tests.test_underground_planning::test_underground_fixed_versions_formats[schem-26.3-underground_city] | 87.750 |
| tests.test_underground_planning::test_underground_fixed_versions_formats[litematic-26.1.2-underground_city] | 87.634 |
| tests.test_underground_planning::test_underground_fixed_versions_formats[litematic-1.21.11-underground_city] | 87.582 |
| tests.test_underground_planning::test_underground_fixed_versions_formats[schem-26.1.2-underground_city] | 87.293 |
| tests.test_underground_planning::test_underground_fixed_versions_formats[schem-26.2-underground_city] | 87.139 |
| tests.test_underground_planning::test_underground_fixed_versions_formats[litematic-26.3-underground_city] | 86.977 |
| tests.test_underground_planning::test_underground_fixed_versions_formats[schem-1.21.11-underground_city] | 86.501 |
