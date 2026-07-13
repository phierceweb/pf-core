"""
Mountable Typer sub-app for pf-core jobs administration.

Exposes the list / show / retry / cancel / reclaim / purge commands behind
the ``pf-jobs`` console script, so a consumer project can also include them
inside its own CLI namespace instead of rewriting them::

    # my_project/app/cli/__init__.py
    from pf_core.cli.jobs import app as jobs_app
    from pf_core.cli import create_cli

    app = create_cli("myapp", help="My application CLI.")
    app.add_typer(jobs_app, name="jobs")

Now ``myapp jobs list``, ``myapp jobs show 42``, etc. work and share
exactly the same behaviour as ``pf-jobs``.

The sub-app is a plain ``typer.Typer`` — no ``create_cli`` wrapping — so
it composes via :meth:`typer.Typer.add_typer` without double-registering
the ``--verbose`` callback.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import typer
from rich.console import Console
from rich.table import Table

from pf_core.jobs import JobRepo

# ---------------------------------------------------------------------------
# Typer sub-app
# ---------------------------------------------------------------------------

app = typer.Typer(
    name="jobs",
    help="Admin commands for pf-core jobs (list / show / retry / cancel / reclaim / purge).",
    no_args_is_help=True,
    add_completion=False,
)

_stdout = Console()

# ---------------------------------------------------------------------------
# Duration parsing
# ---------------------------------------------------------------------------

_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhdw])\s*$", re.IGNORECASE)
_DURATION_UNITS = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
}


def parse_duration(value: str) -> timedelta:
    """Parse ``'90d'`` / ``'24h'`` / ``'30m'`` / ``'60s'`` / ``'2w'`` → timedelta."""
    m = _DURATION_RE.match(value)
    if not m:
        raise typer.BadParameter(
            f"invalid duration {value!r}; expected e.g. 90d, 24h, 30m"
        )
    n, unit = int(m.group(1)), m.group(2).lower()
    return timedelta(seconds=n * _DURATION_UNITS[unit])


def _fmt_ts(ts: datetime | None) -> str:
    if ts is None:
        return "-"
    return ts.strftime("%Y-%m-%d %H:%M:%S")


def _fmt_json(value) -> str:
    if value is None:
        return "-"
    return json.dumps(value, indent=2, default=str, sort_keys=True)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@app.command("list")
def list_jobs(
    kind: str | None = typer.Option(None, "--kind", help="Filter by job kind."),
    status: str | None = typer.Option(None, "--status", help="Filter by status."),
    since: str | None = typer.Option(
        None, "--since", help="Only jobs created within this window, e.g. 24h."
    ),
    created_by: str | None = typer.Option(
        None, "--created-by", help="Filter by creator."
    ),
    limit: int = typer.Option(20, "--limit", help="Max rows."),
) -> None:
    """List jobs, newest-first."""
    since_dt = (
        datetime.now(timezone.utc) - parse_duration(since) if since else None
    )
    rows = JobRepo().find(
        kind=kind,
        status=status,
        since=since_dt,
        created_by=created_by,
        limit=limit,
    )
    if not rows:
        _stdout.print("No jobs match.")
        return

    table = Table(title=f"jobs ({len(rows)} rows)")
    table.add_column("id", justify="right")
    table.add_column("kind")
    table.add_column("status")
    table.add_column("prio", justify="right")
    table.add_column("progress")
    table.add_column("created_by")
    table.add_column("created_at")
    for r in rows:
        progress = (
            f"{r['progress_current']}/{r['progress_total']}"
            if r.get("progress_total") is not None
            else str(r["progress_current"])
        )
        table.add_row(
            str(r["id"]),
            r["kind"],
            r["status"],
            str(r["priority"]),
            progress,
            r.get("created_by") or "-",
            _fmt_ts(r.get("created_at")),
        )
    _stdout.print(table)


@app.command("show")
def show_job(job_id: int = typer.Argument(..., help="Job id.")) -> None:
    """Show full detail for a single job: header, steps, events."""
    repo = JobRepo()
    bundle = repo.get_with_steps(job_id)
    if bundle is None:
        _stdout.print(f"[red]Job {job_id} not found.[/red]")
        raise typer.Exit(code=1)

    job = bundle
    _stdout.print(
        f"[bold]Job {job['id']}[/bold]  kind={job['kind']}  status={job['status']}"
    )
    _stdout.print(
        f"  priority={job['priority']}  created_by={job.get('created_by') or '-'}"
    )
    _stdout.print(
        f"  created_at={_fmt_ts(job.get('created_at'))}  "
        f"started_at={_fmt_ts(job.get('started_at'))}  "
        f"finished_at={_fmt_ts(job.get('finished_at'))}"
    )
    if job.get("current_step"):
        _stdout.print(f"  current_step={job['current_step']}")
    if job.get("error"):
        _stdout.print(
            f"  [red]error[/red]: {job['error']} ({job.get('error_class') or '-'})"
        )

    _stdout.print("\n[bold]Inputs[/bold]")
    _stdout.print(_fmt_json(job.get("inputs")))
    _stdout.print("\n[bold]Outputs[/bold]")
    _stdout.print(_fmt_json(job.get("outputs")))

    steps = bundle.get("steps") or []
    if steps:
        t = Table(title=f"Steps ({len(steps)})")
        t.add_column("idx", justify="right")
        t.add_column("name")
        t.add_column("status")
        t.add_column("duration_ms", justify="right")
        t.add_column("started_at")
        for s in steps:
            t.add_row(
                str(s["step_index"]),
                s["name"],
                s["status"],
                str(s.get("duration_ms") or "-"),
                _fmt_ts(s.get("started_at")),
            )
        _stdout.print("")
        _stdout.print(t)

    events = bundle.get("events") or []
    if events:
        t = Table(title=f"Events ({len(events)})")
        t.add_column("created_at")
        t.add_column("type")
        t.add_column("message")
        for e in events:
            t.add_row(
                _fmt_ts(e.get("created_at")),
                e["event_type"],
                e["message"] or "",
            )
        _stdout.print("")
        _stdout.print(t)


@app.command("retry")
def retry_job(job_id: int = typer.Argument(...)) -> None:
    """Reset a failed job to pending (priority bumps +10)."""
    JobRepo().retry(job_id)
    _stdout.print(f"[green]Job {job_id} requeued.[/green]")


@app.command("cancel")
def cancel_job(
    job_id: int = typer.Argument(...),
    reason: str = typer.Option("user aborted via pf-jobs", "--reason"),
) -> None:
    """Cancel a pending or running job."""
    JobRepo().cancel(job_id, reason=reason)
    _stdout.print(f"[yellow]Job {job_id} canceled.[/yellow]")


@app.command("reclaim")
def reclaim(
    lease_seconds: int | None = typer.Option(
        None, "--lease-seconds",
        help="Override JOB_LEASE_SECONDS (default 300).",
    ),
) -> None:
    """Reset stale claims: running jobs whose lease expired go back to pending."""
    n = JobRepo().reclaim_stale(lease_seconds=lease_seconds)
    _stdout.print(f"Reclaimed {n} stale job(s).")


@app.command("purge")
def purge(
    older_than: str = typer.Option(
        ..., "--older-than",
        help="Delete jobs finished more than this ago, e.g. 90d.",
    ),
    status: str | None = typer.Option(
        "succeeded", "--status",
        help="Only purge this status; pass 'any' for all terminal statuses.",
    ),
    yes: bool = typer.Option(False, "--yes", help="Skip confirmation."),
) -> None:
    """Delete old finished jobs. llm_runs keep their history (job_id → NULL)."""
    cutoff = parse_duration(older_than)
    status_arg: str | list[str] | None
    if status is None or status.lower() == "any":
        status_arg = ["succeeded", "failed", "canceled"]
    else:
        status_arg = status

    if not yes:
        confirm = typer.confirm(
            f"Delete jobs with status={status_arg!r} older than {older_than}?"
        )
        if not confirm:
            _stdout.print("Aborted.")
            raise typer.Exit(code=1)

    n = JobRepo().purge(older_than=cutoff, status=status_arg)
    _stdout.print(f"[green]Purged {n} job(s).[/green]")


__all__ = ["app", "parse_duration"]
