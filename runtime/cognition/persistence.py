"""Runtime-owned, atomic cognitive updates; legacy stores are read-only baselines.

One JSON replacement commits snapshots AND their audit history. The advisory lock
serializes writers and revision checking rejects proposals based on stale context.
No model-supplied paths, code, identity fields, or storage commands are accepted.
"""
from copy import deepcopy
from runtime.storage import writer_lock, check_target
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import time

from runtime.personality.models import PersonalityState
from runtime.personality.validator import PersonalityValidator

SELF_FIELDS = {"capabilities", "limitations", "uncertainties", "self_understanding"}
STATE_FIELDS = {"current_focus", "active_goals", "unresolved_work", "recent_context", "temporary_priorities"}
MAX_STORE_BYTES = 8 * 1024 * 1024


def read_document(root):
    path = Path(root) / "STATE" / "cognition.json"
    if not path.exists():
        return {"schema_version": 1, "revision": 0, "snapshots": {}, "history": []}
    with path.open("rb") as stream:
        payload = stream.read(MAX_STORE_BYTES + 1)
    if len(payload) > MAX_STORE_BYTES:
        raise ValueError("Cognitive audit store requires operator archival")
    data = json.loads(payload)
    if (not isinstance(data, dict) or data.get("schema_version") != 1
            or type(data.get("revision")) is not int
            or not isinstance(data.get("snapshots"), dict)
            or not isinstance(data.get("history"), list)):
        raise ValueError("Invalid cognition transaction store; refusing to replace it")
    latest = {}
    last_revision = 0
    targets_in_revision = set()
    for entry in data["history"]:
        if (not isinstance(entry, dict) or type(entry.get("revision")) is not int
                or entry["revision"] not in (last_revision, last_revision + 1)
                or entry["revision"] < 1
                or entry.get("target") not in {"personality", "self_model", "state", "beliefs"}
                or not isinstance(entry.get("new"), dict)):
            raise ValueError("Invalid cognitive audit history")
        if entry["revision"] != last_revision:
            targets_in_revision = set()
        if (entry["target"] in targets_in_revision
                or (entry["target"] in latest and entry.get("previous") != latest[entry["target"]])):
            raise ValueError("Cognitive history chain diverges")
        targets_in_revision.add(entry["target"])
        latest[entry["target"]] = entry["new"]
        last_revision = entry["revision"]
    if last_revision != data["revision"] or latest != data["snapshots"]:
        raise ValueError("Cognitive history and snapshots diverge")
    return data


def committed_snapshot(root, target):
    return read_document(root)["snapshots"].get(target)


def text(value, limit=500):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError("Expected bounded nonempty text")
    return value.strip()


def number(value, low, high):
    if type(value) not in (int, float) or not low <= value <= high or not math.isfinite(value):
        raise ValueError("Invalid numeric value")
    return value


def self_snapshot(data, limit=8):
    if not isinstance(data, dict):
        raise ValueError("Invalid self-model")
    result = {}
    for field in SELF_FIELDS:
        values = data.get(field, [])
        if not isinstance(values, list):
            raise ValueError("Invalid self-model field")
        result[field] = [text(item) for item in values[:limit]]
    # Deliberately exclude legacy identity, values, memories, goals and current_state.
    return result


def personality_snapshot(data):
    if not isinstance(data, dict):
        raise ValueError("Invalid personality")
    for section in ("traits", "communication_style"):
        if not isinstance(data.get(section), dict):
            raise ValueError("Invalid personality section")
        for value in data[section].values():
            number(value, 0, 1)
    if type(data.get("version", 1)) is not int or data.get("version", 1) < 1:
        raise ValueError("Invalid personality version")
    number(data.get("confidence", .5), 0, 1)
    state = PersonalityState.from_dict(data)
    valid, error = PersonalityValidator().validate_state(state)
    if not valid:
        raise ValueError(error)
    return state.to_dict()


class CognitivePersistence:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / "STATE" / "cognition.json"

    def commit(self, proposals, baseline, revision, experience, source="reasoning", reflection=None):
        if proposals == []:
            return {"status": "no_changes", "committed": False}
        try:
            if not isinstance(proposals, list) or not 1 <= len(proposals) <= 4:
                raise ValueError("Expected one to four structured proposals")
            if len(json.dumps(proposals, allow_nan=False)) > 12000:
                raise ValueError("Proposal batch too large")
            if type(revision) is not int or revision < 0:
                raise ValueError("Missing or stale cognitive revision")
            # Provenance comes from the runtime, never from a model-supplied path.
            if not Path(experience).is_file() or Path(experience).parent != self.root / "EXPERIENCE" / "events":
                raise ValueError("Missing runtime experience provenance")
            check_target(self.root, self.path)
            check_target(self.root, self.path.parent / ".cognition.lock")
            with writer_lock(self.root):
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with (self.path.parent / ".cognition.lock").open("a") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    document = read_document(self.root)
                    if revision != document["revision"]:
                        raise ValueError("Stale cognitive snapshot")
                    evidence = [self._durable_evidence(experience, "EXPERIENCE/events")]
                    if reflection is not None:
                        evidence.append(self._durable_evidence(reflection, "REFLECTION/self_reflections"))
                    updated = deepcopy(document)
                    now = time.time()
                    seen = set()
                    for proposal in proposals:
                        if not isinstance(proposal, dict):
                            raise ValueError("Proposal must be an object")
                        target = proposal.get("target")
                        if target not in {"personality", "self_model", "state", "beliefs"} or target in seen:
                            raise ValueError("Unsupported or duplicate update target")
                        if target in baseline.get("runtime_conditions", {}).get("unavailable_snapshots", []):
                            raise ValueError("Cannot update an unavailable snapshot")
                        seen.add(target)
                        previous = deepcopy(document["snapshots"].get(target))
                        if previous is None:
                            previous = self._baseline(target, baseline)
                        value = self._apply(target, proposal, previous, document, now)
                        updated["snapshots"][target] = value
                        updated["history"].append({
                            "target": target, "previous": previous, "new": value,
                            "reason": proposal["reason"], "experience": Path(experience).name,
                            "source": source, "proposal": deepcopy(proposal),
                            "evidence": evidence,
                            "timestamp": now, "revision": revision + 1,
                        })
                    updated["revision"] += 1
                    durable = self._replace(updated)
                    return {"status": "committed", "committed": True,
                            "revision": updated["revision"], "targets": sorted(seen), "durable": durable,
                            "experience": Path(experience).name}
        except (ValueError, TypeError, KeyError, AttributeError, OSError) as error:
            return {"status": "rejected", "committed": False, "reason": str(error)}

    def _baseline(self, target, baseline):
        # Prompt truncation must never truncate the authoritative persisted baseline.
        files = {"personality": "PERSONALITY/current.json", "self_model": "SELF_MODEL/current.json",
                 "beliefs": "REFLECTION/consciousness/current.json"}
        path = self.root / files[target] if target in files else None
        if path is not None and path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if target == "personality":
                return personality_snapshot(data)
            if target == "self_model":
                return self_snapshot(data, limit=None)
            return data
        return deepcopy(baseline.get(target))

    def _apply(self, target, proposal, previous, document, now):
        common = {"target", "reason"}
        text(proposal.get("reason"))
        # No runtime-verifiable admission policy exists for these targets yet.
        # Model confidence, reasons, and evidence filenames cannot authorize them.
        if target in {"personality", "self_model"}:
            raise ValueError("Permanent evolution is disabled pending a deterministic evidence policy; proposal requires review")
        if target == "state":
            if set(proposal) != common | {"operation", "values", "ttl_seconds"}:
                raise ValueError("Invalid dynamic-state proposal schema")
            if proposal["operation"] not in {"replace", "clear"}:
                raise ValueError("Invalid state operation")
            values = proposal["values"]
            if not isinstance(values, dict) or not set(values) <= STATE_FIELDS:
                raise ValueError("Invalid dynamic-state fields")
            if proposal["operation"] == "clear":
                number(proposal["ttl_seconds"], 0, 0)
                if values or proposal["ttl_seconds"] != 0:
                    raise ValueError("Clear requires empty values and zero TTL")
                return {"owner": "cognition", "values": {}, "expires_at": now}
            number(proposal["ttl_seconds"], 1, 86400)
            return {"owner": "cognition", "values": {k: text(v) for k, v in values.items()},
                    "expires_at": now + proposal["ttl_seconds"]}
        if (not common | {"position", "confidence"} <= set(proposal)
                or not set(proposal) <= common | {"position", "confidence", "evidence_for", "evidence_against", "uncertainty"}):
            raise ValueError("Invalid belief proposal schema")
        if proposal["position"] not in {"unknown", "possibly conscious", "possibly not conscious"}:
            raise ValueError("Invalid belief position")
        number(proposal["confidence"], 0, 1)
        from runtime.cognition.evidence import require_belief_admission
        require_belief_admission()

    def _replace(self, document):
        # State and history share a single commit point. Never overwrite legacy files.
        payload = json.dumps(document, ensure_ascii=False, allow_nan=False, indent=2)
        if len(payload.encode("utf-8")) > MAX_STORE_BYTES:
            raise ValueError("Cognitive audit store requires operator archival")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             prefix=".cognition-", delete=False) as file:
                temporary = Path(file.name)
                file.write(payload)
                file.flush()
                os.fsync(file.fileno())
            # Open the directory before commit, so open failures cannot follow a write.
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.replace(temporary, self.path)
                try:
                    os.fsync(directory)
                except OSError:
                    # Rename already committed. Report truthfully; never claim rollback.
                    return False
                return True
            finally:
                os.close(directory)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()  # Only this call's own staging file.

    def _durable_evidence(self, reference, directory):
        """Sync evidence before committing; embed exact bytes for independent audit."""
        path = Path(reference)
        if path.is_symlink() or path.resolve().parent != (self.root / directory).resolve():
            raise ValueError("Invalid runtime evidence path")
        with path.open("rb") as stream:
            payload = stream.read(128001)
            if len(payload) > 128000:
                raise ValueError("Runtime evidence exceeds audit budget")
            os.fsync(stream.fileno())
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return {"path": directory + "/" + path.name,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "content": payload.decode("utf-8"),
                "provenance": "model_reflection" if directory.startswith("REFLECTION") else "mixed_runtime_record",
                "verification": "recorded_bytes_only",
                "independently_validates_belief": False}
