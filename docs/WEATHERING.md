# Weather（劣化・植生）

mcblueprint.weather.apply_weather は GeneratedRegion の生成直後の RegionVolume を読み、ブロック更新を一括で core へ反映する後処理です。generator・CLIとの接続は行わず、呼出側が各領域に適用します。分割生成には mcblueprint.weather.required_halo(config["weathering"]) を generate_regions の reference_radius に渡し、Weatherが読む範囲を確保してください。reference_radius指定時のhaloはその値になり、指定がない場合は limits.halo を使います。劣化・支持判定はhaloを含むsnapshotで行い、変更の確定はcoreだけです。全体boundsの外側は既知の空気として露出に数え、bounds内でhaloが不足する参照は露出とみなしません。

    apply_weather(
        region,
        load_block_data(version),
        seed=blueprint.seed,
        config=config["weathering"],
        ground_height=terrain_base_y,
        randomness=config["randomness"],
    )

ground_height は元地形または基準面のY座標です。一定の整数、または (x, z) からY座標を返すMappingを渡します。領域は参照に必要なhaloを含めて生成し、Weather後もcore外は変更しません。
max_memory_mb はWeatherの作業領域に使う上限（既定2048 MiB）です。Generateは生成とWeatherのセル当たり作業量の大きい方でcore寸法を縮小します。Weather単体も領域配列・距離場・候補保持の概算が上限を超える場合は処理を開始せずエラーになります。

makeでは保持中の計画・入力を差し引いた合計予算からGenerate/Weatherへ配賦します。
各段がそれぞれ設定上限の全額を使う扱いにはしません。配賦とRSS測定の区別は
[ARCHITECTURE.md](ARCHITECTURE.md) の領域生成の説明を参照してください。

## 処理規則

- 水距離は water_proximity.radius までの6近傍L1距離です。水・露出・支持判定は変更前volumeから読み取ります。
- 苔・ひびはblockdataの遷移先だけを使い、形状と元stateのpropertiesを保持できない遷移は適用しません。苔とひびが両方抽選された場合は確率が高い方を使い、同値なら苔を使います。
- 欠けは予約されていない外壁面だけをairへ置き換えます。植生は予約されていない支持ブロックに接する空気セルへ配置します。側面はvineまたはpersistentなoak leaves、地面上はshort grassまたはfernを使い、版のblockdataにない候補は除外します。
- circulation はairを含め変更しません。structure は形状・stateを保つひび遷移だけを許可します。
- 乱数はseed・段・要素ID・用途・世界座標から導出し、領域サイズや走査順に依存しません。
- age と各劣化量が0なら、その処理は乱数強度にかかわらず無効です。

config は docs/CONFIG.md の weathering オブジェクト、乱数強度は同設定の randomness を使います。呼出し前に設定schemaで検証してください。
戻り値は遷移先がブロックの形状・stateを保持できず適用しなかった場合の診断tupleです。該当がなければ空tupleになります。
