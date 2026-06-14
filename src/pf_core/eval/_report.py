"""
EvalResult and EvalReport — replay results, summary, and HTML output.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pf_core.eval._config import AgentEvalConfig

# ---------------------------------------------------------------------------
# Inline Jinja2 HTML template
# ---------------------------------------------------------------------------

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Eval Report: {{ agent_type }} vs {{ version }}</title>
<style>
body { font-family: monospace; max-width: 1100px; margin: 2rem auto; padding: 0 1rem; }
h1 { border-bottom: 2px solid #333; }
.summary { background: #f5f5f5; padding: 1rem; border-radius: 4px; margin: 1rem 0; }
.pass { color: #1a7f3c; font-weight: bold; }
.fail { color: #b91c1c; font-weight: bold; }
table { border-collapse: collapse; width: 100%; margin: 1rem 0; }
th, td { border: 1px solid #ccc; padding: 0.4rem 0.6rem; text-align: left; }
th { background: #e5e5e5; }
tr.row-pass td { background: #f0fff4; }
tr.row-fail td { background: #fff5f5; }
.score-bar { display: inline-block; background: #4ade80; height: 10px; vertical-align: middle; }
.score-bar.low { background: #f87171; }
details > summary { cursor: pointer; }
pre { background: #f5f5f5; padding: 0.5rem; overflow-x: auto; font-size: 0.85em; }
</style>
</head>
<body>
<h1>Eval Report: {{ agent_type }} vs {{ version }}</h1>
<div class="summary">
  <p><strong>Target:</strong> {{ target_str }}</p>
  <p><strong>Runs:</strong> {{ n_completed }}/{{ n_total }} completed</p>
  <p><strong>Mean score:</strong> {{ "%.3f"|format(mean_score) }}</p>
  {% if n_completed > 1 %}
  <p><strong>Median:</strong> {{ "%.3f"|format(median_score) }}</p>
  {% endif %}
  <p><strong>Pass rate:</strong> {{ n_passed }}/{{ n_completed }}
    ({{ "%.0f"|format(pass_rate * 100) }}%)</p>
  <p><strong>Overall:</strong>
    {% if overall_pass %}<span class="pass">PASS</span>{% else %}<span class="fail">FAIL</span>{% endif %}
    (threshold {{ "%.2f"|format(threshold) }})</p>
</div>

{% if failures %}
<h2>Low-scoring runs (score &lt; 0.5)</h2>
<ul>
{% for r in failures %}
  <li>replay {{ r.run_id }} ← golden {{ r.golden_id }} &nbsp; score: <strong>{{ "%.3f"|format(r.score) }}</strong>
    {% if r.error %}&nbsp; error: <em>{{ r.error[:120] }}</em>{% endif %}</li>
{% endfor %}
</ul>
{% endif %}

<h2>All results</h2>
<table>
<tr><th>Golden ID</th><th>Replay ID</th><th>Score</th><th>Pass</th><th>Error</th></tr>
{% for r in results %}
<tr class="{{ 'row-pass' if r.passed else 'row-fail' }}">
  <td>{{ r.golden_id }}</td>
  <td>{{ r.run_id if r.run_id >= 0 else "—" }}</td>
  <td>
    <span class="score-bar {{ 'low' if r.score < 0.5 else '' }}"
          style="width: {{ (r.score * 100)|int }}px"></span>
    {{ "%.3f"|format(r.score) }}
  </td>
  <td>{{ "✓" if r.passed else "✗" }}</td>
  <td>{{ r.error or "" }}</td>
</tr>
{% endfor %}
</table>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class EvalResult:
    """Result for one golden-run replay."""

    golden_id: int
    """ID of the golden ``llm_runs`` row that was replayed."""

    run_id: int
    """ID of the new replay ``llm_runs`` row. ``-1`` if the call failed before recording."""

    score: float
    """0.0–1.0. 0.0 on outright failure or comparator returning minimum."""

    passed: bool
    """True if ``score >= pass_threshold`` from the agent's eval config."""

    error: str | None = None
    """Short error message if the replay failed (network, content filter, etc.)."""


@dataclass
class EvalReport:
    """Aggregated results for one eval run (one agent type, one golden set version).

    Returned by :meth:`EvalRunner.run`. Contains all per-run results plus
    aggregate statistics and display methods.
    """

    agent_type: str
    version: str
    target: dict
    results: list[EvalResult]
    cfg: "AgentEvalConfig"
    job_id: int | None = field(default=None, repr=False)

    # ------------------------------------------------------------------
    # Aggregates
    # ------------------------------------------------------------------

    @property
    def mean_score(self) -> float:
        """Mean score across completed (non-error) results. 0.0 if none."""
        scores = [r.score for r in self.results if r.error is None]
        return statistics.mean(scores) if scores else 0.0

    @property
    def median_score(self) -> float:
        scores = [r.score for r in self.results if r.error is None]
        return statistics.median(scores) if scores else 0.0

    @property
    def pass_rate(self) -> float:
        completed = [r for r in self.results if r.error is None]
        if not completed:
            return 0.0
        return sum(1 for r in completed if r.passed) / len(completed)

    @property
    def passed(self) -> bool:
        """True if mean_score meets the pass_threshold in the agent config."""
        return self.mean_score >= self.cfg.pass_threshold

    # ------------------------------------------------------------------
    # Display
    # ------------------------------------------------------------------

    def summary(self) -> str:
        """Human-readable summary suitable for CLI output."""
        n = len(self.results)
        n_ok = sum(1 for r in self.results if r.error is None)
        n_pass = sum(1 for r in self.results if r.passed)
        target_str = ", ".join(f"{k}={v}" for k, v in self.target.items())
        verdict = "PASS" if self.passed else "FAIL"
        lines = [
            f"EVAL REPORT: {self.agent_type} vs {self.version}",
            f"Target:  {target_str}",
            f"Runs:    {n_ok}/{n} completed",
            f"Mean:    {self.mean_score:.3f}",
        ]
        if n_ok > 1:
            lines.append(f"Median:  {self.median_score:.3f}")
        lines.append(
            f"Pass:    {verdict} ({n_pass}/{n_ok} above threshold {self.cfg.pass_threshold:.2f})"
        )
        failures = [r for r in self.results if r.score < 0.5]
        if failures:
            lines.append("")
            lines.append("Failures (score < 0.5):")
            for r in failures[:10]:
                err = f"   error: {r.error[:80]}" if r.error else ""
                lines.append(
                    f"  replay {r.run_id} ← golden {r.golden_id}"
                    f"   score: {r.score:.3f}{err}"
                )
        return "\n".join(lines)

    def write_html(self, path: str) -> None:
        """Write a self-contained HTML diff report to ``path``."""
        import jinja2

        env = jinja2.Environment(loader=jinja2.BaseLoader(), autoescape=True)
        tmpl = env.from_string(_HTML_TEMPLATE)
        n_completed = sum(1 for r in self.results if r.error is None)
        html = tmpl.render(
            agent_type=self.agent_type,
            version=self.version,
            target_str=", ".join(f"{k}={v}" for k, v in self.target.items()),
            n_total=len(self.results),
            n_completed=n_completed,
            n_passed=sum(1 for r in self.results if r.passed),
            mean_score=self.mean_score,
            median_score=self.median_score if n_completed > 1 else self.mean_score,
            pass_rate=self.pass_rate,
            overall_pass=self.passed,
            threshold=self.cfg.pass_threshold,
            results=self.results,
            failures=[r for r in self.results if r.score < 0.5],
        )
        from pathlib import Path

        Path(path).write_text(html, encoding="utf-8")
