"""pf_core.guards.adoption — the paste-ready blocks ``--emit-baseline`` / ``--emit-allowlist`` print."""

from __future__ import annotations

from pf_core.guards.adoption import allowlist_block, baseline_block
from pf_core.guards.layering import LayeringViolation


def test_the_baseline_block_lists_each_file_at_its_size_sorted() -> None:
    assert baseline_block({"pkg/z.py": 640, "pkg/a.py": 512}) == (
        '[tool.pf_guards.baseline]\n"pkg/a.py" = 512\n"pkg/z.py" = 640'
    )


def test_the_allowlist_block_groups_imports_per_path_sorted() -> None:
    violations = [
        LayeringViolation("app/orchestrators/flow.py", "app.repo.q", "orchestrators → repo"),
        LayeringViolation("app/orchestrators/flow.py", "app.clients.c", "orchestrators → clients"),
        LayeringViolation("app/api/_util.py", "app.repo.q", "api → repo"),
        LayeringViolation("app/orchestrators/flow.py", "app.repo.q", "twice on one path"),
    ]
    assert allowlist_block(violations) == (
        "[tool.pf_guards.layering_allowlist]\n"
        '"app/api/_util.py" = ["app.repo.q"]\n'
        '"app/orchestrators/flow.py" = ["app.clients.c", "app.repo.q"]'
    )


def test_an_empty_block_is_only_its_header() -> None:
    assert baseline_block({}) == "[tool.pf_guards.baseline]"
    assert allowlist_block([]) == "[tool.pf_guards.layering_allowlist]"
