"""Tests for pf_core.cli.subcommands — Typer subcommand factories."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from pf_core.cli.subcommands import (
    make_baseline_subcommand_group,
    make_invalidate_subcommand,
)
from pf_core.pipeline.baseline import BaselineConfig
from pf_core.pipeline.cache import StageDefinition, StageRegistry

runner = CliRunner()

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _plain_output(output: str) -> str:
    """CliRunner output normalized for substring asserts.

    Typer renders usage errors through rich: an 80-col panel that wraps
    phrases across `│`-bordered lines, plus — when GITHUB_ACTIONS (or
    FORCE_COLOR) is set — ANSI color codes glued to the words. Strip
    escapes and box borders, collapse whitespace.
    """
    return " ".join(_ANSI_RE.sub("", output).replace("│", " ").split())


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def registry() -> StageRegistry:
    """A multi-stage pipeline; shared by all invalidate tests."""
    return StageRegistry(
        stages=(
            StageDefinition(
                "backend",
                structural_files=("{stem}.raw.md", "images"),
            ),
            StageDefinition(
                "extract",
                content_keyed_files=(".extract-cache",),
            ),
            StageDefinition(
                "cleanup",
                structural_files=("{stem}.post-cleanup.md",),
            ),
            StageDefinition(
                "normalize",
                structural_files=("{stem}.pre-normalize.md",),
                content_keyed_files=(".normalize-cache",),
            ),
            StageDefinition(
                "split",
                structural_files=("sections", "INDEX.md"),
            ),
        )
    )


@pytest.fixture
def myapp_config() -> BaselineConfig:
    """Consumer-shaped config: a custom `.myapp-run.json` instead of `run.json`."""
    return BaselineConfig(run_record_filename=".myapp-run.json")


def _populate_invalidate_dir(
    out: Path, *, run_record_filename: str, stem: str = "doc"
) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{stem}.raw.md").write_text("raw")
    (out / "sections").mkdir()
    (out / "sections" / "Intro.md").write_text("# Intro")
    (out / "INDEX.md").write_text("# INDEX")
    (out / run_record_filename).write_text(
        json.dumps({"input": f"{stem}.html", "version": "0.17.0"})
    )


def _populate_baseline_dir(
    out: Path,
    *,
    run_record_filename: str = "run.json",
    consolidated_name: str = "doc.md",
) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / consolidated_name).write_text("# Doc", encoding="utf-8")
    (out / "INDEX.md").write_text("# INDEX", encoding="utf-8")
    (out / "sections").mkdir(exist_ok=True)
    (out / "sections" / "Intro.md").write_text("## Intro", encoding="utf-8")
    (out / run_record_filename).write_text(
        json.dumps(
            {
                "version": "0.17.1",
                "preset": "rag-default",
                "input": "doc.html",
                "section_count": 1,
                "image_count": 0,
                "started_at": "2026-05-10T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# make_invalidate_subcommand
# ---------------------------------------------------------------------------


class TestInvalidateSubcommand:
    def _app(self, registry: StageRegistry, **kwargs: object) -> typer.Typer:
        # Typer collapses a single-command app into a flat invocation
        # (the command name disappears). Add a stub command so the
        # registered name actually has to appear on the command line.
        app = typer.Typer()
        make_invalidate_subcommand(app, registry=registry, **kwargs)  # type: ignore[arg-type]

        @app.command("_stub")
        def _stub() -> None:
            pass

        return app

    def test_command_registers_in_help(self, registry: StageRegistry) -> None:
        app = self._app(registry)
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "invalidate" in result.output

    def test_explicit_source_stem_deletes_correctly(
        self, registry: StageRegistry, tmp_path: Path
    ) -> None:
        out = tmp_path / "out"
        out.mkdir()
        (out / "mydoc.raw.md").write_text("raw")
        (out / "sections").mkdir()

        app = self._app(registry)
        result = runner.invoke(
            app, ["invalidate", str(out), "split", "--source-stem", "mydoc"]
        )

        assert result.exit_code == 0, result.output
        assert not (out / "sections").exists()
        assert (out / "mydoc.raw.md").exists(), "upstream cache preserved"

    def test_inferred_source_stem_from_run_record(
        self, registry: StageRegistry, tmp_path: Path
    ) -> None:
        out = tmp_path / "out"
        _populate_invalidate_dir(out, run_record_filename="run.json")

        app = self._app(registry)
        result = runner.invoke(app, ["invalidate", str(out), "split"])

        assert result.exit_code == 0, result.output
        assert not (out / "sections").exists()
        assert not (out / "INDEX.md").exists()
        assert (out / "doc.raw.md").exists()

    def test_invalid_stage_errors(
        self, registry: StageRegistry, tmp_path: Path
    ) -> None:
        out = tmp_path / "out"
        _populate_invalidate_dir(out, run_record_filename="run.json")

        app = self._app(registry)
        result = runner.invoke(app, ["invalidate", str(out), "bogus"])

        assert result.exit_code != 0
        combined = result.output + str(result.exception)
        assert "must be one of" in combined

    def test_no_run_record_no_stem_errors(
        self, registry: StageRegistry, tmp_path: Path
    ) -> None:
        out = tmp_path / "out"
        out.mkdir()
        (out / "doc.raw.md").write_text("raw")

        app = self._app(registry)
        result = runner.invoke(app, ["invalidate", str(out), "backend"])

        assert result.exit_code != 0

    def test_no_caches_prints_noop_message_to_stderr(
        self, registry: StageRegistry, tmp_path: Path
    ) -> None:
        out = tmp_path / "out"
        _populate_invalidate_dir(out, run_record_filename="run.json")
        # Wipe everything but the run record.
        (out / "sections" / "Intro.md").unlink()
        (out / "sections").rmdir()
        (out / "INDEX.md").unlink()

        app = self._app(registry)
        result = runner.invoke(app, ["invalidate", str(out), "split"])

        assert result.exit_code == 0
        assert "no caches to invalidate" in result.output

    def test_custom_command_name(self, registry: StageRegistry, tmp_path: Path) -> None:
        out = tmp_path / "out"
        out.mkdir()
        (out / "sections").mkdir()

        app = self._app(registry, command_name="bust")
        # `invalidate` should NOT be registered.
        result = runner.invoke(
            app, ["invalidate", str(out), "split", "--source-stem", "doc"]
        )
        assert result.exit_code != 0

        # `bust` SHOULD be registered.
        result = runner.invoke(app, ["bust", str(out), "split", "--source-stem", "doc"])
        assert result.exit_code == 0

    def test_custom_run_record_filename(
        self, registry: StageRegistry, tmp_path: Path
    ) -> None:
        out = tmp_path / "out"
        _populate_invalidate_dir(out, run_record_filename=".myapp-run.json")

        app = self._app(registry, run_record_filename=".myapp-run.json")
        result = runner.invoke(app, ["invalidate", str(out), "split"])

        assert result.exit_code == 0, result.output
        assert not (out / "sections").exists()

    def test_run_record_missing_input_field_errors(
        self, registry: StageRegistry, tmp_path: Path
    ) -> None:
        out = tmp_path / "out"
        out.mkdir()
        (out / "run.json").write_text(json.dumps({"version": "0.17.0"}))

        app = self._app(registry)
        result = runner.invoke(app, ["invalidate", str(out), "split"])

        assert result.exit_code != 0

    def test_lists_deleted_paths_in_stdout(
        self, registry: StageRegistry, tmp_path: Path
    ) -> None:
        out = tmp_path / "out"
        _populate_invalidate_dir(out, run_record_filename="run.json")

        app = self._app(registry)
        result = runner.invoke(app, ["invalidate", str(out), "split"])

        assert result.exit_code == 0
        assert "sections" in result.output
        assert "INDEX.md" in result.output


# ---------------------------------------------------------------------------
# make_baseline_subcommand_group
# ---------------------------------------------------------------------------


class TestBaselineSubcommandGroup:
    def _app(self, **kwargs: object) -> typer.Typer:
        app = typer.Typer()
        make_baseline_subcommand_group(app, **kwargs)  # type: ignore[arg-type]
        return app

    def test_group_registers_with_three_subcommands(self) -> None:
        app = self._app()
        result = runner.invoke(app, ["baseline", "--help"])
        assert result.exit_code == 0
        assert "save" in result.output
        assert "list" in result.output
        assert "diff" in result.output

    def test_save_default_label_succeeds(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        _populate_baseline_dir(out)

        app = self._app()
        result = runner.invoke(app, ["baseline", "save", str(out)])

        assert result.exit_code == 0, result.output
        bases = list((out / ".baselines").iterdir())
        assert len(bases) == 1
        assert (bases[0] / "doc.md").exists()

    def test_save_explicit_label(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        _populate_baseline_dir(out)

        app = self._app()
        result = runner.invoke(
            app, ["baseline", "save", str(out), "--label", "corpus-final"]
        )

        assert result.exit_code == 0
        assert (out / ".baselines" / "corpus-final" / "doc.md").exists()

    def test_save_no_run_record_errors(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        out.mkdir()
        (out / "doc.md").write_text("body")

        app = self._app()
        result = runner.invoke(app, ["baseline", "save", str(out)])

        assert result.exit_code != 0

    def test_save_label_collision_errors(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        _populate_baseline_dir(out)

        app = self._app()
        runner.invoke(app, ["baseline", "save", str(out), "--label", "dup"])
        result = runner.invoke(app, ["baseline", "save", str(out), "--label", "dup"])

        assert result.exit_code != 0

    def test_list_empty_prints_no_baselines_to_stderr(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        out.mkdir()

        app = self._app()
        result = runner.invoke(app, ["baseline", "list", str(out)])

        assert result.exit_code == 0
        assert "no baselines" in result.output.lower()

    def test_list_shows_saved_baselines(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        _populate_baseline_dir(out)

        app = self._app()
        runner.invoke(app, ["baseline", "save", str(out), "--label", "alpha"])
        runner.invoke(app, ["baseline", "save", str(out), "--label", "beta"])

        result = runner.invoke(app, ["baseline", "list", str(out)])

        assert result.exit_code == 0
        assert "alpha" in result.output
        assert "beta" in result.output
        assert "0.17.1" in result.output
        assert "rag-default" in result.output

    def test_diff_unknown_label_errors(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        _populate_baseline_dir(out)

        app = self._app()
        result = runner.invoke(app, ["baseline", "diff", str(out), "nope"])

        assert result.exit_code != 0
        assert "no baseline labeled" in result.output

    def test_diff_unchanged_prints_unchanged(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        _populate_baseline_dir(out)

        app = self._app()
        runner.invoke(app, ["baseline", "save", str(out), "--label", "v1"])
        result = runner.invoke(app, ["baseline", "diff", str(out), "v1"])

        assert result.exit_code == 0
        assert "Run record: unchanged" in result.output
        assert "Sections: unchanged" in result.output
        assert "Body changes: none" in result.output

    def test_diff_section_added(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        _populate_baseline_dir(out)

        app = self._app()
        runner.invoke(app, ["baseline", "save", str(out), "--label", "v1"])
        (out / "sections" / "New.md").write_text("## New", encoding="utf-8")

        result = runner.invoke(app, ["baseline", "diff", str(out), "v1"])

        assert result.exit_code == 0
        assert "added" in result.output.lower()
        assert "New.md" in result.output

    def test_diff_show_section_emits_unified_diff(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        _populate_baseline_dir(out)

        app = self._app()
        runner.invoke(app, ["baseline", "save", str(out), "--label", "v1"])
        (out / "sections" / "Intro.md").write_text(
            "## Intro\n\nMODIFIED body.\n", encoding="utf-8"
        )

        result = runner.invoke(
            app, ["baseline", "diff", str(out), "v1", "--show-section", "Intro.md"]
        )

        assert result.exit_code == 0
        assert "---" in result.output
        assert "+++" in result.output
        assert "MODIFIED body" in result.output

    def test_diff_show_consolidated_emits_unified_diff(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        _populate_baseline_dir(out)

        app = self._app()
        runner.invoke(app, ["baseline", "save", str(out), "--label", "v1"])
        (out / "doc.md").write_text("# Doc\n\nMODIFIED.\n", encoding="utf-8")

        result = runner.invoke(
            app, ["baseline", "diff", str(out), "v1", "--show-consolidated"]
        )

        assert result.exit_code == 0
        assert "---" in result.output
        assert "+++" in result.output
        assert "MODIFIED" in result.output

    def test_diff_show_section_and_consolidated_together_errors(
        self, tmp_path: Path
    ) -> None:
        out = tmp_path / "out"
        _populate_baseline_dir(out)

        app = self._app()
        runner.invoke(app, ["baseline", "save", str(out), "--label", "v1"])

        result = runner.invoke(
            app,
            [
                "baseline",
                "diff",
                str(out),
                "v1",
                "--show-section",
                "Intro.md",
                "--show-consolidated",
            ],
        )

        assert result.exit_code != 0
        # The error message may wrap mid-phrase in Typer's rich-formatted
        # box (with `│` borders), and under GITHUB_ACTIONS typer forces
        # rich's terminal mode so ANSI color codes glue to the words —
        # strip escapes, box chars, and whitespace before the substring
        # assertion.
        assert "mutually exclusive" in _plain_output(result.output)

    def test_diff_show_section_missing_file_errors(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        _populate_baseline_dir(out)

        app = self._app()
        runner.invoke(app, ["baseline", "save", str(out), "--label", "v1"])

        result = runner.invoke(
            app, ["baseline", "diff", str(out), "v1", "--show-section", "Missing.md"]
        )

        assert result.exit_code != 0
        assert "missing from baseline or current" in result.output

    def test_custom_config_with_custom_run_record_filename(
        self, myapp_config: BaselineConfig, tmp_path: Path
    ) -> None:
        out = tmp_path / "out"
        _populate_baseline_dir(out, run_record_filename=".myapp-run.json")

        app = self._app(config=myapp_config)
        result = runner.invoke(app, ["baseline", "save", str(out), "--label", "v1"])
        assert result.exit_code == 0, result.output
        assert (out / ".baselines" / "v1" / ".myapp-run.json").exists()

        result = runner.invoke(app, ["baseline", "list", str(out)])
        assert result.exit_code == 0
        assert "v1" in result.output

        result = runner.invoke(app, ["baseline", "diff", str(out), "v1"])
        assert result.exit_code == 0
        assert "Run record: unchanged" in result.output

    def test_custom_command_name(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        _populate_baseline_dir(out)

        app = self._app(command_name="snapshot")
        # `baseline` group should NOT be registered.
        result = runner.invoke(app, ["baseline", "--help"])
        assert result.exit_code != 0

        # `snapshot` group SHOULD be.
        result = runner.invoke(app, ["snapshot", "--help"])
        assert result.exit_code == 0
        assert "save" in result.output
        assert "list" in result.output
        assert "diff" in result.output

        result = runner.invoke(app, ["snapshot", "save", str(out), "--label", "s1"])
        assert result.exit_code == 0
        assert (out / ".baselines" / "s1").exists()

    def test_save_output_includes_metadata_fields(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        _populate_baseline_dir(out)

        app = self._app()
        result = runner.invoke(app, ["baseline", "save", str(out), "--label", "v1"])

        assert result.exit_code == 0
        assert "version" in result.output
        assert "0.17.1" in result.output
        assert "rag-default" in result.output
