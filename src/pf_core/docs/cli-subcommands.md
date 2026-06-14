# CLI Subcommand Factories

Typer subcommand factories that wrap `pf_core.pipeline.{baseline, baseline_diff, cache}` so consumers building a Typer CLI on top of those modules don't have to write the same boilerplate command bodies for every pipeline-style project.

Promoted from a document-extraction pipeline's `cli/_invalidate.py` and `cli/_baseline.py`. Consumer-specific bindings (filenames, dir names) are parameterized via [`BaselineConfig`](pipeline.md#baselineconfig) and the `run_record_filename` keyword on the invalidate factory.

## When to use

- You have a Typer CLI in your project (built with [`pf_core.cli.create_cli`](cli.md)) AND you adopted the [`pf_core.pipeline.*`](pipeline.md) machinery for baseline / cache / re-run ergonomics.
- You'd otherwise write the same `--label`, `--show-section`, `--source-stem` Typer wiring that the first pipeline consumer wrote, that the second would write, and that any future pipeline consumer would write.

## When NOT to use

- Your CLI is not Typer-based (the factories return Typer wiring directly).
- Your project doesn't use `pf_core.pipeline` at all — there's nothing for these factories to wrap.

## Usage

```python
# In your project's app/cli/__init__.py:
from pf_core.cli import create_cli, run_cli
from pf_core.cli.subcommands import (
    make_baseline_subcommand_group,
    make_invalidate_subcommand,
)
from pf_core.pipeline.baseline import BaselineConfig
from app.pipeline import REGISTRY  # your project's StageRegistry

app = create_cli("myapp", help="My application CLI.")

# Custom config if your project uses non-default filenames:
my_config = BaselineConfig(
    run_record_filename=".myapp-run.json",
    consolidated_md_pattern="{stem}.md",
)

make_baseline_subcommand_group(app, config=my_config)
make_invalidate_subcommand(
    app, registry=REGISTRY, run_record_filename=".myapp-run.json",
)

def main():
    run_cli(app)
```

That gives the user:

```
myapp baseline save <output_dir> [--label <label>]
myapp baseline list <output_dir>
myapp baseline diff <output_dir> <label> [--show-section <path>] [--show-consolidated]
myapp invalidate <output_dir> <stage> [--source-stem <stem>]
```

Both factories register on the caller's existing `app` — pass the same Typer instance to multiple factories to compose a richer CLI.

## make_baseline_subcommand_group

```python
make_baseline_subcommand_group(
    app: typer.Typer,
    *,
    config: BaselineConfig = DEFAULT_CONFIG,
    command_name: str = "baseline",
) -> None
```

Registers a Typer subcommand group on `app` with three subcommands:

| Subcommand | Wraps | Notes |
|---|---|---|
| `save` | [`save_baseline`](pipeline.md#baseline) | Optional `--label`. Default label: `<version>-<YYYYMMDD-HHMMSS>` |
| `list` | [`list_baselines`](pipeline.md#baseline) | Tabular output with version, preset, sections, images |
| `diff` | [`diff_baseline`](pipeline.md#baseline_diff) | `--show-section <path>` and `--show-consolidated` for drill-in unified diffs |

| Parameter | Default | Description |
|---|---|---|
| `app` | required | Typer app to register on |
| `config` | `DEFAULT_CONFIG` | Filename / directory conventions for the underlying pipeline calls |
| `command_name` | `"baseline"` | Subcommand group name (in case you want a different verb) |

## make_invalidate_subcommand

```python
make_invalidate_subcommand(
    app: typer.Typer,
    *,
    registry: StageRegistry,
    run_record_filename: str = "run.json",
    command_name: str = "invalidate",
) -> None
```

Registers a single command that calls [`invalidate_caches`](pipeline.md#invalidate_caches) per the cascade rule.

The registered command takes:

- `output_dir` (positional Path)
- `stage` (positional str — must be one of `registry.stage_names`)
- `--source-stem <stem>` (optional; inferred from `<output_dir>/<run_record_filename>`'s `input` field if omitted)

| Parameter | Default | Description |
|---|---|---|
| `app` | required | Typer app to register on |
| `registry` | required | Your project's `StageRegistry` describing the pipeline's stages |
| `run_record_filename` | `"run.json"` | Used to infer `source_stem` when omitted |
| `command_name` | `"invalidate"` | CLI command name |

## See also

- [`cli.md`](cli.md) — `create_cli` and `run_cli` for the surrounding Typer scaffolding
- [`pipeline.md`](pipeline.md) — the underlying functions these factories wrap
