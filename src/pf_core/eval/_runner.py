"""
EvalRunner — orchestrates replay eval runs against the golden set.

Usage::

    from pf_core.eval import EvalRunner

    runner = EvalRunner(config_path="config/eval.yaml")

    report = runner.run(
        version="golden_v2",
        agent_type="drafter",
        target={"model": "anthropic/claude-opus-4-7"},
        tag_as="experiment:opus47-v5",
    )
    print(report.summary())
    report.write_html("out/drafter_opus47_v5.html")
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from pf_core.exceptions import ConfigurationError, PreconditionError
from pf_core.jobs import Job, JobRepo
from pf_core.llm.parse import parse_llm_json
from pf_core.llm.router import get_agent_config, resolve_agent
from pf_core.llm.tracking.repo import LlmRunRepo
from pf_core.llm.tracking.subrepos import LlmRunOutcomeRepo
from pf_core.log import get_logger

from pf_core.eval._compare import get_comparator
from pf_core.eval._config import AgentEvalConfig, load_eval_config
from pf_core.eval._golden import GoldenSetRepo
from pf_core.eval._report import EvalReport, EvalResult

logger = get_logger(__name__)


class EvalRunner:
    """Orchestrates replay evaluations against a golden set.

    Args:
        config_path: Path to ``eval.yaml``. Defaults to ``EVAL_CONFIG`` env
            var, then ``config/eval.yaml``.
    """

    def __init__(self, config_path: str | None = None) -> None:
        self._cfg = load_eval_config(config_path)

    # ------------------------------------------------------------------
    # Public: run one eval
    # ------------------------------------------------------------------

    def run(
        self,
        *,
        version: str,
        agent_type: str,
        target: dict[str, Any],
        tag_as: str | None = None,
    ) -> EvalReport:
        """Replay the golden set and return an EvalReport.

        Creates one ``eval_replay`` job to track progress. Each golden run is
        replayed via a direct ``client.chat()`` call; the result is recorded in
        ``llm_runs``, linked to its golden via ``llm_run_links``, and scored
        in ``llm_run_outcomes``.

        Args:
            version: Golden set version (e.g. ``"golden_v2"``).
            agent_type: Agent type slug to replay (e.g. ``"drafter"``).
            target: Dict of overrides applied to the agent config for this
                experiment. Typically ``{"model": "claude-opus-4-7"}``.
                Any key present in the router config can be overridden.
            tag_as: Optional experiment tag applied to all replay runs
                (e.g. ``"experiment:opus47-v5"``).

        Raises:
            PreconditionError: If no golden runs exist for the requested
                version + agent_type combination.

        Returns:
            :class:`EvalReport` with per-run scores and aggregate statistics.
        """
        agent_cfg = self._cfg.for_agent(agent_type)
        golden_repo = GoldenSetRepo()
        goldens = golden_repo.list(version=version, agent_type=agent_type)

        if not goldens:
            raise PreconditionError(
                f"No golden runs found for version={version!r}, agent_type={agent_type!r}. "
                "Use GoldenSetRepo.add()/seed_from_outcomes() to add some."
            )

        # NOT f"eval:{version}" — that exact string is the golden-membership
        # tag; putting it on replays adds them to the golden set.
        replay_tags = [f"eval:replay:{version}", f"agent:{agent_type}"]
        if tag_as:
            replay_tags.append(tag_as)

        job_id = JobRepo().create(
            kind="eval_replay",
            inputs={
                "version": version,
                "agent_type": agent_type,
                "target": target,
                "tag_as": tag_as,
            },
            created_by="pf_core.eval",
        )

        results: list[EvalResult] = []

        def _replay_one(golden_run: dict) -> EvalResult:
            golden_id = int(golden_run["id"])
            try:
                return self._run_single_replay(
                    golden_id=golden_id,
                    agent_type=agent_type,
                    agent_cfg=agent_cfg,
                    target=target,
                    tags=replay_tags,
                    golden_repo=golden_repo,
                )
            except Exception as exc:
                logger.warning(
                    "replay_failed",
                    golden_id=golden_id,
                    error=str(exc)[:300],
                )
                return EvalResult(
                    golden_id=golden_id,
                    run_id=-1,
                    score=0.0,
                    passed=False,
                    error=str(exc)[:500],
                )

        with Job(job_id) as job:
            job.transition("running")
            job.progress(total=len(goldens))
            done = [0]

            if agent_cfg.parallelism > 1:
                with ThreadPoolExecutor(max_workers=agent_cfg.parallelism) as executor:
                    fmap = {executor.submit(_replay_one, g): g for g in goldens}
                    for future in as_completed(fmap):
                        result = future.result()
                        results.append(result)
                        done[0] += 1
                        job.progress(current=done[0])
            else:
                for golden_run in goldens:
                    result = _replay_one(golden_run)
                    results.append(result)
                    done[0] += 1
                    job.progress(current=done[0])

            report = EvalReport(
                agent_type=agent_type,
                version=version,
                target=target,
                results=results,
                cfg=agent_cfg,
                job_id=job_id,
            )
            job.outputs = {
                "n_runs": len(results),
                "n_passed": sum(1 for r in results if r.passed),
                "mean_score": report.mean_score,
                "passed": report.passed,
            }
            job.transition("succeeded")

        return report

    # ------------------------------------------------------------------
    # Public: compare two experiment tags
    # ------------------------------------------------------------------

    def compare_experiments(
        self,
        *,
        baseline: str,
        candidate: str,
        agent_type: str,
    ) -> list[dict]:
        """Pair baseline and candidate replay runs by shared golden parent.

        Returns list of dicts with keys: ``golden_id``, ``baseline_score``,
        ``candidate_score``, ``delta`` (candidate − baseline).

        Args:
            baseline: Experiment tag for the baseline runs.
            candidate: Experiment tag for the candidate runs.
            agent_type: Slug to filter both sets by.
        """
        from sqlalchemy import select, and_

        from pf_core.db.repository import Repository
        from pf_core.llm.tracking import schema as s

        repo = Repository()

        def _scores_for_tag(tag: str) -> dict[int, float]:
            """Map golden_id → eval_score for all replays with this tag."""
            stmt = (
                select(
                    s.llm_run_links.c.parent_run_id.label("golden_id"),
                    s.llm_run_outcomes.c.score,
                )
                .join(
                    s.llm_run_tags,
                    s.llm_run_tags.c.llm_run_id == s.llm_run_links.c.child_run_id,
                )
                .join(
                    s.llm_run_outcomes,
                    and_(
                        s.llm_run_outcomes.c.llm_run_id == s.llm_run_links.c.child_run_id,
                        s.llm_run_outcomes.c.outcome_kind == "eval_score",
                    ),
                )
                .join(
                    s.llm_runs,
                    s.llm_runs.c.id == s.llm_run_links.c.child_run_id,
                )
                .join(
                    s.llm_agent_types,
                    s.llm_agent_types.c.id == s.llm_runs.c.agent_type_id,
                )
                .where(s.llm_run_tags.c.tag == tag)
                .where(s.llm_run_links.c.relation == "replay")
                .where(s.llm_agent_types.c.slug == agent_type)
            )
            with repo._tx() as conn:
                rows = conn.execute(stmt).mappings().fetchall()
            return {int(r["golden_id"]): float(r["score"]) for r in rows}

        base_scores = _scores_for_tag(baseline)
        cand_scores = _scores_for_tag(candidate)
        shared_golden_ids = sorted(set(base_scores) & set(cand_scores))

        return [
            {
                "golden_id": gid,
                "baseline_score": base_scores[gid],
                "candidate_score": cand_scores[gid],
                "delta": cand_scores[gid] - base_scores[gid],
            }
            for gid in shared_golden_ids
        ]

    # ------------------------------------------------------------------
    # Internal: replay one golden run
    # ------------------------------------------------------------------

    def _run_single_replay(
        self,
        *,
        golden_id: int,
        agent_type: str,
        agent_cfg: AgentEvalConfig,
        target: dict[str, Any],
        tags: list[str],
        golden_repo: GoldenSetRepo,
    ) -> EvalResult:
        """Execute one replay and return a scored EvalResult."""
        from pf_core.eval._judge import run_judge

        payload = golden_repo.get_payload(golden_id)
        if payload is None:
            raise PreconditionError(
                f"No payload stored for golden run {golden_id}. "
                "Ensure payloads were saved when the original run was recorded."
            )

        rendered_system = payload.get("rendered_system")
        rendered_user = payload.get("rendered_user")
        golden_parsed: dict = payload.get("parsed_output") or {}
        if not golden_parsed and payload.get("raw_response"):
            # Consumers that validate post-record can store JSON-null
            # parsed_output; without this fallback every replay scores vs {}.
            reparsed = parse_llm_json(payload["raw_response"])
            if isinstance(reparsed, dict):
                golden_parsed = reparsed

        # Structured comparators need a non-empty dict golden — fail here,
        # before the replay spends tokens ({} vs {} would score 1.0).
        if agent_cfg.compare != "llm_judge" and (
            not isinstance(golden_parsed, dict) or not golden_parsed
        ):
            raise PreconditionError(
                f"golden {golden_id} has no non-empty dict parsed_output for "
                f"structured comparison (compare={agent_cfg.compare!r}). "
                "Re-promote it with dict output or use compare: llm_judge."
            )

        messages: list[dict] = []
        if rendered_system:
            messages.append({"role": "system", "content": rendered_system})
        if rendered_user:
            messages.append({"role": "user", "content": rendered_user})

        # Resolve the replay client through the router so replays run on the
        # backend the agent declares (target keys "backend"/"model" override).
        # Agents absent from the router degrade — loudly — to the OpenRouter
        # client with the target-supplied model.
        target = dict(target)
        backend_override = target.pop("backend", None)
        model_override = target.pop("model", None)
        try:
            client, base_cfg, _replay_backend = resolve_agent(
                agent_type, backend=backend_override, model_override=model_override
            )
        except ConfigurationError as exc:
            logger.warning(
                "replay_router_unavailable",
                agent_type=agent_type,
                error=str(exc)[:200],
            )
            from pf_core.clients.openrouter import get_client

            client = get_client()
            try:
                base_cfg = get_agent_config(agent_type, model_override=model_override)
            except ConfigurationError:
                base_cfg = {"model": model_override} if model_override else {}
        merged = {**base_cfg, **agent_cfg.sampling, **target}
        model = merged.pop("model", "")
        if not model:
            raise PreconditionError(
                f"No model resolved for agent_type={agent_type!r}. "
                "Set it in model_router.yaml or pass via target={'model': '...'}."
            )

        t0 = time.monotonic()
        try:
            raw_content, usage_raw = client.chat(
                messages=messages, model=model, **merged
            )
            status = "success"
            error_msg: str | None = None
        except Exception as exc:
            raw_content = ""
            usage_raw = {}
            status = "failed"
            error_msg = str(exc)[:1000]
        duration_ms = int((time.monotonic() - t0) * 1000)

        parsed_replay: dict | None = None
        if raw_content:
            result = parse_llm_json(raw_content)
            if isinstance(result, dict):
                parsed_replay = result

        replay_run_id = LlmRunRepo().record(
            agent_type=agent_type,
            model=model,
            sampling={**merged, "model": model},
            rendered_prompts=(rendered_system, rendered_user),
            raw_response=raw_content or None,
            parsed_output=parsed_replay,
            tags=tags,
            parent_run=(golden_id, "replay"),
            status=status,
            error=error_msg,
            usage={**usage_raw, "duration_ms": duration_ms},
        )

        if status == "failed":
            return EvalResult(
                golden_id=golden_id,
                run_id=replay_run_id,
                score=0.0,
                passed=False,
                error=error_msg,
            )

        # Score
        if agent_cfg.compare == "llm_judge":
            score = run_judge(
                agent_type=agent_type,
                judge_agent_type=agent_cfg.judge_agent_type or f"{agent_type}_judge",
                golden_payload=payload,
                replay_content=raw_content,
                replay_run_id=replay_run_id,
            )
        else:
            comparator = get_comparator(agent_cfg.compare)
            context = {
                "diff_fields": agent_cfg.diff_fields,
                "tolerances": agent_cfg.tolerances,
            }
            score = comparator(
                golden_parsed,
                parsed_replay or {},
                context=context,
            )

        # Metric gate check (against metrics stored on the replay run, if any)
        if agent_cfg.metrics:
            replay_metrics = golden_repo.get_ground_truth(replay_run_id)
            for gate in agent_cfg.metrics:
                val = replay_metrics.get(gate.name)
                if val is not None:
                    if gate.min is not None and val < gate.min:
                        score = 0.0
                        break
                    if gate.max is not None and val > gate.max:
                        score = 0.0
                        break

        LlmRunOutcomeRepo().record(
            replay_run_id,
            outcome_kind="eval_score",
            score=score,
            notes=f"vs golden {golden_id}",
        )

        passed = score >= agent_cfg.pass_threshold
        return EvalResult(
            golden_id=golden_id,
            run_id=replay_run_id,
            score=score,
            passed=passed,
            error=None,
        )
