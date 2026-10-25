"""Read-only Dream Quality Report.

The report separates four questions that are often conflated:

* did the dream job execute to a terminal result?
* did a completed dream persist a readable result row?
* what did the persisted result admit, review, or leave unverified?
* is there evidence of downstream learning benefit? (never, from stored data)

It reads only ``jobs``, ``dream_results``, ``audit_events`` and ``dream_state``.
It never writes, triggers a dream, contacts a model, or includes proposal text,
evidence payloads, prompts, memory contents, or raw job payloads.
"""
from __future__ import annotations

import argparse
import datetime
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

from core.db import Database
from core.migrations import current_schema_version

REPORT_NAME = "dream_quality"
REPORT_VERSION = 1
CATEGORY_KEYS = (
    "new_units",
    "resolutions",
    "associations",
    "procedures",
    "review_items",
    "sourced_memories",
)
CONFLICT_CATEGORIES = {
    "lease_lost",
    "sqlite_revision",
    "historical_evidence_changed",
    "protected_context_changed",
    "identity_drift",
}
DEFERRAL_CATEGORIES = {"writer_busy", "sqlite_writer_busy"}


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _safe_token(value: Any, limit: int = 64) -> str | None:
    if not isinstance(value, str) or not 0 < len(value) <= limit:
        return None
    if not value.isascii() or not all(ch.isalnum() or ch in "_-." for ch in value):
        return None
    return value


def _timestamp_text(value: Any) -> str | None:
    if isinstance(value, str) and 0 < len(value) <= 64 and value.isprintable():
        return value
    return None


def _parse_json(value: Any) -> tuple[bool, Any]:
    try:
        return True, json.loads(value)
    except (TypeError, ValueError):
        return False, None


def _parse_object(value: Any) -> dict[str, Any] | None:
    valid, parsed = _parse_json(value)
    if valid and isinstance(parsed, dict):
        return parsed
    return None


def _warning(code: str, detail: str) -> dict[str, str]:
    return {"code": code, "detail": detail}


def _category(change: Any, *, accepted: bool) -> str | None:
    if not isinstance(change, dict):
        return None
    kind = change.get("type")
    if kind == "unit":
        return "new_units"
    if kind == "resolve":
        return "resolutions"
    if kind == "association":
        return "associations"
    if kind == "procedure":
        return "procedures"
    if kind == "memory":
        return "sourced_memories"
    if kind == "review":
        if accepted and change.get("target") == "memory_consolidation":
            return "sourced_memories"
        return "review_items"
    return None


def _count_changes(items: Any, *, accepted: bool) -> dict[str, Any]:
    counts = {key: 0 for key in CATEGORY_KEYS}
    if not isinstance(items, list):
        return {
            "available": False,
            "counts": counts,
            "total": None,
            "unknown_items": None,
            "invalid_items": None,
        }
    unknown = 0
    invalid = 0
    for item in items:
        if not isinstance(item, dict):
            invalid += 1
            continue
        category = _category(item, accepted=accepted)
        if category is None:
            unknown += 1
        else:
            counts[category] += 1
    return {
        "available": True,
        "counts": counts,
        "total": len(items),
        "unknown_items": unknown,
        "invalid_items": invalid,
    }


def _audit_index(conn: sqlite3.Connection, job_ids: list[str]) -> dict[str, dict[str, list[dict[str, Any]]]]:
    if not job_ids:
        return {}
    placeholders = ",".join("?" for _ in job_ids)
    rows = conn.execute(
        "SELECT target_id,operation,details_json FROM audit_events "
        "WHERE target_id IN (" + placeholders + ") "
        "AND operation IN ('dream.failed','dream.deferred','dream.committed') "
        "ORDER BY occurred_at,event_id",
        job_ids,
    ).fetchall()
    index: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for row in rows:
        job_id = row["target_id"]
        operation = row["operation"]
        details = _parse_object(row["details_json"])
        index.setdefault(job_id, {}).setdefault(operation, []).append(
            {"valid": details is not None, "details": details or {}}
        )
    return index


def _last_audit(audits: dict[str, list[dict[str, Any]]], operation: str) -> dict[str, Any] | None:
    values = audits.get(operation)
    return values[-1] if values else None


def _execution(job: dict[str, Any], failure: dict[str, Any] | None,
               deferral: dict[str, Any] | None, warnings: list[dict[str, str]]) -> dict[str, Any]:
    status = job.get("status")
    job_error = _safe_token(job.get("error"))
    failure_details = failure.get("details", {}) if failure and failure.get("valid") else {}
    deferral_details = deferral.get("details", {}) if deferral and deferral.get("valid") else {}
    failure_category = _safe_token(failure_details.get("category"))
    deferral_category = _safe_token(deferral_details.get("category"))
    if failure and not failure.get("valid"):
        warnings.append(_warning("invalid_failure_audit", "dream.failed details are not a JSON object"))
    if deferral and not deferral.get("valid"):
        warnings.append(_warning("invalid_deferral_audit", "dream.deferred details are not a JSON object"))
    if job.get("error") and job_error is None:
        warnings.append(_warning("unrecognized_job_error", "stored job.error is not a safe category token"))
    if failure_category and job_error and failure_category != job_error:
        warnings.append(_warning("failure_category_mismatch",
                                 "job.error and dream.failed audit disagree"))

    if status == "completed":
        outcome = "success"
    elif status in {"pending", "leased"}:
        outcome = status
    elif status == "review_required":
        if deferral_category in DEFERRAL_CATEGORIES:
            outcome = "deferral"
        elif deferral_category in CONFLICT_CATEGORIES:
            outcome = "conflict"
        else:
            outcome = "deferred"
            if deferral_category is None:
                warnings.append(_warning("unspecified_deferral",
                                         "deferred job has no recognized conflict category"))
    elif status == "failed":
        category = failure_category or job_error
        if category == "timeout":
            outcome = "timeout"
        elif category in {"invalid_output", "identity_drift"}:
            outcome = "validation_failure"
        else:
            outcome = "failure"
            if category is None:
                warnings.append(_warning("unspecified_failure", "failed job has no failure category"))
    else:
        outcome = "unknown"
        warnings.append(_warning("unknown_job_status", "job status is not recognized"))

    latency = _float_or_none(failure_details.get("latency"))
    retry_after = _float_or_none(deferral_details.get("retry_after"))
    failure_record = None
    if status == "failed":
        failure_record = {
            "category": failure_category or job_error,
            "stage": _safe_token(failure_details.get("stage")),
            "kind": _safe_token(failure_details.get("kind")),
            "latency_seconds": latency,
            "source": "dream.failed" if failure_category else "jobs.error",
            "retry_budget_exhausted": job_error == "retry_budget_exhausted",
        }
    deferral_record = None
    if status == "review_required":
        deferral_record = {
            "category": deferral_category,
            "retry_after": retry_after,
            "source": "dream.deferred" if deferral_category else None,
        }
    return {
        "status": status,
        "outcome": outcome,
        "terminal": status in {"completed", "failed", "review_required"},
        "attempt_count": _int_or_none(job.get("attempt_count")),
        "created_at": _timestamp_text(job.get("created_at")),
        "completed_at": _timestamp_text(job.get("completed_at")),
        "job_error": job_error,
        "failure": failure_record,
        "deferral": deferral_record,
        "deferred": status == "review_required",
    }


def _persistence(job: dict[str, Any], result_row: dict[str, Any] | None,
                 result_object: dict[str, Any] | None, result_json_valid: bool,
                 commit_audit: dict[str, Any] | None,
                 counts: dict[str, Any], warnings: list[dict[str, str]]) -> dict[str, Any]:
    job_completed = job.get("status") == "completed"
    result_present = result_row is not None
    source_revision = _int_or_none(result_row.get("source_revision")) if result_row else None
    resulting_revision = _int_or_none(result_row.get("resulting_revision")) if result_row else None
    result_source = _int_or_none(result_object.get("source_revision")) if result_object else None
    result_resulting = _int_or_none(result_object.get("revision")) if result_object else None
    revisions_present = all(
        value is not None
        for value in (source_revision, resulting_revision, result_source, result_resulting)
    )
    revisions_consistent = revisions_present and (
        source_revision == result_source and resulting_revision == result_resulting
    )
    revision_changed = None
    revision_delta = None
    if source_revision is not None and resulting_revision is not None:
        revision_changed = resulting_revision > source_revision
        revision_delta = resulting_revision - source_revision
    commit_present = commit_audit is not None and commit_audit.get("valid", False)
    commit_details = commit_audit.get("details", {}) if commit_present else {}
    commit_counts_match = None
    if commit_present:
        commit_accepted = _int_or_none(commit_details.get("accepted"))
        commit_review = _int_or_none(commit_details.get("review"))
        accepted_total = counts.get("accepted", {}).get("total")
        review_total = counts.get("review", {}).get("total")
        if None not in (commit_accepted, commit_review, accepted_total, review_total):
            commit_counts_match = (
                commit_accepted == accepted_total and commit_review == review_total
            )
            if commit_counts_match is False:
                warnings.append(_warning("commit_audit_count_mismatch",
                                         "dream.committed counts disagree with dream result"))
        else:
            warnings.append(_warning("commit_audit_counts_unverifiable",
                                     "dream.committed counts are not available for comparison"))
    elif job_completed:
        warnings.append(_warning("missing_commit_audit",
                                 "completed dream has no readable dream.committed audit"))

    checks = [
        {"name": "job_completed", "passed": job_completed},
        {"name": "dream_result_present", "passed": result_present},
        {"name": "result_json_valid", "passed": result_json_valid},
        {"name": "result_object_valid", "passed": result_object is not None},
        {"name": "revisions_present", "passed": revisions_present},
        {"name": "revisions_consistent", "passed": revisions_consistent},
    ]
    verified = all(check["passed"] for check in checks)
    if result_present and result_json_valid and result_object is None:
        warnings.append(_warning("result_not_object", "dream result JSON is not an object"))
    if result_row and result_object and revisions_present and not revisions_consistent:
        warnings.append(_warning("revision_mismatch",
                                 "dream_results columns disagree with result_json revisions"))
    if job_completed and not result_present:
        warnings.append(_warning("missing_dream_result", "completed dream has no dream_results row"))
    if not job_completed and result_present:
        warnings.append(_warning("nonterminal_result_row", "non-completed job has a dream_results row"))

    provenance_verified = bool(
        verified and commit_present and commit_counts_match is True
    )
    return {
        "verified": verified,
        "provenance_verified": provenance_verified,
        "job_completed": job_completed,
        "dream_result_present": result_present,
        "result_json_valid": result_json_valid,
        "result_object_valid": result_object is not None,
        "commit_audit_present": commit_present,
        "commit_audit_counts_match": commit_counts_match,
        "revisions_present": revisions_present,
        "revisions_consistent": revisions_consistent,
        "source_revision": source_revision,
        "resulting_revision": resulting_revision,
        "revision_changed": revision_changed,
        "revision_delta": revision_delta,
        "checks": checks,
    }


def _knowledge_impact(counts: dict[str, Any], persistence: dict[str, Any],
                      warnings: list[dict[str, str]]) -> dict[str, Any]:
    accepted = counts["accepted"]
    review = counts["review"]
    counts_verified = (
        accepted["available"]
        and review["available"]
        and accepted["unknown_items"] == 0
        and review["unknown_items"] == 0
        and accepted["invalid_items"] == 0
        and review["invalid_items"] == 0
    )
    if accepted["available"] and (accepted["unknown_items"] or accepted["invalid_items"]):
        warnings.append(_warning("unrecognized_accepted_change",
                                 "accepted result contains an unrecognized or invalid item"))
    if review["available"] and (review["unknown_items"] or review["invalid_items"]):
        warnings.append(_warning("unrecognized_review_change",
                                 "review result contains an unrecognized or invalid item"))
    assessed = bool(persistence["verified"] and counts_verified)
    impact = {
        "assessed": assessed,
        "counts_verified": counts_verified,
        "accepted_change_count": accepted["total"],
        "review_item_count": review["total"],
        "revision_changed": None,
        "revision_delta": None,
        "substantive_change": None,
        "bookkeeping_only": None,
        "review_only": None,
        "no_recorded_changes": None,
        "durable_categories": [],
        "review_categories": [],
        "learning_benefit_verified": False,
        "benefit_reason": (
            "Stored dream and audit records do not contain a downstream retrieval or "
            "behavioural-benefit measurement; persistence is not proof of learning benefit."
        ),
        "reason": None,
    }
    if not persistence["verified"]:
        impact["reason"] = "Dream persistence is not verified; no knowledge impact is claimed."
        return impact
    if not counts_verified:
        impact["reason"] = "Persisted result change counts are missing or incomplete."
        return impact
    if accepted["total"] is None or review["total"] is None:
        impact["reason"] = "Persisted result change counts are missing."
        return impact
    revision_changed = persistence["revision_changed"]
    substantive = bool(accepted["total"] > 0 and revision_changed)
    bookkeeping = bool(revision_changed and accepted["total"] == 0 and review["total"] == 0)
    review_only = bool(accepted["total"] == 0 and review["total"] > 0)
    impact.update({
        "revision_changed": revision_changed,
        "revision_delta": persistence["revision_delta"],
        "substantive_change": substantive,
        "bookkeeping_only": bookkeeping,
        "review_only": review_only,
        "no_recorded_changes": accepted["total"] == 0 and review["total"] == 0,
        "durable_categories": [
            key for key, value in accepted["counts"].items() if value > 0
        ],
        "review_categories": [
            key for key, value in review["counts"].items() if value > 0
        ],
    })
    if substantive:
        impact["reason"] = (
            "Accepted changes were persisted in the same committed revision. "
            "This verifies durable admission, not downstream benefit."
        )
    elif bookkeeping:
        impact["reason"] = (
            "The dream advanced the revision without accepted or review changes; "
            "this is bookkeeping, not learning."
        )
    elif review_only:
        impact["reason"] = (
            "No accepted durable change was persisted; the proposal was admitted "
            "for review only."
        )
    else:
        impact["reason"] = "No accepted or review changes were recorded."
    if accepted["total"] > 0 and revision_changed is False:
        warnings.append(_warning(
            "accepted_without_revision_change",
            "Accepted changes are recorded, but the source and resulting revisions are equal.",
        ))
        impact["reason"] = (
            "The persisted result admits accepted changes, but no revision change was "
            "observed; durable knowledge-state change cannot be claimed."
        )
    return impact


def _coverage(result_object: dict[str, Any] | None) -> dict[str, Any]:
    considered = _int_or_none(result_object.get("records_considered")) if result_object else None
    return {
        "evidence_items_considered": considered,
        "cognitive_units_considered": None,
        "cognitive_units_reason": (
            "Dream results persist the number of evidence items considered, not the "
            "number of cognitive units presented to the model."
        ),
    }


def _missing_fields(job: dict[str, Any], result_object: dict[str, Any] | None,
                    persistence: dict[str, Any], impact: dict[str, Any],
                    execution: dict[str, Any], coverage: dict[str, Any]) -> dict[str, str]:
    missing: dict[str, str] = {}
    if job.get("status") == "completed" and result_object is None:
        missing["dream_result"] = "No readable dream result object is stored."
    if not persistence["revisions_present"]:
        missing["revisions"] = "Source/resulting revisions are missing from one or both records."
    missing["model_identifier"] = "Dream records do not persist the model identifier."
    if execution["outcome"] == "success":
        missing["latency_seconds"] = "Successful dream results do not persist model latency."
    if coverage["evidence_items_considered"] is None:
        missing["evidence_items_considered"] = "Result does not contain an integer records_considered."
    if not impact["assessed"]:
        missing["knowledge_impact"] = str(impact.get("reason") or "Impact could not be assessed.")
    if execution["outcome"] in {"validation_failure", "timeout", "failure"}:
        missing["rejected_proposal_count"] = (
            "Rejected proposal content and per-item rejection counts are not persisted."
        )
    return missing


def _dream_report(job: dict[str, Any], result_row: dict[str, Any] | None,
                  audits: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    warnings: list[dict[str, str]] = []
    result_json_valid = False
    result_object = None
    if result_row is not None:
        result_json_valid, parsed = _parse_json(result_row.get("result_json"))
        if result_json_valid and isinstance(parsed, dict):
            result_object = parsed
    commit_audit = _last_audit(audits, "dream.committed")
    failure_audit = _last_audit(audits, "dream.failed")
    deferral_audit = _last_audit(audits, "dream.deferred")
    for operation, values in audits.items():
        if len(values) > 1:
            warnings.append(_warning("duplicate_audit",
                                     "multiple " + operation + " audits reference this job"))
    execution = _execution(job, failure_audit, deferral_audit, warnings)
    accepted = _count_changes(result_object.get("accepted") if result_object else None,
                              accepted=True)
    review = _count_changes(result_object.get("review") if result_object else None,
                            accepted=False)
    counts = {"accepted": accepted, "review": review}
    persistence = _persistence(job, result_row, result_object, result_json_valid,
                               commit_audit, counts, warnings)
    impact = _knowledge_impact(counts, persistence, warnings)
    coverage = _coverage(result_object)
    missing = _missing_fields(job, result_object, persistence, impact, execution, coverage)
    if execution["outcome"] == "validation_failure":
        proposal_rejected = True
    elif execution["outcome"] == "success":
        proposal_rejected = False
    else:
        proposal_rejected = None
    return {
        "job_id": job.get("job_id"),
        "execution": execution,
        "persistence": persistence,
        "proposals": {
            "counts_verified": impact["counts_verified"],
            "accepted_total": accepted["total"],
            "review_total": review["total"],
            "reported_change_total": (
                None if accepted["total"] is None or review["total"] is None
                else accepted["total"] + review["total"]
            ),
            "accepted_by_type": accepted["counts"],
            "review_by_type": review["counts"],
            "dispositions": {
                "accepted": accepted["total"],
                "sent_for_review": review["total"],
                "deferred": bool(execution["deferred"]),
                "proposal_rejected": proposal_rejected,
                "rejected_count": 0 if execution["outcome"] == "success" else None,
                "rejected_count_reason": (
                    "Whole-proposal rejections are recorded as job failures; "
                    "the proposal and per-item rejections are not persisted."
                ),
            },
        },
        "knowledge_impact": impact,
        "coverage": coverage,
        "model": {
            "verified": False,
            "value": None,
            "reason": "Dream records do not persist the model identifier.",
        },
        "missing_fields": missing,
        "warnings": warnings,
    }


def _current_gate(conn: sqlite3.Connection, now: float,
                  historical_failures: int, warnings: list[dict[str, str]]) -> dict[str, Any]:
    try:
        row = conn.execute("SELECT * FROM dream_state WHERE singleton=1").fetchone()
    except sqlite3.OperationalError:
        warnings.append(_warning("dream_state_unavailable", "dream_state could not be read"))
        return {
            "available": False,
            "active": None,
            "historical_failure_jobs": historical_failures,
            "historical_failures_are_not_gate_evidence": True,
        }
    if row is None:
        warnings.append(_warning("dream_state_missing", "dream_state singleton is missing"))
        return {
            "available": False,
            "active": None,
            "historical_failure_jobs": historical_failures,
            "historical_failures_are_not_gate_evidence": True,
        }
    state = dict(row)
    recovery_after = _float_or_none(state.get("recovery_after")) or 0.0
    next_allowed = _float_or_none(state.get("next_allowed")) or 0.0
    last_attempt = _float_or_none(state.get("last_attempt")) or 0.0
    last_success = _float_or_none(state.get("last_success")) or 0.0
    active = now < recovery_after
    next_allowed_blocking = now < next_allowed
    if active:
        explanation = "Recovery gate is active until " + _iso(recovery_after) + "."
    else:
        explanation = (
            "Recovery gate is inactive. Historical failed jobs are reported separately "
            "and are not evidence that the gate is currently open or closed."
        )
    return {
        "available": True,
        "active": active,
        "recovery_after": recovery_after,
        "next_allowed": next_allowed,
        "next_allowed_blocking": next_allowed_blocking,
        "failures": _int_or_none(state.get("failures")),
        "deferrals": _int_or_none(state.get("deferrals")),
        "last_failure_category": _safe_token(state.get("last_failure_category")),
        "last_attempt": last_attempt,
        "last_success": last_success,
        "reflected_revision": _int_or_none(state.get("reflected_revision")),
        "historical_failure_jobs": historical_failures,
        "historical_failures_are_not_gate_evidence": True,
        "explanation": explanation,
    }


def _iso(timestamp: float) -> str:
    return datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc).isoformat()


def _summary(dreams: list[dict[str, Any]], historical_failures: int,
             gate: dict[str, Any]) -> dict[str, Any]:
    outcomes = [dream["execution"]["outcome"] for dream in dreams]
    impacts = [dream["knowledge_impact"] for dream in dreams]
    return {
        "recorded_dreams": len(dreams),
        "successful_executions": outcomes.count("success"),
        "failed_executions": sum(outcome in {"timeout", "validation_failure", "failure"}
                                 for outcome in outcomes),
        "deferred_or_conflicted": sum(outcome in {"deferral", "conflict", "deferred"}
                                      for outcome in outcomes),
        "pending_or_leased": sum(outcome in {"pending", "leased"} for outcome in outcomes),
        "persistence_verified": sum(dream["persistence"]["verified"] for dream in dreams),
        "provenance_verified": sum(dream["persistence"]["provenance_verified"]
                                   for dream in dreams),
        "substantive_changes": sum(impact["substantive_change"] is True for impact in impacts),
        "bookkeeping_only": sum(impact["bookkeeping_only"] is True for impact in impacts),
        "review_only": sum(impact["review_only"] is True for impact in impacts),
        "no_recorded_changes": sum(impact["no_recorded_changes"] is True for impact in impacts),
        "historical_failure_jobs": historical_failures,
        "current_gate_active": gate.get("active"),
    }


def _limitations(schema_version: int, gate: dict[str, Any]) -> list[str]:
    limitations = [
        "Read-only: no dream, model call, migration, config change, or background work is triggered.",
        "Proposal text, evidence payloads, prompts, memory contents, and raw job payloads are never included.",
        "Successful dream results do not persist the model identifier or latency.",
        "Cognitive units considered are not persisted; only the evidence item count is available.",
        "Rejected proposal content and per-item rejection counts are not persisted.",
        "Persistence verification proves durable admission; it does not prove downstream learning benefit.",
        "Historical failed jobs are reported separately from the current recovery gate.",
        "The report reads jobs, dream_results, audit_events, and dream_state only.",
    ]
    if schema_version < 5:
        limitations.append(
            "Schema " + str(schema_version) + " predates sourced memory tables; this report does not require them."
        )
    if not gate.get("available"):
        limitations.append("Current gate state is unavailable; no gate conclusion is claimed.")
    return limitations


def build_recent_report(conn: sqlite3.Connection, *, limit: int = 20,
                        job_ids: Iterable[str] | None = None,
                        now: float | None = None) -> dict[str, Any]:
    """Build a read-only report over the requested or most recent dream jobs."""
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
        raise ValueError("limit must be an integer between 1 and 1000")
    evaluated_at = time.time() if now is None else float(now)
    warnings: list[dict[str, str]] = []
    schema_version = current_schema_version(conn)
    job_count = int(conn.execute(
        "SELECT count(*) FROM jobs WHERE job_type='dream'"
    ).fetchone()[0])
    historical_failures = int(conn.execute(
        "SELECT count(*) FROM jobs WHERE job_type='dream' AND status='failed'"
    ).fetchone()[0])

    requested: list[str] | None = None
    if job_ids is not None:
        requested = []
        seen = set()
        for job_id in job_ids:
            if not isinstance(job_id, str) or not job_id:
                raise ValueError("job ids must be non-empty strings")
            if job_id not in seen:
                requested.append(job_id)
                seen.add(job_id)
        if len(requested) > 500:
            raise ValueError("at most 500 job ids may be requested")
        placeholders = ",".join("?" for _ in requested)
        rows = (conn.execute(
            "SELECT * FROM jobs WHERE job_type='dream' AND job_id IN (" + placeholders + ")",
            requested,
        ).fetchall() if requested else [])
        by_id = {row["job_id"]: dict(row) for row in rows}
        for job_id in requested:
            if job_id not in by_id:
                warnings.append(_warning("requested_job_missing",
                                         "no dream job exists for requested id " + job_id))
        jobs = [by_id[job_id] for job_id in requested if job_id in by_id]
        mode = "job_ids"
    else:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE job_type='dream' "
            "ORDER BY created_at DESC,job_id DESC LIMIT ?", (limit,)
        ).fetchall()
        jobs = [dict(row) for row in rows]
        mode = "recent"

    selected_ids = [job["job_id"] for job in jobs]
    if selected_ids:
        placeholders = ",".join("?" for _ in selected_ids)
        result_rows = conn.execute(
            "SELECT job_id,source_revision,resulting_revision,result_json "
            "FROM dream_results WHERE job_id IN (" + placeholders + ")",
            selected_ids,
        ).fetchall()
        results = {row["job_id"]: dict(row) for row in result_rows}
        audits = _audit_index(conn, selected_ids)
    else:
        results = {}
        audits = {}
    dreams = [
        _dream_report(job, results.get(job["job_id"]), audits.get(job["job_id"], {}))
        for job in jobs
    ]
    gate = _current_gate(conn, evaluated_at, historical_failures, warnings)
    if schema_version < 5:
        warnings.append(_warning("older_schema",
                                 "schema " + str(schema_version) + " is readable by this report"))
    return {
        "report": REPORT_NAME,
        "report_version": REPORT_VERSION,
        "generated_at": _iso(evaluated_at),
        "evaluated_at": evaluated_at,
        "read_only": True,
        "database_schema_version": schema_version,
        "selection": {
            "mode": mode,
            "limit": None if requested is not None else limit,
            "requested_job_ids": requested,
            "selected_job_ids": selected_ids,
            "dream_jobs_available": job_count,
        },
        "current_gate": gate,
        "summary": _summary(dreams, historical_failures, gate),
        "dreams": dreams,
        "limitations": _limitations(schema_version, gate),
        "warnings": warnings,
    }


def build_dream_report(conn: sqlite3.Connection, job_id: str,
                       *, now: float | None = None) -> dict[str, Any]:
    """Build a report for one dream job, raising LookupError when absent."""
    report = build_recent_report(conn, job_ids=[job_id], now=now)
    if not report["dreams"]:
        raise LookupError("no dream job exists for requested id " + job_id)
    return report["dreams"][0]


def load_report(database_path: str | Path, *, limit: int = 20,
                job_ids: Iterable[str] | None = None,
                now: float | None = None) -> dict[str, Any]:
    """Open the database read-only and build the report."""
    database = Database(database_path)
    with database.connect(readonly=True) as conn:
        return build_recent_report(conn, limit=limit, job_ids=job_ids, now=now)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Minisa Dream Quality Report")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--job-id", action="append", dest="job_ids",
                        help="report one job id; repeat for multiple jobs")
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= 1000:
        parser.error("--limit must be between 1 and 1000")
    report = load_report(args.database, limit=args.limit, job_ids=args.job_ids)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
