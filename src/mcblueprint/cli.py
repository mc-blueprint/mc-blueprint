"""Command-line entry point for mc-blueprint (see docs/ARCHITECTURE.md section 11).

mcblueprint validate <blueprint.json> [--strict] [--max-dimension N] [--minecraft-version V]
                                      [--json] [--kind KIND ...] [--path PATH ...]
                                      [--limit N] [--offset N]
mcblueprint build    <blueprint.json> [-o DIR|FILE] [--format schem] [--seed N]
                                      [--strict] [--max-dimension N] [--minecraft-version V]
                                      [--region-size X Y Z]
mcblueprint inspect  <blueprint.json> [--json] [--max-dimension N] [--minecraft-version V]
mcblueprint stats    <blueprint.json> [--json] [--seed N] [--max-dimension N]
mcblueprint preview  <blueprint.json> [-o DIR] [--views top,north,east,isometric] [--scale N]
                                      [--layer Y ...] [--layers A..B|all] [--grid N]
                                      [--from X Y Z --to X Y Z]
                                      [--seed N] [--max-dimension N] [--minecraft-version V]
mcblueprint import   <file.schem|.litematic> [-o FILE] [--name NAME] [--minecraft-version V]
                                      [--component] [--description TEXT]
mcblueprint diff     <a.json> <b.json> [--json] [--max-dimension N]
mcblueprint traverse <blueprint.json> (--building INDEX | --start X Y Z --end X Y Z) [--json]
mcblueprint versions [--json]
mcblueprint check    [PATH ...] [--strict] [--minecraft-version V]
mcblueprint components [--json] [--minecraft-version V]
mcblueprint design   <file.schem|.litematic|.json> [-o FILE] [--name NAME] [--part NAME=FILE ...]
                                      [--reference] [--no-views] [--no-parts]
                                      [--minecraft-version V]

``--minecraft-version`` overrides the blueprint's ``minecraftVersion`` (block data to
check against and the DataVersion written to the output).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from hashlib import sha256
from pathlib import Path
from typing import Any, TextIO

from mcblueprint import __version__, blockdata, components
from mcblueprint.analysis import analyze
from mcblueprint.checking import (
    DEFAULT_TARGETS,
    check_component,
    check_paths,
    check_preset,
    list_components,
)
from mcblueprint.design_preset import PartInfo, render_preset
from mcblueprint.diagnostics import validation_report
from mcblueprint.diffing import diff_volumes, format_diff, normalize
from mcblueprint.errors import BlueprintError, ValidationError
from mcblueprint.exporters import EXPORTERS
from mcblueprint.generator import generate, generate_with_report
from mcblueprint.importers import (
    ImportedSchematic,
    read_schematic,
    to_blueprint,
    to_component,
    version_for_data_version,
)
from mcblueprint.loader import read_blueprint_json
from mcblueprint.model.blueprint import Blueprint
from mcblueprint.model.vec import AABB, Vec3
from mcblueprint.operations.base import Operation
from mcblueprint.operations.building import BuildingOperation
from mcblueprint.operations.nested import NestedOperation
from mcblueprint.pattern_checking import check_patterns
from mcblueprint.pipeline import GENERATION, generated_blueprint_regions
from mcblueprint.preview import (
    DEFAULT_VIEWS,
    VIEWS,
    selected_bounds,
    uncoloured_blocks,
    write_previews,
)
from mcblueprint.preview import Options as PreviewOptions
from mcblueprint.region_export import export_regions
from mcblueprint.support import check_support, format_warnings
from mcblueprint.surface_path import SurfacePathError
from mcblueprint.traversal import check_building, check_route
from mcblueprint.validator import DEFAULT_MAX_DIMENSION, format_errors, validate, validate_and_load
from mcblueprint.volume import BlockVolume

EXIT_OK = 0
EXIT_VALIDATION_ERROR = 1
EXIT_USAGE_ERROR = 2

DEFAULT_OUTPUT_DIR = Path("output")
DEFAULT_PREVIEW_DIR = Path("preview")
DEFAULT_BLUEPRINTS_DIR = Path("blueprints")
AIR_ID = "minecraft:air"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcblueprint",
        description="Build Minecraft schematics from Blueprint JSON.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    validate_parser = subparsers.add_parser(
        "validate", help="check a blueprint and report every problem"
    )
    _add_common_arguments(validate_parser)
    _add_strict_argument(validate_parser)
    validate_parser.add_argument("--json", action="store_true", help="構造化した検証結果を出力")
    validate_parser.add_argument(
        "--kind", choices=("schema", "semantic", "support"), action="append", default=[],
        help="表示する検査の種類（複数指定可）",
    )
    validate_parser.add_argument(
        "--path", action="append", default=[], help="表示するJSONパスの前方一致（複数指定可）",
    )
    validate_parser.add_argument(
        "--limit", type=int, default=None, help="表示上限（0=全件、JSON既定50）",
    )
    validate_parser.add_argument("--offset", type=int, default=0, help="一致した結果の開始位置")

    build_parser_ = subparsers.add_parser(
        "build", help="validate a blueprint and write the schematic"
    )
    _add_common_arguments(build_parser_)
    _add_strict_argument(build_parser_)
    build_parser_.add_argument(
        "-o",
        "--output",
        default=None,
        help="output file, or directory to place <name>.<ext> in (default: output/)",
    )
    build_parser_.add_argument(
        "--format",
        choices=sorted(EXPORTERS),
        default="schem",
        help="output format (default: schem)",
    )
    build_parser_.add_argument(
        "--region-size", nargs=3, type=int, metavar=("X", "Y", "Z"),
        help="全生成後に指定寸法で分割し、新規ディレクトリへ manifest と領域を出力",
    )
    _add_seed_argument(build_parser_)

    inspect_parser = subparsers.add_parser(
        "inspect", help="show a blueprint's metadata and bounds without generating it"
    )
    _add_common_arguments(inspect_parser)
    inspect_parser.add_argument("--json", action="store_true", help="machine-readable output")

    stats_parser = subparsers.add_parser(
        "stats", help="generate a blueprint and count blocks by state"
    )
    _add_common_arguments(stats_parser)
    stats_parser.add_argument("--json", action="store_true", help="machine-readable output")
    _add_seed_argument(stats_parser)

    preview_parser = subparsers.add_parser(
        "preview", help="render PNG previews (needs the optional Pillow dependency)"
    )
    _add_common_arguments(preview_parser)
    preview_parser.add_argument(
        "-o", "--output", default=None, help="directory for the images (default: preview/)"
    )
    preview_parser.add_argument(
        "--views",
        default=None,
        help=f"comma-separated views from {', '.join(VIEWS)} "
        f"(default: {','.join(DEFAULT_VIEWS)}, or none when layers are requested)",
    )
    preview_parser.add_argument(
        "--scale", type=int, default=8, help="pixels per block (default: 8)"
    )
    preview_parser.add_argument(
        "--layer",
        type=int,
        action="append",
        default=[],
        metavar="Y",
        help="horizontal section at this y (repeatable), written as <name>-y<NN>.png",
    )
    preview_parser.add_argument(
        "--layers",
        default=None,
        metavar="A..B|all",
        help="horizontal sections for a range of y levels, or every level",
    )
    preview_parser.add_argument(
        "--grid",
        type=int,
        default=None,
        metavar="N",
        help="draw heavier grid lines with coordinates every N blocks and mark the origin",
    )
    preview_parser.add_argument(
        "--from", dest="region_from", nargs=3, type=int, metavar=("X", "Y", "Z"),
        help="preview region minimum in blueprint coordinates (requires --to)",
    )
    preview_parser.add_argument(
        "--to", dest="region_to", nargs=3, type=int, metavar=("X", "Y", "Z"),
        help="preview region maximum, inclusive (requires --from)",
    )
    _add_seed_argument(preview_parser)

    import_parser = subparsers.add_parser(
        "import", help="convert a .schem / .litematic file into a Blueprint JSON"
    )
    import_parser.add_argument("schematic", type=Path, help="path to the .schem / .litematic")
    import_parser.add_argument(
        "-o", "--output", default=None, help="output JSON (default: blueprints/<name>.json)"
    )
    import_parser.add_argument("--name", default=None, help="blueprint name (default: file stem)")
    import_parser.add_argument(
        "--component",
        action="store_true",
        help="write a component file (components/<name>.json) with the minimum corner as origin",
    )
    import_parser.add_argument(
        "--description",
        default=None,
        help="description stored in the output (default: source file)",
    )
    import_parser.add_argument(
        "--minecraft-version",
        default=None,
        help="target version when it cannot be derived from the file's DataVersion",
    )

    versions_parser = subparsers.add_parser(
        "versions", help="list the Minecraft versions with bundled block data"
    )
    versions_parser.add_argument("--json", action="store_true", help="machine-readable output")

    check_parser = subparsers.add_parser(
        "check", help="validate design presets, components, or pattern libraries"
    )
    check_parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="files or directories (default: designs/ and components/ in the current directory)",
    )
    _add_strict_argument(check_parser)
    check_parser.add_argument(
        "--minecraft-version",
        default=None,
        metavar="V",
        help="block data to check against (default: the oldest bundled version)",
    )

    design_parser = subparsers.add_parser(
        "design",
        help="write a design preset (designs/local/<name>.md) inferred from an existing build",
    )
    design_parser.add_argument(
        "source", type=Path, help="a .schem / .litematic file or a Blueprint JSON"
    )
    design_parser.add_argument(
        "-o", "--output", default=None, help="output markdown (default: designs/local/<name>.md)"
    )
    design_parser.add_argument("--name", default=None, help="preset name (default: file stem)")
    design_parser.add_argument(
        "--part",
        action="append",
        default=[],
        metavar="NAME=FILE",
        help="also convert a part (.schem / .litematic) into components/NAME.json (repeatable)",
    )
    design_parser.add_argument(
        "--reference",
        action="store_true",
        help="also write the whole build as components/<name>_reference.json",
    )
    design_parser.add_argument(
        "--no-views", action="store_true", help="omit the plan and elevation drawings"
    )
    design_parser.add_argument(
        "--no-parts",
        action="store_true",
        help="do not cut decorated windows / doors out of the build as components",
    )
    design_parser.add_argument(
        "--minecraft-version",
        default=None,
        metavar="V",
        help="block data to use when the file's DataVersion has none (default: nearest)",
    )

    components_parser = subparsers.add_parser(
        "components", help="list the components on the search path with their size"
    )
    components_parser.add_argument("--json", action="store_true", help="machine-readable output")
    components_parser.add_argument(
        "--minecraft-version",
        default=None,
        metavar="V",
        help="block data to check against (default: the oldest bundled version)",
    )

    diff_parser = subparsers.add_parser(
        "diff", help="compare the generated output of two blueprints"
    )
    diff_parser.add_argument("blueprint", type=Path, help="the first (old) Blueprint JSON")
    diff_parser.add_argument("other", type=Path, help="the second (new) Blueprint JSON")
    diff_parser.add_argument("--json", action="store_true", help="machine-readable output")
    diff_parser.add_argument(
        "--max-dimension",
        type=int,
        default=DEFAULT_MAX_DIMENSION,
        metavar="N",
        help=f"maximum size per axis in blocks (default: {DEFAULT_MAX_DIMENSION})",
    )
    traversal_parser = subparsers.add_parser(
        "traverse", help="明示した対象の限定モデルによる通行を検査する"
    )
    _add_common_arguments(traversal_parser)
    _add_seed_argument(traversal_parser)
    target = traversal_parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--building", type=int, metavar="INDEX",
                        help="トップレベルoperationsのbuilding index（0始まり）")
    target.add_argument("--start", type=int, nargs=3, metavar=("X", "Y", "Z"),
                        help="開始する立位の足元セル")
    traversal_parser.add_argument("--end", type=int, nargs=3, metavar=("X", "Y", "Z"),
                                  help="終了する立位の足元セル（--startと同時指定）")
    traversal_parser.add_argument("--json", action="store_true", help="機械可読の検査結果")
    config_parser = subparsers.add_parser("config", help="設定を表示する")
    config_subparsers = config_parser.add_subparsers(dest="config_command", required=True)
    config_show = config_subparsers.add_parser("show", help="合成済み設定と値の出所を表示")
    config_show.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    make_parser = subparsers.add_parser(
        "make", help="BuildSpecから生成・評価・出力する",
        description="実測評価とrun記録を出力。終了コード: 0=FAILなし、1=検証・構造、"
                    "2=引数・I/O・資源不足、3=品質不合格（成果物を保存）。",
    )
    make_parser.add_argument("spec", type=Path)
    make_parser.add_argument("-o", "--output", help="出力先ファイルまたはディレクトリ")
    make_parser.add_argument("--format", choices=tuple(EXPORTERS))
    make_parser.add_argument("--seed", type=int)
    make_parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    make_parser.add_argument("--emit", action="append",
                             choices=("resolve", "site", "massing", "detail", "blueprint"))
    make_parser.add_argument("--resume", type=Path, help="検証付き中間ファイルから再開")
    subparsers.add_parser("archetypes", help="対応アーキタイプを1行1件で表示")
    template = subparsers.add_parser("spec-template", help="コメント付きBuildSpec雛形")
    template.add_argument("archetype", choices=("house", "tower"))
    template.add_argument("--minecraft-version", required=True,
                          choices=blockdata.supported_versions())
    help_parser = subparsers.add_parser("help", help="必要な仕様の参照先を表示")
    help_parser.add_argument("topic", choices=("buildspec", "operations", "materials", "resume"))
    view_parser = subparsers.add_parser("view", help="実入力を認証付きローカルviewerで表示する")
    view_parser.add_argument("input", type=Path)
    view_parser.add_argument("-o", "--output")
    view_parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    view_parser.add_argument("--minecraft-version")
    _add_render_parser(subparsers)
    edits_parser = subparsers.add_parser(
        "apply-edits", help="編集を保存する（再生成しない）",
        description="editsを検証・保存する。--specは既定で原本を更新する。"
                    "--inputは置換・削除だけを派生Blueprintへ保存し原本を保つ。再生成は行わない。",
    )
    edits_parser.add_argument("edits", type=Path)
    source = edits_parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--spec", type=Path)
    source.add_argument("--input", type=Path)
    edits_parser.add_argument("--scene", type=Path, required=True)
    edits_parser.add_argument("-o", "--output", type=Path)
    edits_parser.add_argument("--seed", type=int, help="scene生成時と同じseedを指定する")
    edits_parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                              help="scene生成時と同じ設定の上書きを指定する")
    return parser


def _add_render_parser(subparsers) -> None:
    parser = subparsers.add_parser("render", help="同梱viewerで3D静止画を出力する")
    parser.add_argument("input", type=Path, nargs="?")
    parser.add_argument("--setup", action="store_true", help="対応Chromiumだけを取得する")
    parser.add_argument("--views", help="iso,front,top（既定は全3視点）")
    parser.add_argument("--out", type=Path, help="画像保存先（既定preview/）")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--minecraft-version")


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("blueprint", type=Path, help="path to the Blueprint JSON file")
    parser.add_argument(
        "--max-dimension",
        type=int,
        default=DEFAULT_MAX_DIMENSION,
        metavar="N",
        help=f"maximum size per axis in blocks (default: {DEFAULT_MAX_DIMENSION})",
    )
    parser.add_argument(
        "--minecraft-version",
        default=None,
        metavar="V",
        help="override the blueprint's minecraftVersion (see 'mcblueprint versions')",
    )


def _add_strict_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--strict",
        action="store_true",
        help="treat support warnings (unsupported lanterns, torches, ...) as errors",
    )


def _add_seed_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--seed", type=int, default=None, help="override the blueprint's palette seed"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return EXIT_USAGE_ERROR
    commands = {
        "validate": _run_validate,
        "traverse": _run_traverse,
        "build": _run_build,
        "inspect": _run_inspect,
        "stats": _run_stats,
        "preview": _run_preview,
        "import": _run_import,
        "diff": _run_diff,
        "versions": _run_versions,
        "check": _run_check,
        "components": _run_components,
        "design": _run_design,
        "config": _run_config,
        "make": _run_make,
        "archetypes": _run_archetypes,
        "spec-template": _run_spec_template,
        "help": _run_help,
        "view": _run_view,
        "render": _run_render,
        "apply-edits": _run_apply_edits,
    }
    try:
        paths = components.default_search_paths(getattr(args, "blueprint", None))
        with components.component_search_paths(paths):
            return commands[args.command](args, sys.stdout)
    except SurfacePathError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_VALIDATION_ERROR
    except BlueprintError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_USAGE_ERROR


def _run_render(args: argparse.Namespace, out: TextIO) -> int:
    from mcblueprint.assets import ensure_assets
    from mcblueprint.render import VIEWS, render, setup
    from mcblueprint.viewer_input import prepare_input

    if args.setup:
        if (args.input is not None or args.views is not None
                or args.out is not None or args.set or args.minecraft_version):
            raise BlueprintError("--setupは入力・描画オプションと併用できません")
        setup()
        return EXIT_OK
    if args.input is None:
        raise BlueprintError("入力を指定するか、初回は render --setup を実行してください")
    views = tuple((args.views if args.views is not None else "iso,front,top").split(","))
    if not views or len(set(views)) != len(views) or any(v not in VIEWS for v in views):
        raise BlueprintError("--viewsはiso,front,topから重複なしで指定してください")
    try:
        prepared = prepare_input(args.input, overrides=args.set,
                                 minecraft_version=args.minecraft_version)
        assets = ensure_assets(prepared.scene["minecraftVersion"], prepared.config["assets"])
        for path in render(prepared, assets, args.out or Path("preview"), views=views, out=out):
            print(f"Wrote {path.as_posix()}", file=out)
    except (OSError, ValueError) as exc:
        raise BlueprintError(f"render: {exc}") from exc
    return EXIT_OK


def _run_view(args: argparse.Namespace, out: TextIO) -> int:
    from mcblueprint.assets import ensure_assets
    from mcblueprint.viewer_input import prepare_input
    from mcblueprint.viewer_server import serve

    try:
        prepared = prepare_input(args.input, output=args.output, overrides=args.set,
                                 minecraft_version=args.minecraft_version)
        assets = ensure_assets(prepared.scene["minecraftVersion"], prepared.config["assets"])
        serve(prepared, assets, open_browser=prepared.config["preview"]["open_browser"], out=out)
    except (OSError, ValueError) as exc:
        raise BlueprintError(f"view: {exc}") from exc
    return EXIT_OK


def _run_apply_edits(args: argparse.Namespace, out: TextIO) -> int:
    from mcblueprint.viewer_input import apply_input_edits

    overrides = args.set.copy()
    if args.seed is not None:
        overrides.append(f"seed={args.seed}")
    notes = []
    try:
        path = apply_input_edits(args.edits, args.spec or args.input, args.scene,
                                 output=args.output, spec=args.spec is not None, notes=notes,
                                 overrides=overrides)
    except (OSError, ValueError) as exc:
        raise BlueprintError(f"apply-edits: {exc}") from exc
    print(f"Wrote {path.as_posix()}", file=out)
    for note in notes:
        print("メモ " + json.dumps(note, ensure_ascii=False), file=out)
    return EXIT_OK


def _run_config(args: argparse.Namespace, out: TextIO) -> int:
    from mcblueprint.buildspec import resolve_buildspec

    result = resolve_buildspec(overrides=args.set)
    print(
        json.dumps(
            {"config": result.config, "sources": result.origins}, ensure_ascii=False, indent=2
        ),
        file=out,
    )
    return EXIT_OK


def _run_make(args: argparse.Namespace, out: TextIO) -> int:
    from mcblueprint.pipeline import PipelineError, make, preview_make

    overrides = args.set.copy()
    if args.format is not None:
        overrides.append(f"output.format={args.format}")
    if args.seed is not None:
        overrides.append(f"seed={args.seed}")
    try:
        result = make(args.spec, output=args.output, emit=args.emit, resume=args.resume,
                      overrides=overrides)
    except PipelineError as exc:
        print(f"FAIL {exc} {args.spec}", file=out)
        return exc.code
    except (OSError, BlueprintError) as exc:
        print(f"FAIL Resolve: {exc} {args.spec}", file=out)
        cause = exc
        while cause.__cause__ is not None:
            cause = cause.__cause__
        return 2 if isinstance(cause, OSError) else 1
    for line in result.diagnostics:
        print(line, file=out)
    print(f"Wrote {result.output.as_posix()}", file=out)
    print(f"Run {result.record.as_posix()}", file=out)
    try:
        preview_make(result, Path(args.spec), overrides=overrides, out=out)
    except PipelineError as exc:
        print(f"FAIL {exc} {args.spec}", file=out)
        return exc.code
    return result.code


def _run_archetypes(args: argparse.Namespace, out: TextIO) -> int:
    print("house 住宅（1〜2階、gable/hip、最小内装）", file=out)
    print("tower 塔（round/square、階段・各階床・胸壁）", file=out)
    return EXIT_OK


def _run_spec_template(args: argparse.Namespace, out: TextIO) -> int:
    features = ("  floors: 2\n  floor_height: 4\n  roof: gable" if args.archetype == "house"
                else "  shape: round\n  floors: 3\n  floor_height: 4\n  battlement: true")
    print(f'''# 対象版を明示し、敷地は軒・胸壁を含む上限にする。
formatVersion: 1
minecraftVersion: "{args.minecraft_version}"
archetype: {args.archetype}
# medieval / japanese / modern
style: medieval
# tiny / small / medium / large / huge。size指定が優先。
scale: small
# size: [32, 32, 32]
seed: 42
site:
  terrain: none
  biome: plains
features:
{features}
# none / minimal。furnishedはDで対応。
interior: minimal
# 材質名または固定ブロックIDで指定する。
# materials:
#   wall_main: minecraft:stone_bricks
#   frame: minecraft:oak_log
# 既知ルートIDのパラメータだけ編集できる。
# elements:
#   {args.archetype}#1:
#     angle: 90
# config:
#   weathering:
#     age: 0.4''', file=out)
    return EXIT_OK


def _run_help(args: argparse.Namespace, out: TextIO) -> int:
    topics = {
        "buildspec": ("docs/BUILDSPEC.md", "語彙・寸法・要素編集"),
        "operations": ("docs/OPERATIONS.md", "Operation一覧・引数・組合せ"),
        "materials": ("docs/OPERATIONS.md#materialize-前の役割入力", "材質・配色"),
        "resume": ("docs/BUILDSPEC.md#中間出力と再開", "hash・engine・データ検証"),
    }
    path, description = topics[args.topic]
    print(description, file=out)
    print("https://github.com/mc-blueprint/mc-blueprint/blob/develop/" + path, file=out)
    return EXIT_OK


def _run_validate(args: argparse.Namespace, out: TextIO) -> int:
    if args.offset < 0 or (args.limit is not None and args.limit < 0):
        raise BlueprintError("--limit と --offset は0以上を指定してください")
    data = _read_blueprint(args)
    blueprint, errors = validate_and_load(
        data, max_dimension=args.max_dimension, base_dir=args.blueprint.parent,
    )
    if args.json or args.kind or args.path or args.limit is not None or args.offset:
        warnings = []
        if not errors:
            assert blueprint is not None
            try:
                warnings = check_support(generate(blueprint))
            except SurfacePathError as exc:
                errors = [
                    ValidationError("surfacePath", issue.reason, value=issue.position.to_list())
                    for issue in exc.issues
                ]
        report = validation_report(
            args.blueprint.as_posix(), errors, warnings, strict=args.strict,
            kinds=args.kind, paths=args.path,
            limit=args.limit if args.limit is not None else (50 if args.json else 0),
            offset=args.offset,
        )
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), file=out)
        else:
            for item in report["diagnostics"]:
                target = item["path"] if item["path"] is not None else item["position"]
                print(f"{item['severity'].upper()} {target}: {item['message']}", file=out)
                if item["value"] is not None:
                    print(f"  Current value: {item['value']}", file=out)
            counts = report["counts"]
            print(
                f"{counts['errors']} error(s), {counts['warnings']} warning(s); "
                f"shown {counts['shown']}, omitted {counts['omitted']}, "
                f"filtered out {counts['filteredOut']}.", file=out,
            )
            if counts["omitted"] or counts["filteredOut"]:
                print(report["retrieval"]["hint"], file=out)
        return report["exitCode"]

    if errors:
        print(format_errors(errors), file=out)
        return EXIT_VALIDATION_ERROR
    assert blueprint is not None
    volume, foundation = generate_with_report(blueprint)
    print(format_errors(errors), file=out)
    if foundation is not None:
        print("Foundation: " + json.dumps(foundation.to_json(), ensure_ascii=False), file=out)
    if blueprint.surface_path is not None:
        print("SurfacePath: " + json.dumps(blueprint.surface_path.result.to_json()), file=out)
    return _report_warnings(volume, args.strict, out)


def _report_warnings(volume: BlockVolume, strict: bool, out: TextIO) -> int:
    """Print support warnings; exit code 1 only when ``strict`` and there are any."""
    warnings = check_support(volume)
    if not warnings:
        return EXIT_OK
    print("", file=out)
    print(format_warnings(warnings), file=out)
    return EXIT_VALIDATION_ERROR if strict else EXIT_OK


def _read_blueprint(args: argparse.Namespace) -> dict[str, Any]:
    """The blueprint JSON with ``--minecraft-version`` applied."""
    data = read_blueprint_json(args.blueprint)
    override = getattr(args, "minecraft_version", None)
    if override is not None and isinstance(data, dict):
        data["minecraftVersion"] = override
    return data


def _load_validated(
    args: argparse.Namespace, out: TextIO, *, data: dict[str, Any] | None = None,
) -> Blueprint | None:
    """Validate and load; prints the errors and returns None when invalid."""
    data = _read_blueprint(args) if data is None else data
    blueprint, errors = validate_and_load(
        data, max_dimension=args.max_dimension, base_dir=args.blueprint.parent,
    )
    if errors:
        print(format_errors(errors), file=out)
        return None
    return blueprint


def _run_build(args: argparse.Namespace, out: TextIO) -> int:
    data = _read_blueprint(args)
    metadata = data.get("metadata")
    generation = metadata.get("generation") if isinstance(metadata, dict) else None
    if isinstance(generation, dict) and generation.get("mode") == "buildspec-v1":
        from mcblueprint.pipeline import export_generated_blueprint

        blueprint = _load_validated(args, out, data=data)
        if blueprint is None:
            return EXIT_VALIDATION_ERROR
        if args.region_size is not None:
            raise BlueprintError("make由来Blueprintのbuildは単一出力に対応します")
        path = resolve_output_path(args.output, args.blueprint, EXPORTERS[args.format].extension)
        count, first = export_generated_blueprint(blueprint, path, format=args.format,
                                                 seed=args.seed, strict=args.strict)
        if count:
            print(f"WARNING support={count} {first}", file=out)
            if args.strict:
                print("Not written because of warnings (--strict).", file=out)
                return EXIT_VALIDATION_ERROR
        print(f"Wrote {path.as_posix()}", file=out)
        return EXIT_OK
    if args.region_size is not None and any(value <= 0 for value in args.region_size):
        raise BlueprintError("分割寸法は各軸とも正整数にしてください")
    input_hash = None
    if args.region_size is not None:
        try:
            raw = args.blueprint.read_bytes()
        except OSError as exc:
            raise BlueprintError(f"Cannot read blueprint file {args.blueprint}: {exc}") from exc
        input_hash = sha256(raw).hexdigest()
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise BlueprintError(f"Invalid JSON in {args.blueprint}: {exc}") from exc
        if not isinstance(data, dict):
            raise BlueprintError(f"Blueprint root must be a JSON object: {args.blueprint}")
        if args.minecraft_version is not None:
            data["minecraftVersion"] = args.minecraft_version
    blueprint = _load_validated(args, out, data=data)
    if blueprint is None:
        return EXIT_VALIDATION_ERROR
    volume, foundation = generate_with_report(blueprint, seed=args.seed)
    warnings = check_support(volume)
    if warnings:
        print(format_warnings(warnings), file=out)
        print("", file=out)
        if args.strict:
            print("Not written because of warnings (--strict).", file=out)
            return EXIT_VALIDATION_ERROR
    if args.region_size is not None:
        out_dir = (Path(args.output) if args.output
                   else DEFAULT_OUTPUT_DIR / (args.blueprint.stem + "-regions"))
        manifest = export_regions(
            volume, blueprint, out_dir, region_size=Vec3(*args.region_size),
            input_sha256=input_hash, format=args.format, seed=args.seed,
        )
        print(f"Wrote {manifest.as_posix()}", file=out)
    else:
        exporter = EXPORTERS[args.format]()
        out_path = resolve_output_path(args.output, args.blueprint, exporter.extension)
        exporter.export(volume, blueprint, out_path)

        bounds = volume.bounds()
        assert bounds is not None  # export() rejects empty volumes
        size = bounds.size
        print(f"Wrote {out_path.as_posix()}", file=out)
        print(f"Bounds: {bounds}  ({size.x} x {size.y} x {size.z})", file=out)
        print(f"Blocks: {len(volume)}", file=out)
    if foundation is not None:
        print("Foundation: " + json.dumps(foundation.to_json(), ensure_ascii=False), file=out)
    if blueprint.surface_path is not None:
        print("SurfacePath: " + json.dumps(blueprint.surface_path.result.to_json()), file=out)
    return EXIT_OK


def _run_inspect(args: argparse.Namespace, out: TextIO) -> int:
    blueprint = _load_validated(args, out)
    if blueprint is None:
        return EXIT_VALIDATION_ERROR
    info = inspect_blueprint(blueprint)
    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2), file=out)
        return EXIT_OK
    b = info["bounds"]
    s = info["size"]
    lines = [
        f"Name: {info['name']}",
        f"Minecraft: {info['minecraftVersion']}",
        "",
        "Bounds:",
        f"X: {b['min'][0]} .. {b['max'][0]}",
        f"Y: {b['min'][1]} .. {b['max'][1]}",
        f"Z: {b['min'][2]} .. {b['max'][2]}",
        f"Size: {s[0]} x {s[1]} x {s[2]}",
        "",
        f"Operations: {info['operations']} (top level: {info['topLevelOperations']})",
        f"Palettes: {info['palettes']}",
        f"Seed: {info['seed']}",
        f"Origin: {info['origin']}",
    ]
    if info["declaredSize"] is not None:
        lines.append(f"Declared size: {info['declaredSize']}")
    if info["description"]:
        lines.append(f"Description: {info['description']}")
    print("\n".join(lines), file=out)
    return EXIT_OK


def inspect_blueprint(blueprint: Blueprint) -> dict[str, Any]:
    """Metadata and analytic bounds (no generation) as a JSON-friendly dict."""
    bounds = blueprint.bounds()
    return {
        "name": blueprint.name,
        "minecraftVersion": blueprint.minecraft_version,
        "description": blueprint.description,
        "author": blueprint.author,
        "seed": blueprint.seed,
        "origin": blueprint.origin.to_list(),
        "declaredSize": blueprint.size.to_list() if blueprint.size else None,
        "bounds": {"min": bounds.min.to_list(), "max": bounds.max.to_list()},
        "size": bounds.size.to_list(),
        "topLevelOperations": len(blueprint.operations),
        "operations": count_operations(blueprint.operations),
        "palettes": len(blueprint.palettes),
    }


def count_operations(operations: Sequence[Operation]) -> int:
    """Number of operations including those nested in mirror / repeat."""
    total = 0
    for op in operations:
        total += 1
        if isinstance(op, NestedOperation):
            total += count_operations(op.operations)
    return total


def _run_traverse(args: argparse.Namespace, out: TextIO) -> int:
    if (args.start is None) != (args.end is None):
        raise BlueprintError("--start と --end は同時に指定してください")
    if args.building is not None and args.end is not None:
        raise BlueprintError("--building と --end は同時指定できません")
    blueprint = _load_validated(args, out)
    if blueprint is None:
        return EXIT_VALIDATION_ERROR
    selected = None
    if args.building is not None:
        index = args.building
        if index < 0 or index >= len(blueprint.operations):
            raise BlueprintError("--building の index が operations の範囲外です")
        selected = blueprint.operations[index]
        if not isinstance(selected, BuildingOperation):
            raise BlueprintError("--building はトップレベルの building を指定してください")
    volume = generate(blueprint, seed=args.seed)
    if selected is not None:
        results = check_building(volume, selected.layout, blueprint.minecraft_version)
    else:
        results = (check_route(volume, Vec3.from_seq(args.start), Vec3.from_seq(args.end),
                               blueprint.minecraft_version),)
    if args.json:
        print(json.dumps({"movementModel": "walk-straight-v1",
                          "results": [r.to_dict() for r in results]},
                         ensure_ascii=False, indent=2), file=out)
    else:
        print("限定モデルの検査結果（実機全動作の保証ではありません）", file=out)
        for result in results:
            print(f"{result.status}: {result.start} → {result.end}", file=out)
            for issue in result.issues[:8]:
                print(f"  {issue.position}: {issue.reason} ({issue.block})", file=out)
    return EXIT_OK if all(r.status == "reachable" for r in results) else EXIT_VALIDATION_ERROR


def _run_stats(args: argparse.Namespace, out: TextIO) -> int:
    blueprint = _load_validated(args, out)
    if blueprint is None:
        return EXIT_VALIDATION_ERROR
    volume = generate(blueprint, seed=args.seed)
    info = stats_volume(volume)
    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2), file=out)
        return EXIT_OK
    print(f"Total blocks: {info['totalBlocks']} (air: {info['airBlocks']})", file=out)
    bounds = info["bounds"]
    if bounds is not None:
        s = info["size"]
        print(
            f"Bounds: X {bounds['min'][0]}..{bounds['max'][0]}  "
            f"Y {bounds['min'][1]}..{bounds['max'][1]}  "
            f"Z {bounds['min'][2]}..{bounds['max'][2]}  ({s[0]} x {s[1]} x {s[2]})",
            file=out,
        )
    print("", file=out)
    counts = info["blocks"]
    width = max((len(str(n)) for n in counts.values()), default=1)
    for state, count in counts.items():
        print(f"{count:>{width}}  {state}", file=out)
    return EXIT_OK


def stats_volume(volume: BlockVolume) -> dict[str, Any]:
    """Block counts (air excluded from the list and total) as a JSON-friendly dict."""
    counts = volume.count_by_state()
    air = counts.pop(AIR_ID, 0)
    bounds = volume.bounds()
    return {
        "totalBlocks": len(volume) - air,
        "airBlocks": air,
        "bounds": (
            None if bounds is None else {"min": bounds.min.to_list(), "max": bounds.max.to_list()}
        ),
        "size": None if bounds is None else bounds.size.to_list(),
        "blocks": counts,
    }


def _parse_layers(spec: str | None, bounds: AABB) -> list[int]:
    """``--layers`` value: ``all``, ``A..B`` (either end may be omitted) or a single y."""
    if spec is None:
        return []
    lo, hi = bounds.min.y, bounds.max.y
    if spec == "all":
        return list(range(lo, hi + 1))
    try:
        if ".." in spec:
            a, b = spec.split("..", 1)
            start, end = (int(a) if a else lo), (int(b) if b else hi)
        else:
            start = end = int(spec)
    except ValueError:
        raise BlueprintError(
            f"--layers must be 'all', 'A..B' or a y level (got {spec!r})"
        ) from None
    if start > end:
        start, end = end, start
    return list(range(start, end + 1))


def _run_preview(args: argparse.Namespace, out: TextIO) -> int:
    want_layers = bool(args.layer) or args.layers is not None
    if args.views is None:
        views = [] if want_layers else list(DEFAULT_VIEWS)
    else:
        views = [v.strip() for v in args.views.split(",") if v.strip()]
    unknown = [v for v in views if v not in VIEWS]
    if unknown or (not views and not want_layers):
        raise BlueprintError(
            f"Unknown view(s): {', '.join(unknown) or '(none)'}; choose from {', '.join(VIEWS)}"
        )
    if args.scale < 1:
        raise BlueprintError("--scale must be >= 1")
    if args.grid is not None and args.grid < 1:
        raise BlueprintError("--grid must be >= 1")
    if (args.region_from is None) != (args.region_to is None):
        raise BlueprintError("--from and --to must be specified together")
    try:
        region = (
            AABB(Vec3.from_seq(args.region_from), Vec3.from_seq(args.region_to))
            if args.region_from is not None else None
        )
    except ValueError as exc:
        raise BlueprintError(f"Invalid preview region: {exc}") from None
    blueprint = _load_validated(args, out)
    if blueprint is None:
        return EXIT_VALIDATION_ERROR
    generation = (blueprint.metadata or {}).get("generation", {})
    if isinstance(generation, dict) and generation.get("mode") == GENERATION:
        volume = BlockVolume()
        for generated_region in generated_blueprint_regions(blueprint, seed=args.seed):
            for pos, state in generated_region.volume:
                if generated_region.core.contains(pos):
                    volume.set(pos, state, generated_region.volume.attributes(pos))
    else:
        volume = generate(blueprint, seed=args.seed)
    bounds = volume.bounds()
    if bounds is None:
        raise BlueprintError("Nothing to preview: no blocks were generated")
    bounds = selected_bounds(bounds, region)
    layers = sorted(set(args.layer) | set(_parse_layers(args.layers, bounds)))
    outside = [y for y in layers if not bounds.min.y <= y <= bounds.max.y]
    if outside:
        raise BlueprintError(
            f"Layer(s) {', '.join(map(str, outside))} are outside the preview bounds "
            f"(y {bounds.min.y}..{bounds.max.y})"
        )
    missing = uncoloured_blocks(volume, blueprint.minecraft_version)
    if missing:
        print(
            f"WARNING: no blockdata face colour for {', '.join(missing)} "
            "(using plains water tint or same-material median)",
            file=out,
        )
    options = PreviewOptions(
        scale=args.scale, grid=args.grid, origin=blueprint.origin,
        region=region, source=str(args.blueprint.resolve()), version=blueprint.minecraft_version,
    )
    out_dir = Path(args.output) if args.output else DEFAULT_PREVIEW_DIR
    for path in write_previews(
        volume, out_dir, args.blueprint.stem, views, layers=layers, options=options
    ):
        print(f"Wrote {path.as_posix()}", file=out)
    return EXIT_OK


def _run_import(args: argparse.Namespace, out: TextIO) -> int:
    imported = read_schematic(args.schematic)
    version = args.minecraft_version or version_for_data_version(imported.data_version)
    if version is None:
        known = ", ".join(blockdata.supported_versions())
        if imported.data_version is None:
            raise BlueprintError(
                f"The file has no DataVersion; pass --minecraft-version (supported: {known})"
            )
        version = blockdata.nearest_version(imported.data_version)
        print(
            f"Note: DataVersion {imported.data_version} has no bundled block data; using "
            f"{version} (DataVersion {blockdata.data_version(version)}). "
            f"Pass --minecraft-version to choose another (supported: {known}).",
            file=out,
        )
    name = args.name or imported.name or args.schematic.stem
    description = args.description or f"Imported from {args.schematic.name}"
    if args.component:
        if not components.NAME_PATTERN.match(name):
            raise BlueprintError(f"Component name {name!r} must use a-z, 0-9 and _ (pass --name)")
        data = to_component(imported, name=name, minecraft_version=version, description=description)
        default_path = Path(components.COMPONENTS_DIR) / f"{name}.json"
    else:
        data = to_blueprint(imported, name=name, minecraft_version=version, description=description)
        default_path = DEFAULT_BLUEPRINTS_DIR / f"{args.schematic.stem}.json"
    out_path = Path(args.output) if args.output else default_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2)
    out_path.write_text(text + "\n", encoding="utf-8")
    bounds = imported.volume.bounds()
    assert bounds is not None
    size = bounds.size
    print(f"Wrote {out_path.as_posix()}", file=out)
    print(f"Blocks: {len(imported.volume)}  Operations: {len(data['operations'])}", file=out)
    print(f"Size: {size.x} x {size.y} x {size.z}  Minecraft: {version}", file=out)
    if args.component:
        result = check_component(out_path, version)
        if result.warnings:
            print("", file=out)
            print(format_warnings(result.warnings), file=out)
        if result.errors:
            print("", file=out)
            print("The component has validation errors (unknown blocks?):", file=out)
            print(format_errors(result.errors), file=out)
            return EXIT_VALIDATION_ERROR
        return EXIT_OK
    errors = validate(data)
    if errors:
        print("", file=out)
        print("The imported blueprint has validation errors (unknown blocks?):", file=out)
        print(format_errors(errors), file=out)
        return EXIT_VALIDATION_ERROR
    return EXIT_OK


def _run_check(args: argparse.Namespace, out: TextIO) -> int:
    default_paths = not args.paths
    paths = list(args.paths) or [Path(name) for name in DEFAULT_TARGETS if Path(name).is_dir()]
    if not paths:
        raise BlueprintError(
            "Nothing to check: give paths or run where designs/ or components/ exist"
        )
    results = []
    for path in paths:
        if path.name != "designs" and (path / "styles").is_dir():
            results.extend(check_patterns(path, args.minecraft_version))
        else:
            results.extend(check_paths([path], args.minecraft_version))
    if default_paths:
        results.extend(check_patterns(components.PATTERNS_DIR.parent, args.minecraft_version))
    errors = warnings = 0
    for result in results:
        if result.ok and not result.warnings:
            continue
        print(f"{result.path.as_posix()} ({result.kind})", file=out)
        for err in result.errors:
            print(_indent(err.format()), file=out)
        for warning in result.warnings:
            print(_indent(warning.format()), file=out)
        print("", file=out)
        errors += len(result.errors)
        warnings += len(result.warnings)
    noun = "file" if len(results) == 1 else "files"
    print(f"{len(results)} {noun} checked: {errors} error(s), {warnings} warning(s).", file=out)
    if errors or (warnings and args.strict):
        return EXIT_VALIDATION_ERROR
    return EXIT_OK


def _indent(text: str) -> str:
    return "\n".join("  " + line for line in text.splitlines())


def _run_components(args: argparse.Namespace, out: TextIO) -> int:
    results = list_components(components.search_paths(), args.minecraft_version)
    rows = [
        {
            "name": result.name,
            "path": result.path.as_posix(),
            "size": result.size.to_list() if result.size is not None else None,
            "description": result.description,
            "errors": len(result.errors),
            "warnings": len(result.warnings),
        }
        for result in results
    ]
    if args.json:
        json.dump({"components": rows}, out, indent=2, ensure_ascii=False)
        print("", file=out)
        return EXIT_OK
    if not rows:
        searched = ", ".join(p.as_posix() for p in components.search_paths())
        print(f"No components found (searched: {searched}).", file=out)
        return EXIT_OK
    width = max(len(row["name"]) for row in rows)
    for row in rows:
        size = "x".join(map(str, row["size"])) if row["size"] else "-"
        status = ""
        if row["errors"]:
            status = f"  [{row['errors']} error(s)]"
        elif row["warnings"]:
            status = f"  [{row['warnings']} warning(s)]"
        print(f"{row['name']:<{width}}  {size:>10}  {row['description'] or ''}{status}", file=out)
    return EXIT_OK


def _load_volume(path: Path, version_override: str | None, out: TextIO) -> tuple[BlockVolume, str]:
    """A schematic or a Blueprint JSON as a generated volume, plus the Minecraft version."""
    if path.suffix.lower() == ".json":
        data = read_blueprint_json(path)
        if version_override is not None and isinstance(data, dict):
            data["minecraftVersion"] = version_override
        blueprint, errors = validate_and_load(data, base_dir=path.parent)
        if errors:
            raise BlueprintError(f"{path} is not a valid blueprint:\n{format_errors(errors)}")
        assert blueprint is not None
        search = (*components.default_search_paths(path), *components.search_paths())
        with components.component_search_paths(search):
            return generate(blueprint), blueprint.minecraft_version
    imported = read_schematic(path)
    version = version_override or version_for_data_version(imported.data_version)
    if version is None:
        if imported.data_version is None:
            known = ", ".join(blockdata.supported_versions())
            raise BlueprintError(
                f"{path} has no DataVersion; pass --minecraft-version (supported: {known})"
            )
        version = blockdata.nearest_version(imported.data_version)
        print(f"Note: using block data of {version} for {path.name}.", file=out)
    return imported.volume, version


def _write_component_file(
    imported: ImportedSchematic, name: str, version: str, description: str, out: TextIO
) -> PartInfo:
    if not components.NAME_PATTERN.match(name):
        raise BlueprintError(f"Component name {name!r} must use a-z, 0-9 and _")
    data = to_component(imported, name=name, minecraft_version=version, description=description)
    path = Path(components.COMPONENTS_DIR) / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    result = check_component(path, version)
    for warning in result.warnings:
        print(_indent(warning.format()), file=out)
    for err in result.errors:
        print(_indent(err.format()), file=out)
    print(f"Wrote {path.as_posix()}", file=out)
    bounds = imported.volume.bounds()
    assert bounds is not None
    return PartInfo(name, bounds.size, description, path.as_posix())


def _run_design(args: argparse.Namespace, out: TextIO) -> int:
    name = args.name or args.source.stem
    if not components.NAME_PATTERN.match(name):
        raise BlueprintError(f"Preset name {name!r} must use a-z, 0-9 and _ (pass --name)")
    volume, version = _load_volume(args.source, args.minecraft_version, out)
    bounds = volume.bounds()
    if bounds is None:
        raise BlueprintError("Nothing to analyze: the source has no blocks")
    analysis = analyze(volume)
    parts: list[PartInfo] = []
    for spec in args.part:
        if "=" not in spec:
            raise BlueprintError(f"--part expects NAME=FILE (got {spec!r})")
        part_name, file_name = spec.split("=", 1)
        imported = read_schematic(Path(file_name))
        parts.append(
            _write_component_file(
                imported, part_name, version, f"Imported from {Path(file_name).name}", out
            )
        )
    if not args.no_parts:
        counters = {"window": 0, "door": 0}
        for part in analysis.parts:
            counters[part.kind] += 1
            suffix = part.kind if counters[part.kind] == 1 else f"{part.kind}{counters[part.kind]}"
            part_volume = BlockVolume()
            for pos, state in part.cells.items():
                part_volume.set(pos, state)
            label = "窓" if part.kind == "window" else "入口"
            o = part.opening
            description = (
                f"{args.source.name} の{label}とその周りの飾り（外側が -z、{label}は部品内 "
                f"[{o.min.x}, {o.min.y}, {o.min.z}]〜[{o.max.x}, {o.max.y}, {o.max.z}]、"
                f"元の建物に {part.count} 箇所）"
            )
            info = _write_component_file(
                ImportedSchematic(part_volume, Vec3(0, 0, 0), None, None),
                f"{name}_{suffix}",
                version,
                description,
                out,
            )
            info.kind, info.count = part.kind, part.count
            parts.append(info)
    reference = None
    if args.reference:
        imported = ImportedSchematic(volume, bounds.min, None, name)
        reference = _write_component_file(
            imported,
            f"{name}_reference",
            version,
            f"{args.source.name} の全体（design の参照用）",
            out,
        )
    text = render_preset(
        name,
        analysis,
        source=args.source.name,
        parts=parts,
        reference=reference,
        views=not args.no_views,
    )
    out_path = Path(args.output) if args.output else Path("designs") / "local" / f"{name}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    print(f"Wrote {out_path.as_posix()}", file=out)
    size = analysis.size
    roles = ", ".join(f"{role} {sum(c.values())}" for role, c in analysis.roles.items())
    print(f"Size: {size.x} x {size.y} x {size.z}  Roles: {roles}", file=out)
    result = check_preset(out_path, version)
    if result.errors:
        print("", file=out)
        print("The preset has validation errors:", file=out)
        for err in result.errors:
            print(_indent(err.format()), file=out)
        return EXIT_VALIDATION_ERROR
    return EXIT_OK


def _run_versions(args: argparse.Namespace, out: TextIO) -> int:
    rows = [
        {
            "version": version,
            "dataVersion": blockdata.data_version(version),
            "blocks": len(blockdata.load_block_data(version)),
        }
        for version in blockdata.supported_versions()
    ]
    if args.json:
        json.dump({"versions": rows}, out, indent=2)
        print("", file=out)
        return EXIT_OK
    width = max(len(row["version"]) for row in rows)
    print(f"{'Version':<{width}}  DataVersion  Blocks", file=out)
    for row in rows:
        print(f"{row['version']:<{width}}  {row['dataVersion']:>11}  {row['blocks']:>6}", file=out)
    return EXIT_OK


def _run_diff(args: argparse.Namespace, out: TextIO) -> int:
    volumes = []
    for path in (args.blueprint, args.other):
        data = read_blueprint_json(path)
        with components.component_search_paths(components.default_search_paths(path)):
            blueprint, errors = validate_and_load(
                data, max_dimension=args.max_dimension, base_dir=path.parent,
            )
            if errors:
                print(f"{path}:", file=out)
                print(format_errors(errors), file=out)
                return EXIT_VALIDATION_ERROR
            assert blueprint is not None
            volumes.append(normalize(generate(blueprint), blueprint))
    result = diff_volumes(volumes[0], volumes[1])
    if args.json:
        print(json.dumps(result.to_json(), ensure_ascii=False, indent=2), file=out)
    else:
        print(format_diff(result), file=out)
    return EXIT_OK


def resolve_output_path(output: str | None, blueprint_path: Path, extension: str) -> Path:
    """``-o`` semantics: none -> output/<stem><ext>; a directory -> inside it; else the file.

    ``output`` counts as a directory when it exists as one, ends with a path
    separator, or has no file extension.
    """
    default_name = blueprint_path.stem + extension
    if output is None:
        return DEFAULT_OUTPUT_DIR / default_name
    path = Path(output)
    if path.is_dir() or output.endswith(("/", "\\")) or not path.suffix:
        return path / default_name
    return path
