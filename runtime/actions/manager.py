from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import math
import re
from runtime.storage import writer_lock

from runtime.services.memory import LocalMemoryService, MemoryService, valid_mutation_receipt


@dataclass
class ActionResult:
    action: str
    status: str
    output: str
    receipt: dict | None = None


class ActionManager:
    def __init__(self, root: Path, memory: MemoryService | None = None):
        self.root = root
        self.memory = memory if memory is not None else LocalMemoryService(root)

    def execute(
        self,
        decision: dict,
        context: dict | None = None,
    ) -> ActionResult:
        action = decision.get("action", "respond")

        if action == "respond":
            return ActionResult(
                action="respond",
                status="completed",
                output="Ready to respond to the user.",
            )

        if action in {"remember", "forget"}:
            try:
                with writer_lock(self.root):
                    return self._remember(decision, context) if action == "remember" else self._forget(decision)
            except (OSError, ValueError, TypeError) as error:
                return ActionResult(action, "failed", f"Could not {action}: {error}",
                                    {"status": "failed", "committed": False})

        return ActionResult(
            action=action,
            status="rejected",
            output=f"Action '{action}' is not available.",
        )

    def _remember(
        self,
        decision: dict,
        context: dict | None,
    ) -> ActionResult:
        memory = decision.get("memory")

        if context is None or not isinstance(memory, dict):
            return ActionResult(
                action="remember",
                status="rejected",
                output="No valid memory proposal was provided.",
                receipt={"status": "rejected", "committed": False,
                         "durable": False, "path": "", "warnings": [],
                         "reason": "No valid memory proposal was provided."},
            )

        memory_type = memory.get("type")
        subject = memory.get("subject")
        title = memory.get("title")
        fact = memory.get("fact")
        confidence = memory.get("confidence")

        if (
            not isinstance(memory_type, str)
            or not isinstance(subject, str)
            or not isinstance(title, str)
            or not isinstance(fact, str)
            or type(confidence) not in (int, float)
            or not math.isfinite(confidence)
            or memory_type not in {"general", "fact", "preference", "identity", "relationship", "belief"}
            or any("\n" in v or "\r" in v for v in (subject, title))
            or "\r" in fact or re.search(r"^## ", fact, re.MULTILINE)
            or len(subject) > 200 or len(title) > 100 or len(fact) > 4000
            or not isinstance(context.get("input"), str)
            or not memory_type.strip()
            or not subject.strip()
            or not title.strip()
            or not fact.strip()
            or not 0.0 <= confidence <= 1.0
        ):
            return ActionResult(
                action="remember",
                status="rejected",
                output="The memory proposal was invalid.",
                receipt={"status": "rejected", "committed": False,
                         "durable": False, "path": "", "warnings": [],
                         "reason": "The memory proposal was invalid."},
            )

        if self.memory.memory_exists(fact):
            return ActionResult(
                action="remember",
                status="skipped",
                output="Memory already exists.",
            )

        memory_id = self.memory.next_memory_id()
        timestamp = datetime.now().astimezone().isoformat()

        content = f"""# {title.strip()}

ID: {memory_id}
Type: {memory_type.strip()}
Subject: {subject.strip()}
Source: User
Status: user_claim
Confidence: {confidence}
Created: {timestamp}

## Fact

{fact.strip()}

## Original Input

{context["input"]}

## Reason

{decision.get("reason", "")}
"""

        receipt = self.memory.create_memory(memory_id, title, content)
        return self._mutation_result("remember", receipt, "Memory created")

    def _forget(self, decision: dict) -> ActionResult:
        memory = decision.get("memory")
        query = memory.get("query", "") if isinstance(memory, dict) else ""
        query = query.strip()

        if not query:
            return ActionResult("forget", "rejected", "Say which memory to forget.")

        candidates = self.memory.find_forget_candidates(query)
        if not candidates:
            return ActionResult("forget", "not_found", "No active memory matched that request.")

        if len(candidates) > 1:
            titles = []
            for path in candidates:
                first_line = self.memory.read_memory(path).splitlines()[0:1]
                titles.append(first_line[0].removeprefix("#").strip() if first_line else str(path))
            return ActionResult(
                "forget",
                "ambiguous",
                "More than one memory matches: " + ", ".join(titles),
            )

        path = candidates[0]
        title_line = self.memory.read_memory(path).splitlines()[0:1]
        title = title_line[0].removeprefix("#").strip() if title_line else str(path)
        try:
            receipt = self.memory.archive_memory(path, query)
        except OSError as error:
            return ActionResult("forget", "failed", f"Could not archive memory: {error}",
                                {"status": "failed", "committed": False, "failure_stage": "before_commit"})

        return self._mutation_result("forget", receipt, f"Archived memory: {title}")

    @staticmethod
    def _mutation_result(action, receipt, description):
        valid = valid_mutation_receipt(receipt)
        if not valid:
            return ActionResult(action, "failed", "Backend did not confirm a successful commit.",
                                receipt if isinstance(receipt, dict) else
                                {"status": "invalid_receipt", "committed": False})
        warnings = list(receipt["warnings"])
        if not receipt["durable"]:
            warnings.append("Committed, but crash durability is not confirmed; do not retry automatically.")
        suffix = " Maintenance warnings: " + "; ".join(warnings) if warnings else ""
        return ActionResult(action, "completed", description + ": " + receipt["path"] + suffix, receipt)
