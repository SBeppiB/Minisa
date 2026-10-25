"""Read-only Dream Provenance report.

The report traces persisted accepted dream changes back to their cited evidence,
forward to their validation and persistence outcome, and on to a resulting
revision/record where the stored data supports such a link.

It reads only ``jobs``, ``dream_results``, ``audit_events``, ``dream_state``,
``workspace_evidence``, and, when present, the resulting record tables. It never
writes, triggers a dream, contacts a model, or includes proposal text, evidence
contents, prompts, memory contents, or raw job payloads.

Evidence links come only from the evidence identifiers persisted in an accepted
change. Temporal proximity and textual similarity are never used to infer a
source link. A provenance-complete result still does not verify downstream
learning benefit.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from core.db import Database, canonical_json
from runtime.cognition.dream_quality import build_recent_report

REPORT_NAME = "dream_provenance"
REPORT_VERSION = 1
AUDIT_OPERATIONS = (
    "dream.scheduled",
    "dream.failed",
    "dream.deferred",
    "dream.committed",
    "dream.change",
    "dream.memory_persisted",
    "dream.review",
    "dream.review_associated",
)


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


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _safe_token(value: Any, limit: int = 128) -> str | None:
    if not isinstance(value, str) or not 0 < len(value) <= limit:
        return None
    if not value.isascii() or not all(
        ch.isalnum() or ch in "_-.:" for ch in value
    ):
        return None
    return value


def _warning(code: str, detail: str) -> dict[str, str]:
    return {"code": code, "detail": detail}


def _link(link: str, status: str, code: str, detail: str,
          references: list[str] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "link": link,
        "status": status,
        "code": code,
        "detail": detail,
    }
    if references is not None:
        result["references"] = references
    return result


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _load_audits(conn: sqlite3.Connection,
                 job_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    if not job_ids:
        return {}
    placeholders = ",".join("?" for _ in job_ids)
    rows = conn.execute(
        "SELECT event_id,global_revision,occurred_at,operation,target_id,"
        "details_json FROM audit_events WHERE target_id IN (" + placeholders + ") "
        "AND operation IN (" + ",".join("?" for _ in AUDIT_OPERATIONS) + ") "
        "ORDER BY occurred_at,event_id",
        [*job_ids, *AUDIT_OPERATIONS],
    ).fetchall()
    index: dict[str, list[dict[str, Any]]] = {job_id: [] for job_id in job_ids}
    for row in rows:
        details = _parse_object(row["details_json"])
        index.setdefault(row["target_id"], []).append({
            "event_id": _safe_token(row["event_id"]),
            "operation": row["operation"],
            "global_revision": _int_or_none(row["global_revision"]),
            "occurred_at": row["occurred_at"],
            "details": details,
            "valid_details": details is not None,
        })
    return index


def _audit_reference(event: dict[str, Any], *,
                     evidence_match: bool | None = None) -> dict[str, Any]:
    reference = {
        "event_id": event["event_id"],
        "operation": event["operation"],
        "global_revision": event["global_revision"],
        "details_valid": event["valid_details"],
    }
    if evidence_match is not None:
        reference["evidence_match"] = evidence_match
    return reference


def _accepted_changes(result_object: dict[str, Any] | None) -> list[Any] | None:
    if not isinstance(result_object, dict):
        return None
    accepted = result_object.get("accepted")
    return accepted if isinstance(accepted, list) else None


def _validation_state(quality_dream: dict[str, Any],
                      accepted: list[Any] | None,
                      review: list[Any] | None) -> dict[str, Any]:
    outcome = quality_dream["execution"]["outcome"]
    if outcome == "validation_failure":
        return {
            "outcome": "failed",
            "basis": (
                "The job failed at proposal validation/output validation; the "
                "whole proposal was rejected and per-item decisions are not persisted."
            ),
            "accepted_total": None,
            "review_total": None,
            "rejected_total": None,
            "per_item_decisions": "unavailable",
        }
    if outcome == "success" and accepted is not None and review is not None:
        if accepted and review:
            basis = "Persisted result lists both accepted and review-only changes."
        elif accepted:
            basis = "Persisted result lists accepted changes after whole-proposal validation."
        elif review:
            basis = "Persisted result lists review-only changes; no accepted durable change."
        else:
            basis = "Persisted result lists no accepted or review-only changes."
        return {
            "outcome": "passed",
            "basis": basis,
            "accepted_total": len(accepted),
            "review_total": len(review),
            "rejected_total": None,
            "per_item_decisions": (
                "accepted/review from persisted result; per-item rejection counts "
                "are not persisted"
            ),
        }
    return {
        "outcome": "unavailable",
        "basis": (
            "No persisted proposal validation decision is available for this job outcome."
        ),
        "accepted_total": None,
        "review_total": None,
        "rejected_total": None,
        "per_item_decisions": "unavailable",
    }


def _change_metadata(change: Any) -> dict[str, Any]:
    """Only safe structural metadata; never proposal text or claims."""
    if not isinstance(change, dict):
        return {
            "type": None,
            "change_kind": None,
            "review_target": None,
            "readable": False,
        }
    kind = _safe_token(change.get("type"))
    metadata: dict[str, Any] = {
        "type": kind,
        "change_kind": None,
        "review_target": None,
        "readable": kind is not None,
    }
    if kind == "unit":
        metadata["change_kind"] = _safe_token(change.get("kind"))
    elif kind == "review":
        metadata["review_target"] = _safe_token(change.get("target"))
    elif kind == "association":
        metadata["change_kind"] = "association"
    return metadata


def _evidence_ids(change: Any) -> tuple[list[str], int, int, bool]:
    """Return safe cited IDs, invalid count, duplicate count, availability."""
    if not isinstance(change, dict) or "evidence" not in change:
        return [], 0, 0, False
    raw = change.get("evidence")
    if not isinstance(raw, list):
        return [], 0, 0, False
    values: list[str] = []
    invalid = 0
    for value in raw:
        token = _safe_token(value)
        if token is None:
            invalid += 1
        else:
            values.append(token)
    unique = sorted(set(values))
    return unique, invalid, len(values) - len(unique), True


def _evidence_link(change: Any, evidence_records: dict[str, str],
                   issues: list[dict[str, Any]]) -> dict[str, Any]:
    ids, invalid, duplicates, available = _evidence_ids(change)
    if not available:
        issues.append(_link(
            "source_evidence", "unavailable", "missing_evidence_citation",
            "The accepted change does not persist an evidence reference list.",
        ))
        return {
            "link": "unavailable",
            "status": "unavailable",
            "basis": "No persisted evidence citation exists for this change.",
            "evidence_ids": [],
            "resolved": [],
            "missing_ids": [],
            "invalid_reference_count": invalid,
            "duplicate_reference_count": duplicates,
            "inferred": False,
        }
    resolved = [
        {"evidence_id": evidence_id, "category": evidence_records[evidence_id]}
        for evidence_id in ids if evidence_id in evidence_records
    ]
    missing = [evidence_id for evidence_id in ids if evidence_id not in evidence_records]
    if not ids:
        status = "unavailable"
        basis = "The persisted evidence list has no usable identifiers."
        issues.append(_link(
            "source_evidence", "unavailable", "no_usable_evidence_reference",
            "The persisted evidence list has no safe, usable identifiers.",
        ))
    elif missing:
        status = "direct_with_missing_records"
        basis = (
            "Evidence IDs are cited directly by the persisted accepted change; "
            "some referenced workspace_evidence rows are missing."
        )
        issues.append(_link(
            "source_evidence", "missing", "evidence_record_missing",
            "Cited evidence identifiers have no persisted workspace_evidence row.",
            missing,
        ))
    elif invalid or duplicates:
        status = "direct_with_incomplete_references"
        basis = (
            "Evidence IDs are cited directly by the persisted accepted change; "
            "the citation list contains invalid or duplicate references."
        )
        issues.append(_link(
            "source_evidence", "ambiguous", "invalid_evidence_reference",
            "The persisted evidence citation list contains invalid or duplicate references.",
        ))
    else:
        status = "direct_and_resolved"
        basis = (
            "Evidence IDs are cited directly by the persisted accepted change and "
            "resolve to persisted workspace_evidence rows."
        )
    return {
        "link": "direct",
        "status": status,
        "basis": basis,
        "evidence_ids": ids,
        "resolved": resolved,
        "missing_ids": missing,
        "invalid_reference_count": invalid,
        "duplicate_reference_count": duplicates,
        "inferred": False,
    }


def _matching_change_audits(change: Any,
                            audits: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    if not isinstance(change, dict):
        return [], "missing"
    exact = [
        event for event in audits
        if event["operation"] == "dream.change" and event["valid_details"]
        and event["details"] == change
    ]
    if exact:
        return exact, "direct" if len(exact) == 1 else "ambiguous"

    kind = change.get("type")
    memory_target = None
    if kind == "memory":
        memory_target = "memory"
    elif kind == "review" and change.get("target") == "memory_consolidation":
        memory_target = "memory_consolidation"
    if memory_target is None:
        return [], "missing"

    evidence, _invalid, _duplicates, _available = _evidence_ids(change)
    wanted = set(evidence)
    candidates = []
    for event in audits:
        if event["operation"] != "dream.memory_persisted" or not event["valid_details"]:
            continue
        details = event["details"]
        if details.get("target") != memory_target:
            continue
        raw = details.get("evidence")
        if not isinstance(raw, list):
            continue
        raw_ids = {_safe_token(value) for value in raw}
        raw_ids.discard(None)
        if raw_ids == wanted:
            candidates.append(event)
    if len(candidates) == 1:
        return candidates, "direct"
    if len(candidates) > 1:
        return candidates, "ambiguous"
    return [], "missing"


def _audit_link(change: Any, audits: list[dict[str, Any]],
                issues: list[dict[str, Any]]) -> dict[str, Any]:
    candidates, status = _matching_change_audits(change, audits)
    references = [
        _audit_reference(event, evidence_match=True)
        for event in candidates
    ]
    if status == "direct":
        code = "change_audit_linked"
        detail = "A persisted audit event directly records this accepted change."
        issue_status = "complete"
    elif status == "ambiguous":
        code = "ambiguous_change_audit"
        detail = (
            "Multiple persisted audit events could correspond to this accepted change; "
            "no single audit link is asserted."
        )
        issues.append(_link(
            "change_audit", "ambiguous", code, detail,
            [reference["event_id"] for reference in references
             if reference["event_id"] is not None],
        ))
        issue_status = "ambiguous"
    else:
        code = "change_audit_missing"
        detail = (
            "No matching per-change audit event was found; the accepted change is "
            "known only from the persisted dream result."
        )
        issues.append(_link("change_audit", "missing", code, detail))
        issue_status = "missing"
    return {
        "status": status,
        "basis": detail,
        "events": references,
        "issue_status": issue_status,
    }


def _record_from_memory(conn: sqlite3.Connection, change: Any,
                        audits: list[dict[str, Any]],
                        resulting_revision: int | None,
                        issues: list[dict[str, Any]]) -> dict[str, Any]:
    memory_ids = sorted({
        _safe_token(event["details"].get("memory_id"))
        for event in audits
        if event["operation"] == "dream.memory_persisted"
        and event["valid_details"] and _safe_token(event["details"].get("memory_id"))
    })
    if not memory_ids:
        return {
            "status": "unavailable",
            "kind": "sourced_memory",
            "id": None,
            "revision": None,
            "link": "unavailable",
            "basis": "No persisted memory identifier is available.",
        }
    if len(memory_ids) > 1:
        issues.append(_link(
            "resulting_record", "ambiguous", "ambiguous_memory_record",
            "Multiple memory identifiers could correspond to this accepted change.",
            memory_ids,
        ))
        return {
            "status": "ambiguous",
            "kind": "sourced_memory",
            "id": None,
            "revision": None,
            "link": "direct_audit",
            "basis": "Multiple persisted memory identifiers are candidates.",
        }
    memory_id = memory_ids[0]
    if not _table_exists(conn, "memory_records"):
        return {
            "status": "record_table_unavailable",
            "kind": "sourced_memory",
            "id": memory_id,
            "revision": None,
            "link": "direct_audit",
            "basis": (
                "The audit persists a memory identifier, but memory_records is not "
                "present in this schema."
            ),
        }
    row = conn.execute(
        "SELECT memory_id,revision FROM memory_records "
        "WHERE memory_id=? OR id=? ORDER BY revision LIMIT 1",
        (memory_id, memory_id),
    ).fetchone()
    if row is None:
        issues.append(_link(
            "resulting_record", "missing", "memory_record_missing",
            "The audited memory identifier has no persisted memory_records row.",
            [memory_id],
        ))
        return {
            "status": "record_missing",
            "kind": "sourced_memory",
            "id": memory_id,
            "revision": None,
            "link": "direct_audit",
            "basis": "The audit names a memory record that is not present.",
        }
    revision = _int_or_none(row["revision"])
    if resulting_revision is not None and revision != resulting_revision:
        issues.append(_link(
            "resulting_record", "ambiguous", "memory_revision_mismatch",
            "The memory record revision does not match the dream resulting revision.",
            [memory_id],
        ))
        status = "revision_mismatch"
    else:
        status = "verified"
    return {
        "status": status,
        "kind": "sourced_memory",
        "id": memory_id,
        "revision": revision,
        "link": "direct_audit",
        "basis": "The memory identifier is persisted by the change audit and resolves.",
    }


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute("PRAGMA table_info(" + table + ")")}


def _record_from_derived_key(conn: sqlite3.Connection, change: Any,
                             resulting_revision: int | None,
                             issues: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not isinstance(change, dict):
        return None
    kind = change.get("type")
    key = hashlib.sha256(canonical_json(change).encode()).hexdigest()
    row = None
    revision = None
    if kind == "procedure":
        kind_name = "learned_procedure"
        record_kind = kind_name
        if not _table_exists(conn, "learned_procedures"):
            return {
                "status": "table_unavailable",
                "kind": kind_name,
                "id": None,
                "revision": None,
                "link": "derived_existing_convention",
                "basis": "The learned_procedures table is not present in this schema.",
            }
        columns = _table_columns(conn, "learned_procedures")
        revision_column = (
            "global_revision" if "global_revision" in columns
            else "revision" if "revision" in columns else None
        )
        if revision_column is None:
            return {
                "status": "table_unavailable",
                "kind": kind_name,
                "id": None,
                "revision": None,
                "link": "derived_existing_convention",
                "basis": "The learned_procedures table has no known revision column.",
            }
        row = conn.execute(
            "SELECT procedure_id," + revision_column + " AS resolved_revision "
            "FROM learned_procedures WHERE procedure_id=?", (key,),
        ).fetchone()
        record_id = key if row is not None else None
        revision = _int_or_none(row["resolved_revision"]) if row is not None else None
    elif kind in {"unit", "association"}:
        kind_name = "workspace_unit"
        record_kind = kind_name
        if kind == "unit" and change.get("kind") == "question":
            from runtime.cognition.workspace import (
                normalized_question,
                question_identity,
            )
            try:
                identity = question_identity(change.get("subject"))
                normalized_question(change.get("subject"))
            except (TypeError, ValueError):
                return None
            keys = [identity]
            if resulting_revision is not None:
                keys.append(identity + ":episode:" + str(resulting_revision))
            placeholders = ",".join("?" for _ in keys)
            rows = conn.execute(
                "SELECT unit_id,revision FROM workspace_units WHERE dedup_key IN ("
                + placeholders + ") ORDER BY revision,unit_id",
                keys,
            ).fetchall()
            if resulting_revision is not None:
                rows = [
                    row for row in rows
                    if _int_or_none(row["revision"]) is not None
                    and row["revision"] >= resulting_revision
                ]
            if rows:
                first_revision = rows[0]["revision"]
                same_revision = [
                    row for row in rows if row["revision"] == first_revision
                ]
                if len(same_revision) > 1:
                    issues.append(_link(
                        "resulting_record", "ambiguous", "ambiguous_unit_record",
                        "Multiple workspace units match the derived question identity.",
                    ))
                    return {
                        "status": "ambiguous",
                        "kind": kind_name,
                        "id": None,
                        "revision": None,
                        "link": "derived_existing_convention",
                        "basis": "Multiple workspace units match the derived identity.",
                    }
                row = rows[0]
        else:
            dedup_key = "dream:" + key
            row = conn.execute(
                "SELECT unit_id,revision FROM workspace_units WHERE dedup_key=?",
                (dedup_key,),
            ).fetchone()
        record_id = row["unit_id"] if row is not None else None
        revision = _int_or_none(row["revision"]) if row is not None else None
    else:
        return None

    if record_id is None:
        return {
            "status": "unavailable",
            "kind": kind_name,
            "id": None,
            "revision": None,
            "link": "derived_existing_convention",
            "basis": (
                "No directly referenced record was persisted; the deterministic "
                "workspace/procedure identity does not resolve to a row."
            ),
        }
    later_workspace_record = (
        resulting_revision is not None and revision is not None
        and revision > resulting_revision and record_kind == "workspace_unit"
    )
    if resulting_revision is not None and revision == resulting_revision:
        status = "verified"
        basis = (
            "The record identifier follows the existing deterministic workspace/"
            "procedure convention and resolves at the dream revision."
        )
    elif later_workspace_record:
        status = "verified_later_revision"
        basis = (
            "The deterministic workspace record exists at a later revision, which "
            "is expected when the same unit was activated again after this dream."
        )
    elif resulting_revision is not None:
        issues.append(_link(
            "resulting_record", "ambiguous", "record_revision_mismatch",
            "The derived resulting record revision does not match the dream revision.",
            [record_id],
        ))
        status = "revision_mismatch"
        basis = "The derived record revision does not match the dream revision."
    else:
        status = "unverified_revision"
        basis = "The dream resulting revision is unavailable for a record check."
    return {
        "status": status,
        "kind": kind_name,
        "id": record_id,
        "revision": revision,
        "link": "derived_existing_convention",
        "basis": basis,
    }

def _resulting_record(conn: sqlite3.Connection, change: Any,
                      audits: list[dict[str, Any]],
                      resulting_revision: int | None,
                      issues: list[dict[str, Any]]) -> dict[str, Any]:
    if isinstance(change, dict):
        kind = change.get("type")
        if kind == "memory" or (
            kind == "review" and change.get("target") == "memory_consolidation"
        ):
            return _record_from_memory(conn, change, audits, resulting_revision, issues)
        derived = _record_from_derived_key(
            conn, change, resulting_revision, issues
        )
        if derived is not None:
            return derived
    return {
        "status": "unavailable",
        "kind": None,
        "id": None,
        "revision": None,
        "link": "unavailable",
        "basis": "No direct or deterministic resulting-record link is defined.",
    }


def _trace_status(evidence: dict[str, Any], audit: dict[str, Any],
                  persistence_status: str,
                  resulting_record: dict[str, Any]) -> str:
    if persistence_status == "unverified":
        return "unverified"
    if (persistence_status == "ambiguous"
            or audit["status"] == "ambiguous"
            or evidence["status"] in {"direct_with_incomplete_references"}
            or resulting_record["status"] == "ambiguous"):
        return "ambiguous"
    if (evidence["status"] != "direct_and_resolved"
            or audit["status"] != "direct"
            or persistence_status != "verified"):
        return "partial"
    if resulting_record["status"] not in {"verified", "verified_later_revision"}:
        return "partial"
    return "complete"


def _change_persistence(quality_persistence: dict[str, Any],
                        audit_status: str,
                        result_copy_state: dict[str, Any]) -> tuple[str, str]:
    if not quality_persistence["verified"]:
        return (
            "unverified",
            "Dream result/revision persistence is not verified for this job.",
        )
    if result_copy_state.get("consistent") is False:
        return (
            "ambiguous",
            "jobs.result_json and dream_results.result_json disagree.",
        )
    if audit_status == "direct":
        return (
            "verified",
            "Accepted change is present in the persisted dream result and a "
            "matching per-change audit was found at the resulting revision.",
        )
    if audit_status == "ambiguous":
        return (
            "ambiguous",
            "Accepted change is persisted in the result, but its per-change audit "
            "is ambiguous.",
        )
    return (
        "partial",
        "Accepted change is persisted in the dream result, but no matching "
        "per-change audit was found.",
    )


def _result_copy_state(job_row: dict[str, Any] | None,
                       result_row: dict[str, Any] | None,
                       result_object: dict[str, Any] | None,
                       warnings: list[dict[str, str]]) -> dict[str, Any]:
    job_result_present = bool(job_row and job_row.get("result_json") is not None)
    dream_result_present = result_row is not None
    job_object = None
    job_json_valid = None
    if job_result_present:
        job_json_valid, parsed = _parse_json(job_row.get("result_json"))
        if job_json_valid and isinstance(parsed, dict):
            job_object = parsed
        else:
            warnings.append(_warning(
                "invalid_job_result_copy",
                "jobs.result_json is not a readable JSON object.",
            ))
    if not job_result_present or not dream_result_present:
        consistent = None
    elif job_object is None or result_object is None:
        consistent = False
    else:
        consistent = job_object == result_object
    if consistent is False and job_json_valid is not False:
        warnings.append(_warning(
            "result_copy_mismatch",
            "jobs.result_json and dream_results.result_json disagree.",
        ))
    return {
        "job_result_present": job_result_present,
        "dream_result_present": dream_result_present,
        "consistent": consistent,
        "job_result_json_valid": job_json_valid,
    }


def _provenance_dream(conn: sqlite3.Connection,
                      quality_dream: dict[str, Any],
                      job_row: dict[str, Any] | None,
                      result_row: dict[str, Any] | None,
                      result_object: dict[str, Any] | None,
                      audits: list[dict[str, Any]],
                      evidence_records: dict[str, str],
                      warnings: list[dict[str, str]]) -> dict[str, Any]:
    job_id = quality_dream["job_id"]
    issues: list[dict[str, Any]] = []
    accepted = _accepted_changes(result_object)
    review = None
    if isinstance(result_object, dict) and isinstance(result_object.get("review"), list):
        review = result_object["review"]
    validation = _validation_state(quality_dream, accepted, review)
    if validation["outcome"] == "unavailable":
        issues.append(_link(
            "proposal_validation", "unavailable", "validation_decision_unavailable",
            validation["basis"],
        ))
    result_copy = _result_copy_state(job_row, result_row, result_object, warnings)
    if result_copy["consistent"] is False:
        issues.append(_link(
            "result_copies", "ambiguous", "result_copy_mismatch",
            "The two persisted copies of the dream result disagree.",
        ))

    revision = quality_dream["persistence"].get("resulting_revision")
    changes: list[dict[str, Any]] = []
    if accepted is None:
        issues.append(_link(
            "accepted_changes", "unavailable", "accepted_changes_unavailable",
            "The persisted dream result does not contain a readable accepted-change list.",
        ))
    else:
        for index, change in enumerate(accepted):
            metadata = _change_metadata(change)
            evidence = _evidence_link(change, evidence_records, issues)
            audit = _audit_link(change, audits, issues)
            persistence_status, persistence_basis = _change_persistence(
                quality_dream["persistence"], audit["status"], result_copy
            )
            record = _resulting_record(
                conn, change, audit["events"] and [
                    event for event in audits
                    if event["operation"] in {"dream.change", "dream.memory_persisted"}
                    and event["event_id"] in {
                        reference["event_id"] for reference in audit["events"]
                    }
                ] or [], revision, issues,
            )
            trace_status = _trace_status(
                evidence, audit, persistence_status, record
            )
            changes.append({
                "index": index,
                "type": metadata["type"],
                "change_kind": metadata["change_kind"],
                "review_target": metadata["review_target"],
                "validation": {
                    "decision": "accepted",
                    "basis": "Listed in the persisted dream_results accepted changes.",
                },
                "evidence": evidence,
                "audit": audit,
                "persistence": {
                    "status": persistence_status,
                    "basis": persistence_basis,
                },
                "resulting_record": record,
                "trace_status": trace_status,
            })

    if changes:
        if any(change["trace_status"] == "ambiguous" for change in changes):
            trace_status = "ambiguous"
        elif any(change["trace_status"] == "partial" for change in changes):
            trace_status = "partial"
        elif all(change["trace_status"] == "complete" for change in changes):
            trace_status = "complete"
        else:
            trace_status = "unverified"
    elif validation["outcome"] == "passed":
        trace_status = "no_accepted_changes"
    else:
        trace_status = validation["outcome"]

    if (quality_dream["knowledge_impact"].get("substantive_change") is True
            and quality_dream["persistence"].get("revision_changed") is False):
        issues.append(_link(
            "observed_state_change", "ambiguous", "accepted_without_revision_change",
            "Accepted changes are recorded without an observed revision change.",
        ))

    job_events = [_audit_reference(event) for event in audits]
    return {
        "job_id": job_id,
        "execution": quality_dream["execution"],
        "validation": validation,
        "persistence": quality_dream["persistence"],
        "observed_state_change": {
            "assessed": quality_dream["knowledge_impact"]["assessed"],
            "revision_changed": quality_dream["knowledge_impact"]["revision_changed"],
            "revision_delta": quality_dream["knowledge_impact"]["revision_delta"],
            "substantive_change": quality_dream["knowledge_impact"]["substantive_change"],
            "bookkeeping_only": quality_dream["knowledge_impact"]["bookkeeping_only"],
            "review_only": quality_dream["knowledge_impact"]["review_only"],
            "no_recorded_changes": quality_dream["knowledge_impact"]["no_recorded_changes"],
        },
        "learning_benefit": {
            "verified": False,
            "reason": (
                "Provenance links and durable persistence do not establish "
                "downstream retrieval or behavioural benefit."
            ),
        },
        "accepted_changes": changes,
        "result_copies": result_copy,
        "audit_references": {
            "job_events": job_events,
        },
        "trace_status": trace_status,
        "missing_or_ambiguous_links": issues,
        "warnings": [*quality_dream["warnings"], *warnings],
    }


def _summary(dreams: list[dict[str, Any]],
             quality_summary: dict[str, Any]) -> dict[str, Any]:
    changes = [
        change for dream in dreams
        for change in dream["accepted_changes"]
    ]
    return {
        "recorded_dreams": len(dreams),
        "accepted_changes": len(changes),
        "accepted_changes_complete": sum(
            change["trace_status"] == "complete" for change in changes
        ),
        "accepted_changes_partial": sum(
            change["trace_status"] == "partial" for change in changes
        ),
        "accepted_changes_ambiguous": sum(
            change["trace_status"] == "ambiguous" for change in changes
        ),
        "accepted_changes_unverified": sum(
            change["trace_status"] == "unverified" for change in changes
        ),
        "accepted_changes_with_direct_evidence": sum(
            change["evidence"]["link"] == "direct" for change in changes
        ),
        "accepted_changes_with_resolved_evidence": sum(
            change["evidence"]["status"] == "direct_and_resolved"
            for change in changes
        ),
        "accepted_changes_with_verified_persistence": sum(
            change["persistence"]["status"] == "verified" for change in changes
        ),
        "validation_passed": sum(
            dream["validation"]["outcome"] == "passed" for dream in dreams
        ),
        "validation_failed": sum(
            dream["validation"]["outcome"] == "failed" for dream in dreams
        ),
        "validation_unavailable": sum(
            dream["validation"]["outcome"] == "unavailable" for dream in dreams
        ),
        "durable_persistence_verified": quality_summary["persistence_verified"],
        "observed_substantive_changes": quality_summary["substantive_changes"],
        "historical_failure_jobs": quality_summary["historical_failure_jobs"],
        "current_gate_active": quality_summary["current_gate_active"],
    }


def _limitations(quality_limitations: list[str],
                 gate: dict[str, Any]) -> list[str]:
    limitations = list(quality_limitations)
    limitations.extend([
        "Evidence links come only from evidence identifiers persisted in accepted changes; "
        "temporal or textual proximity is never used.",
        "The dream snapshot itself is not persisted, so snapshot membership is not "
        "independently reconstructable.",
        "Proposal text for failed or deferred jobs and per-item rejection decisions are "
        "not persisted.",
        "Resulting-record links are direct only where an audit supplies an identifier; "
        "otherwise the existing deterministic record convention is stated explicitly.",
        "Provenance completeness does not verify downstream learning benefit.",
    ])
    if not gate.get("available"):
        limitations.append(
            "Current gate state is unavailable; historical failures are not gate evidence."
        )
    return limitations


def build_provenance_report(conn: sqlite3.Connection, *, limit: int = 20,
                            job_ids: Iterable[str] | None = None,
                            now: float | None = None) -> dict[str, Any]:
    """Build a read-only provenance report over the requested or recent dreams."""
    quality = build_recent_report(conn, limit=limit, job_ids=job_ids, now=now)
    selected_ids = quality["selection"]["selected_job_ids"]
    if selected_ids:
        placeholders = ",".join("?" for _ in selected_ids)
        result_rows = conn.execute(
            "SELECT job_id,source_revision,resulting_revision,result_json "
            "FROM dream_results WHERE job_id IN (" + placeholders + ")",
            selected_ids,
        ).fetchall()
        results = {row["job_id"]: dict(row) for row in result_rows}
        job_rows = conn.execute(
            "SELECT job_id,result_json FROM jobs WHERE job_id IN (" + placeholders + ")",
            selected_ids,
        ).fetchall()
        jobs = {row["job_id"]: dict(row) for row in job_rows}
        audits = _load_audits(conn, selected_ids)
    else:
        results = {}
        jobs = {}
        audits = {}

    parsed_results: dict[str, dict[str, Any] | None] = {}
    evidence_ids: set[str] = set()
    for job_id in selected_ids:
        result_row = results.get(job_id)
        result_object = None
        if result_row is not None:
            valid, parsed = _parse_json(result_row.get("result_json"))
            if valid and isinstance(parsed, dict):
                result_object = parsed
        parsed_results[job_id] = result_object
        accepted = _accepted_changes(result_object)
        if accepted is not None:
            for change in accepted:
                ids, _invalid, _duplicates, _available = _evidence_ids(change)
                evidence_ids.update(ids)

    evidence_records: dict[str, str] = {}
    if evidence_ids:
        placeholders = ",".join("?" for _ in evidence_ids)
        rows = conn.execute(
            "SELECT evidence_id,category FROM workspace_evidence WHERE evidence_id IN ("
            + placeholders + ")",
            sorted(evidence_ids),
        ).fetchall()
        for row in rows:
            category = _safe_token(row["category"])
            evidence_records[row["evidence_id"]] = category
    else:
        rows = []

    dreams = []
    top_warnings = list(quality["warnings"])
    for quality_dream in quality["dreams"]:
        job_id = quality_dream["job_id"]
        warnings: list[dict[str, str]] = []
        dreams.append(_provenance_dream(
            conn, quality_dream, jobs.get(job_id), results.get(job_id),
            parsed_results.get(job_id), audits.get(job_id, []),
            evidence_records, warnings,
        ))
        top_warnings.extend(warnings)

    return {
        "report": REPORT_NAME,
        "report_version": REPORT_VERSION,
        "generated_at": quality["generated_at"],
        "evaluated_at": quality["evaluated_at"],
        "read_only": True,
        "database_schema_version": quality["database_schema_version"],
        "selection": quality["selection"],
        "current_gate": quality["current_gate"],
        "summary": _summary(dreams, quality["summary"]),
        "dreams": dreams,
        "limitations": _limitations(quality["limitations"], quality["current_gate"]),
        "warnings": top_warnings,
    }


def build_job_provenance(conn: sqlite3.Connection, job_id: str,
                         *, now: float | None = None) -> dict[str, Any]:
    """Build provenance for one dream job, raising LookupError when absent."""
    report = build_provenance_report(conn, job_ids=[job_id], now=now)
    if not report["dreams"]:
        raise LookupError("no dream job exists for requested id " + job_id)
    return report["dreams"][0]


def load_provenance_report(database_path: str | Path, *, limit: int = 20,
                           job_ids: Iterable[str] | None = None,
                           now: float | None = None) -> dict[str, Any]:
    """Open the database read-only and build the report."""
    database = Database(database_path)
    with database.connect(readonly=True) as conn:
        return build_provenance_report(conn, limit=limit, job_ids=job_ids, now=now)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only Minisa Dream Provenance Report"
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--job-id", action="append", dest="job_ids",
                        help="report one job id; repeat for multiple jobs")
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= 1000:
        parser.error("--limit must be between 1 and 1000")
    report = load_provenance_report(
        args.database, limit=args.limit, job_ids=args.job_ids
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
