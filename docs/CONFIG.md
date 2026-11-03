# 設定 v1

設定は `schema/config.schema.json` を正本とし、wheel 同梱用コピーを `src/mcblueprint/schema/` に置きます。`config.yaml` の重複キー、未知キー、型・範囲違反は拒否されます。

## 優先順位

組込み既定値 → BuildSpec の `style` が参照する同梱様式の役割別材質候補 → 利用者設定（`%APPDATA%/mc-blueprint/config.yaml`。未設定時は `~/.config/mc-blueprint/config.yaml`）→ cwd の `mc-blueprint.yaml` → BuildSpec の `config`・`materials`・`seed` → CLI `--set`。未知の様式は利用可能な同梱様式名を示してエラーになります。

辞書は再帰的に合成し、配列とスカラー値は上位層で置換します。Python API `resolve_config` は `ResolvedConfig(values, origins)` を返し、各設定葉の出所も提供します。

```console
mcblueprint config show --set weathering.age=0.4
```

表示結果は `mcblueprint.buildspec.resolve_buildspec()` が返す合成設定と出所を JSON で出力します。#151以降の生成処理も同じ公開APIを呼びます。設定値のスキーマは `config.schema.json` を参照してください。
