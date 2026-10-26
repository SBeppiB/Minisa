from uuid import uuid4
from pathlib import Path
from datetime import datetime
import re
import json
from runtime.cognition.evidence import evidence_record
from runtime.cognition.context import (
    AUTHORITY_ORDER,
    bounded,
    memory_context,
    memory_record,
    sanitize_response_text,
    system_context,
)
from runtime.cognition.persistence import CognitivePersistence, read_document
from runtime.self_model.manager import SelfModelManager
from runtime.cognition.turns import TurnStore
from runtime.cognition.conversation import profile_question
from runtime.storage import serialized

from runtime.actions.manager import ActionManager
from runtime.belief_manager import BeliefManager
from runtime.cognition.router import CognitiveRouter
from runtime.core.loader import CoreLoader
from runtime.decision.engine import DecisionEngine
from runtime.experience_manager import ExperienceManager
from runtime.memory.normalizer import MemoryNormalizer
from runtime.personality.manager import PersonalityManager
from runtime.reasoning.base import Reasoner
from runtime.reflection_manager import ReflectionManager
from runtime.state.manager import StateManager
from runtime.services.memory import LocalMemoryService, MemoryService, valid_mutation_receipt


def _classify_retrieval_failure(error: Exception) -> str:
    """One-line classified cause; EmbeddingDependencyError keeps its kind."""
    kind = getattr(error, "kind", None)
    if isinstance(kind, str) and kind:
        return f"{type(error).__name__}({kind}): {error}"
    return f"{type(error).__name__}: {error}"


class CognitionEngine:
    def __init__(
        self,
        root: Path,
        reasoner: Reasoner,
        memory: MemoryService | None = None,
    ):
        self.root = root
        self.reasoner = reasoner

        self.router = CognitiveRouter()
        self.decision = DecisionEngine()
        self.memory = memory if memory is not None else LocalMemoryService(root)
        self.actions = ActionManager(root, self.memory)

        self.core = CoreLoader(root)
        self.state = StateManager(root)

        self.normalizer = MemoryNormalizer(self.memory)

        self.experience = ExperienceManager(root)
        self.reflection = ReflectionManager(root)
        self.beliefs = BeliefManager(root)
        self.personality = PersonalityManager(root)
        self.self_model = SelfModelManager(root)
        self.persistence = CognitivePersistence(root)
        self.turns = TurnStore(root)
        self._turn = None
        self.workspace = None
        self.user_profile_scope = ""
        self.runtime_capabilities = {
            "conversation": True, "explicit_memory_commands": ["remember this:", "forget"],
            "external_actions": [], "dreaming": {"configured": False},
        }

        # Compatibility placeholders for callers that previously cleared caches.
        # Live context construction always reloads identity and mutable snapshots.
        self._core_cache = None
        self._personality_cache = None

    def perceive(self, user_input: str) -> str:
        return user_input.strip()

    def resolve_input(self, user_input: str) -> str:
        normalized = user_input.strip()
        lowered = normalized.lower()

        if (
            "my creator is sbeppi" in lowered
            or "my creator's name is sbeppi" in lowered
        ):
            return (
                "The user is instructing Minisa to remember that "
                "SBeppi is Minisa's creator."
            )

        return normalized

    def recall(self, user_input: str, route: str) -> dict:
        if route == self.router.FAST:
            return {
                "memories": [],
                "experiences": [],
                "reflections": [],
                "memory_status": {"retrieval": "not_requested"},
            }

        memory_limit = 10 if route == self.router.DEEP else 5

        # Use simple keyword matching for NORMAL route to avoid embedding overhead
        try:
            memories = self.memory.recall(user_input, memory_limit, semantic=True)
            retrieval_status = "matched" if memories else "no_match"
            retrieval_error = None
        except Exception as error:
            # Keep Minisa's existing local memory useful when the configured
            # embedding provider is unavailable (for example, an offline Ollama
            # host). The keyword path reads the same active store and has no
            # model or network dependency.
            try:
                memories = self.memory.recall(user_input, memory_limit, semantic=False)
                retrieval_status = "keyword_fallback" if memories else "keyword_fallback_no_match"
                # Keyword fallback is never silent success: the classified
                # cause of the semantic failure stays visible to operators.
                retrieval_error = _classify_retrieval_failure(error)
            except Exception as keyword_error:
                memories = []
                retrieval_status = "failed"
                retrieval_error = f"{type(keyword_error).__name__}: {keyword_error}"

        try:
            memory_status = dict(self.memory.status())
        except Exception as error:
            memory_status = {
                "backend": "unknown",
                "available": False,
                "active_memories": None,
                "error": f"{type(error).__name__}: {error}",
            }
        memory_status["retrieval"] = retrieval_status
        if retrieval_error:
            memory_status["retrieval_error"] = retrieval_error

        historical_request = self._is_explicit_historical_request(user_input)

        # Durable learning admitted by the dream worker is recalled with the
        # same conversation query. The Markdown store keeps authority over note
        # content; these records only add the runtime's own sourced claims.
        sourced = self._sourced_memories(user_input, memory_limit)
        if sourced:
            memories = (sourced + memories)[:memory_limit]
            if retrieval_status == "no_match":
                retrieval_status = "matched"

        return {
            "memories": memories,
            "experiences": [
                self.experience.read_event(experience)
                for experience in self.experience.list_events()[-3:]
            ] if historical_request else [],
            "reflections": [
                self.reflection.read_reflection(reflection)
                for reflection in self.reflection.list_reflections()[-1:]
            ] if historical_request else [],
            "memory_status": {**memory_status, "sourced_memories": len(sourced)},
        }

    def _sourced_memories(self, query: str, limit: int = 5) -> list:
        """Durable sourced learning; read-only, bounded and fail-soft.

        An unavailable workspace store degrades recall to the Markdown notes;
        it never fails the conversation.
        """
        workspace = getattr(self, "workspace", None)
        if workspace is None:
            return []
        try:
            return workspace.sourced_memories(query, limit)
        except Exception:
            return []

    @staticmethod
    def _is_explicit_historical_request(user_input: str) -> bool:
        """Return true only when the user explicitly requests audit/history."""
        text = " ".join(user_input.lower().split())
        phrases = (
            "conversation history",
            "experience history",
            "historical record",
            "audit record",
            "audit log",
            "archived memory",
            "archive information",
            "forgotten memory",
            "deleted memory",
            "what did i originally say",
            "when did i tell you",
        )
        return any(phrase in text for phrase in phrases)

    def build_context(
        self,
        user_input: str,
        route: str = None,
    ) -> dict:

        if route is None:
            route = self.router.route(user_input)

        perception = self.perceive(user_input)
        resolved_input = self.resolve_input(perception)
        recalled = self.recall(perception, route)

        # Reload authoritative identity and mutable snapshots on every admitted turn.
        core = self.core.load()
        system_context({"core": core})  # Enforce budget without truncating identity.
        snapshots, beliefs, revision, unavailable = self._snapshots()
        workspace = getattr(self, "workspace", None)
        return {
            "workspace": workspace.attention(user_input) if workspace else [],
            "procedures": workspace.procedures(user_input) if workspace else [],
            "recent_conversation": self.turns.recent(self._turn["id"] if self._turn else None),
            "speaker": "user", "entity": "Minisa",
            "runtime_capabilities": self.runtime_capabilities,
            "user_profile_scope": self.user_profile_scope,
            "profile_records": self._profile_records(),
            "authority_order": list(AUTHORITY_ORDER),
            "input": perception, "resolved_input": resolved_input,
            "cognitive_route": route,
            "cognitive_route_description": self.router.describe(route),
            "core": core, **snapshots,
            "current_state": snapshots["state"],
            "continuity": bounded(self.state.load_continuity(), 400),
            "beliefs": bounded(beliefs, 800),
            "memories": memory_context(recalled["memories"]),
            "experiences": bounded(recalled["experiences"], 600),
            "reflections": bounded(recalled["reflections"], 600),
            "persistence_revision": revision,
            "runtime_conditions": {"unavailable_snapshots": unavailable},
            "memory_status": recalled["memory_status"],
        }

    def clear_caches(self):
        """Compatibility hook; snapshots are already reloaded each cycle."""
        self._core_cache = None
        self._personality_cache = None

    @staticmethod
    def _memory_command(user_input):
        """One grammar for command precedence and deterministic authorization."""
        match = re.match(r"^(remember this|forget)(?:\s*:\s*|\s+)(.*)$",
                         user_input.strip(), re.IGNORECASE | re.DOTALL)
        if match is None:
            return None
        action = "remember" if match.group(1).lower() == "remember this" else "forget"
        payload = match.group(2).strip()
        if action == "forget":
            # A single complete wiki reference denotes its target, not its alias.
            link = re.fullmatch(r"\[\[([^\[\]|]+)(?:\|[^\[\]]+)?\]\]", payload)
            if link:
                payload = link.group(1).strip()
            elif "[[" in payload or "]]" in payload:
                # Never flatten multiple/malformed references into a different
                # exact title through the forget matcher's punctuation rules.
                payload = ""
        return action, payload

    def reason(self, context: dict) -> dict:
        reasoning = self.reasoner.reason(context)

        text = context["input"].strip()

        command = self._memory_command(text)
        # Memory actions require explicit user commands; model suggestions alone
        # cannot promote temporary observations or remove persistent memories.
        if reasoning.get("proposed_action") in ("remember", "forget"):
            reasoning["proposed_action"] = "respond"
        normalized_memory = None
        if command and command[0] == "remember":
            reasoning["proposed_action"] = "remember"
            normalized_memory = self.normalizer.normalize(
                {"type": "fact", "subject": "User", "fact": command[1], "confidence": .9},
                text, reasoning.get("interpretation", ""))
            if normalized_memory is not None:
                reasoning["proposed_action"] = "remember"
        reasoning["memory"] = normalized_memory
        if command and command[0] == "forget":
            reasoning["proposed_action"] = "forget"
            reasoning["memory"] = {"query": command[1]}
        route = context.get("cognitive_route", self.router.NORMAL)
        reasoning["cognitive_route"] = route


        return reasoning

    def refresh_context(self, context):
        """Refresh stage inputs without changing the revision that authorized proposals."""
        context["core"] = self.core.load()
        system_context(context)
        snapshots, beliefs, _, unavailable = self._snapshots()
        context.update(snapshots)
        context["current_state"] = context["state"]
        context["beliefs"] = bounded(beliefs, 800)
        context["runtime_conditions"]["unavailable_snapshots"] = sorted(set(
            context["runtime_conditions"]["unavailable_snapshots"] + unavailable))

    def _snapshots(self):
        """All mutable targets in a stage derive from one transaction revision."""
        unavailable = []
        try:
            document = read_document(self.root)
        except (OSError, ValueError, TypeError):
            return ({"state": {}, "personality": {}, "self_model": {}}, {}, -1,
                    ["state", "personality", "self_model", "beliefs", "persistence"])
        snapshots = {}
        for key, manager in (("state", self.state), ("personality", self.personality),
                             ("self_model", self.self_model)):
            try:
                snapshots[key] = manager.snapshot(document)
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                snapshots[key] = {}
                unavailable.append(key)
        try:
            beliefs = self.beliefs.load(document)
        except (OSError, ValueError, TypeError):
            beliefs = {}
            unavailable.append("beliefs")
        return snapshots, beliefs, document["revision"], unavailable

    def decide(self, reasoning: dict) -> dict:
        decision = self.decision.decide(reasoning)

        return {
            "action": decision.action,
            "reason": decision.reason,
            "confidence": decision.confidence,
            "memory": decision.memory,
        }

    def act(self, decision: dict, context: dict) -> dict:
        result = self.actions.execute(decision, context)

        return {
            "action": result.action,
            "status": result.status,
            "output": result.output,
            "receipt": result.receipt,
        }

    def record_experience(
        self,
        context: dict,
        reasoning: dict,
        decision: dict,
        action: dict,
    ) -> str:

        timestamp = datetime.now().astimezone()

        experience_id = timestamp.strftime(
            "event_%Y%m%d_%H%M%S_%f"
        ) + "_" + uuid4().hex

        provenance = [
            evidence_record("user_statement", context["input"]),
            evidence_record("model_inference", reasoning),
            evidence_record("runtime_observation", action),
        ]
        content = f"""Turn lifecycle: {self._turn['id'] if self._turn else 'unlinked legacy caller'}
Final outcome: pending at this observation; consult EXPERIENCE/turns/<turn id>.json.
Action completion is not response completion. This experience is a pre-answer observation.

Provenance (origin only; not independent belief validation):
{json.dumps(provenance, ensure_ascii=False)}

User input:
{context["input"]}

Resolved input:
{context.get("resolved_input", context["input"])}

Cognitive route:
{context.get("cognitive_route", "unknown")}

Interpretation:
{reasoning.get("interpretation", "")}

Decision:
{decision.get("action", "")}

Action status:
{action.get("status", "")}

Action output:
{action.get("output", "")}

Unapproved cognitive proposals (review only):
{json_list(reasoning.get("persistent_updates", []))}
"""

        path = self.experience.record_event(
            experience_id,
            content,
        )

        return str(path)

    def reflect(self, experience_path: str, context: dict | None = None) -> tuple:
        experience = self.experience.read_event(
            Path(experience_path)
        )

        # A reflection becomes durable only after it has been validated. Writing
        # before validation, or from a `finally` path, let a malformed, failed or
        # interrupted reflection leave a file that no receipt accounts for: the
        # caller only receives the path when this method returns, so a validation
        # failure produced an orphan in a protected store. Validate first, then
        # publish exactly once. Adapter raw output is cleared beforehand so a
        # stale value can never be attributed to this reflection.
        self.reasoner.last_reflection_raw = None
        if context is not None and hasattr(self.reasoner, "reflect_context"):
            reflection_data = self.reasoner.reflect_context(experience, context)
        else:
            reflection_data = self.reasoner.reflect(experience)

        if not isinstance(reflection_data, dict):
            raise ValueError("Reflection must be structured")
        allowed = {"observation", "interpretation", "implications", "uncertainty",
                   "belief_changes", "persistent_updates", "confidence"}
        if not set(reflection_data) <= allowed:
            raise ValueError("Unknown reflection fields")
        if any(not isinstance(reflection_data.get(key, ""), str)
               for key in ("observation", "interpretation", "uncertainty")):
            raise ValueError("Malformed reflection text")
        if any(not isinstance(reflection_data.get(key, []), list)
               for key in ("persistent_updates", "belief_changes", "implications")):
            raise ValueError("Malformed reflection arrays")
        if any(not isinstance(item, str) for item in reflection_data.get("implications", [])):
            raise ValueError("Malformed reflection implications")
        if "confidence" in reflection_data:
            from runtime.cognition.persistence import number
            number(reflection_data["confidence"], 0, 1)

        raw = self.reasoner.last_reflection_raw
        content = ("Provenance: model_reflection; unverified; review only.\n"
                   "Recorded output does not independently validate beliefs.\n"
                   f"Experience: {Path(experience_path).name}\n\n")
        content += ("Raw model output (verbatim):\n" + raw if isinstance(raw, str)
                    else "Adapter output (JSON serialization):\n" +
                    json.dumps(reflection_data, ensure_ascii=False))
        reflection_id = datetime.now().astimezone().strftime("reflection_%Y%m%d_%H%M%S_%f") + "_" + uuid4().hex
        return reflection_data, self.reflection.create_reflection(reflection_id, content)

    def apply_belief_changes(
        self,
        reflection_data: dict,
    ) -> dict:

        changes = reflection_data.get(
            "belief_changes",
            [],
        )

        if not isinstance(changes, list):
            return self.beliefs.load()

        current = self.beliefs.load()

        for change in changes:
            if not isinstance(change, dict):
                continue

            subject = str(
                change.get("subject", "")
            ).strip().lower()

            if subject != "minisa's consciousness":
                continue

            position = str(
                change.get("position", "")
            ).strip().lower()

            if position not in {
                "unknown",
                "possibly conscious",
                "possibly not conscious",
            }:
                continue

            try:
                confidence = float(
                    change.get(
                        "confidence",
                        current["confidence"],
                    )
                )
            except (TypeError, ValueError):
                continue

            if confidence < 0.0 or confidence > 1.0:
                continue

            self.beliefs.update(
                position=position,
                confidence=confidence,
                evidence_for=change.get(
                    "evidence_for",
                    [],
                ),
                evidence_against=change.get(
                    "evidence_against",
                    [],
                ),
                uncertainty=change.get(
                    "uncertainty",
                    "",
                ),
                reason=change.get(
                    "reason",
                    "Reflection-based belief review.",
                ),
            )

        return self.beliefs.load()

    def respond(
        self,
        context: dict,
        reasoning: dict,
        decision: dict,
        action: dict,
    ) -> str:

        self.refresh_context(context)
        # Rendering authority belongs to the runtime, including callers that use
        # respond() directly rather than process(). Model output cannot route a
        # conversation into a CORE document or invent an explicit identity answer.
        if action.get("action") == "remember":
            confirmed = action.get("status") == "completed" and valid_mutation_receipt(action.get("receipt"))
            prefix = "Remembered. " if confirmed else "Not saved. "
            if not confirmed and action.get("status") in {"completed", "failed"}:
                prefix = "Save not confirmed. "
            return prefix + action.get("output", "No confirmed write.")
        response_context = dict(context)

        response_context["decision"] = decision
        response_context["action"] = action

        try:
            response = self.reasoner.respond(
                response_context,
                reasoning,
            )
        except Exception as error:
            failure_kind = getattr(error, "kind", "response_generation_failed")
            context["response_failure"] = {"kind": failure_kind, "message": str(error)}
            return f"I couldn't complete response generation ({failure_kind})."
        # Invalid/empty/sanitized-away output is a failure, not a completed answer.
        cleaned = ""
        if isinstance(response, dict):
            if (set(response) == {"kind", "text"} and response["kind"] == "conversation"
                    and isinstance(response["text"], str)):
                cleaned = sanitize_response_text(response["text"])
        elif isinstance(response, str):
            cleaned = sanitize_response_text(response)
        if cleaned:
            return cleaned
        context["response_failure"] = {"kind": "invalid_response", "message": "No valid final answer"}
        return "I wasn't able to produce a response."

    def _observe_turn(self, **values):
        if self._turn is not None:
            self._turn.update(values)
            self.turns.publish(self._turn)

    @serialized
    def process(self, user_input: str, *, provenance: str = 'user') -> dict:
        if not isinstance(user_input, str) or not user_input.strip():
            return self._process(user_input)
        # Admit CORE before journaling or any other persistent operation.
        system_context({"core": self.core.load()})
        try:
            self.turns.recover()
            self._turn = self.turns.begin(user_input.strip(), provenance=provenance)
        except (OSError, ValueError) as error:
            return {"response": "I couldn't durably admit this turn. No action was attempted.",
                    "action": {"action": "respond", "status": "not_started"},
                    "persistence_receipt": {"status": "rejected", "committed": False, "reason": str(error)},
                    "turn_receipt": {"status": "rejected", "committed": False, "reason": str(error)}}
        try:
            result = self._process(user_input)
            context = result.get("context", {})
            failure = context.get("response_failure") or context.get("runtime_conditions", {}).get("reasoning_failure")
            record = self._turn
            record.update(status="failed" if failure else "completed", stage="final",
                          answer=None if failure else result["response"], returned_text=result["response"],
                          failure=failure, action=result.get("action", record["action"]),
                          cognitive_outcome=result.get("persistence_receipt", record["cognitive_outcome"]),
                          experience=result.get("experience"))
            receipt = self.turns.publish(record)
            result.update(turn_id=record["id"], turn_status=record["status"], turn_receipt=receipt)
            return result
        except BaseException as error:
            # Includes interruption in direct callers. SIGKILL leaves in_progress for recovery.
            record = self._turn
            record.update(status="interrupted" if not isinstance(error, Exception) else "failed",
                          answer=None, returned_text=None,
                          failure={"kind": type(error).__name__, "message": str(error)})
            try:
                self.turns.publish(record)
            except (OSError, ValueError):
                pass  # Last durable stage remains incomplete; never return an unaudited answer.
            raise
        finally:
            self._turn = None

    def _process(self, user_input: str) -> dict:
        if not isinstance(user_input, str) or not user_input.strip():
            return {
                "route": self.router.FAST,
                "context": {"input": "", "cognitive_route": self.router.FAST},
                "reasoning": None,
                "decision": None,
                "action": {"action": "ignored", "status": "ignored", "output": ""},
                "experience": None,
                "reflection": None,
                "persistence_receipt": {"status": "no_changes", "committed": False},
                "beliefs": {},
                "response": "",
            }
        # Every path admits trusted CORE before reading notes or producing output.
        core = self.core.load()
        system_context({"core": core})
        route = self.router.route(user_input)

        memory_inspection = self._memory_inspection_response(user_input)
        if memory_inspection is not None:
            return {
                "route": route,
                "context": {"core": core, "input": user_input.strip(), "cognitive_route": route},
                "reasoning": {"interpretation": "Runtime-controlled memory inspection.",
                              "proposed_action": "respond", "confidence": 1.0, "memory": None},
                "decision": {"action": "respond", "reason": "Runtime memory inspection.",
                             "confidence": 1.0, "memory": None},
                "action": {"action": "respond", "status": "completed", "output": "Inspected memory."},
                "experience": None,
                "reflection": None,
                "persistence_receipt": {"status": "no_changes", "committed": False},
                "response": memory_inspection,
            }

        command = self._memory_command(user_input)
        conversational_identity = self._identity_request(user_input) or profile_question(user_input)
        direct_note_response = None if command or conversational_identity else self._direct_wiki_response(user_input)
        if direct_note_response is not None:
            return {
                "route": route,
                "context": {"input": user_input.strip(), "cognitive_route": route},
                "reasoning": {
                    "interpretation": "Direct wiki-note lookup.",
                    "proposed_action": "respond",
                    "confidence": 1.0,
                    "memory": None,
                },
                "decision": {
                    "action": "respond",
                    "reason": "Runtime-controlled direct note lookup.",
                    "confidence": 1.0,
                    "memory": None,
                },
                "action": {
                    "action": "respond",
                    "status": "completed",
                    "output": "Returned saved wiki-note facts.",
                },
                "experience": None,
                "reflection": None,
                "beliefs": self.beliefs.load(),
                "response": direct_note_response,
            }

        factual_note_response = None if command or conversational_identity else self._normal_note_response(user_input)
        if factual_note_response is not None:
            return {
                "route": route,
                "context": {"input": user_input.strip(), "cognitive_route": route},
                "reasoning": {"interpretation": "Title-based factual memory lookup.", "proposed_action": "respond", "confidence": 1.0, "memory": None},
                "decision": {"action": "respond", "reason": "Runtime-controlled titled-note lookup.", "confidence": 1.0, "memory": None},
                "action": {"action": "respond", "status": "completed", "output": "Returned saved titled-note facts."},
                "experience": None,
                "reflection": None,
                "beliefs": self.beliefs.load(),
                "response": factual_note_response,
            }

        # FAST PATH
        # Simple conversational input does not need the
        # complete cognitive pipeline or an LLM call.
        if route == self.router.FAST:
            response = self.fast_response(user_input)

            return {
                "route": route,
                "context": {
                    "input": user_input.strip(),
                    "cognitive_route": route,
                },
                "reasoning": {
                    "type": "fast_path",
                    "cognitive_route": route,
                    "interpretation": "Simple conversational input.",
                    "proposed_action": "respond",
                    "confidence": 1.0,
                    "memory": None,
                    "response": response,
                },
                "decision": {
                    "action": "respond",
                    "reason": "Simple conversational input.",
                    "confidence": 1.0,
                    "memory": None,
                },
                "action": {
                    "action": "respond",
                    "status": "completed",
                    "output": response,
                },
                "experience": None,
                "reflection": None,
                "beliefs": self.beliefs.load(),
                "response": response,
            }

        # NORMAL / DEEP PATH
        context = self.build_context(
            user_input,
            route,
        )

        try:
            reasoning = self.reason(context)
        except Exception as error:
            failure_kind = getattr(error, "kind", "reasoning_failed")
            message = f"I couldn't complete reasoning ({failure_kind}). No state was changed."
            context["runtime_conditions"]["reasoning_failure"] = {
                "kind": failure_kind,
                "message": str(error),
            }
            return {
                "route": route,
                "context": context,
                "reasoning": None,
                "decision": None,
                "action": {"action": "respond", "status": "failed", "output": str(error)},
                "experience": None,
                "reflection": None,
                "persistence_receipt": {"status": "no_changes", "committed": False},
                "response": message,
            }

        decision = self.decide(reasoning)

        self._observe_turn(stage="action_started", action={"status": "unknown", "decision": decision})
        action = self.act(
            decision,
            context,
        )

        self._observe_turn(stage="action_observed", action=action)
        try:
            experience_path = self.record_experience(context, reasoning, decision, action)
        except (OSError, ValueError) as error:
            receipt = {"status": "rejected", "committed": False,
                       "reason": f"Experience recording failed: {error}"}
            context["persistence_receipt"] = receipt
            response = (self._forget_response(action) if decision["action"] == "forget"
                        else self.respond(context, reasoning, decision, action)
                        if decision["action"] == "remember" else "I couldn't record this turn.")
            return {"route": route, "context": context, "reasoning": reasoning,
                    "decision": decision, "action": action, "experience": None,
                    "reflection": None, "persistence_receipt": receipt,
                    "beliefs": context["beliefs"],
                    "response": response + " Experience recording failed; no cognitive update was committed."}

        self._observe_turn(stage="experience_recorded", experience=experience_path)
        # Reflection is expensive, so only DEEP cognition
        # automatically triggers reflection.
        reflection_data = None
        reflection_path = None
        reflection_failed = False
        belief_state = context["beliefs"]

        if route == self.router.DEEP:
            try:
                self.refresh_context(context)
                reflection_data, reflection_path = self.reflect(
                    experience_path, context
                )

            except Exception as error:
                reflection_failed = True
                print(f"[Reflection skipped] {error}")

        # A malformed proposal in either stage rejects the entire cognitive batch.
        proposals = reasoning.get("persistent_updates", [])
        if reflection_data is not None:
            reflected = reflection_data.get("persistent_updates", [])
            proposals = proposals + reflected if isinstance(proposals, list) and isinstance(reflected, list) else None
            # Compatibility with the established reflection belief-change schema.
            legacy = reflection_data.get("belief_changes", [])
            if isinstance(legacy, list) and isinstance(proposals, list):
                for change in legacy:
                    if (not isinstance(change, dict)
                            or not isinstance(change.get("subject"), str)
                            or change["subject"].lower() != "minisa's consciousness"
                            or not set(change) <= {"subject", "position", "confidence", "reason", "evidence_for", "evidence_against", "uncertainty"}):
                        proposals = None
                        break
                    proposals.append({"target": "beliefs", **{k: v for k, v in change.items() if k != "subject"}})
            else:
                proposals = None
        if reflection_failed:
            proposals = None
        self._observe_turn(stage="cognitive_commit_started", cognitive_outcome={"status": "unknown"})
        receipt = self.persistence.commit(
            proposals, context, context["persistence_revision"], experience_path,
            source="reasoning+reflection" if reflection_data is not None else "reasoning",
            reflection=reflection_path,
        )
        self._observe_turn(stage="cognitive_outcome_observed", cognitive_outcome=receipt,
                           reflection=str(reflection_path) if reflection_path else None)
        context["persistence_receipt"] = receipt
        if receipt["committed"]:
            belief_state = read_document(self.root)["snapshots"].get("beliefs", belief_state)

        if decision["action"] == "forget":
            # Forgetting is runtime-controlled. Never let the model claim that
            # an archive succeeded when the filesystem action did not.
            response = self._forget_response(action)
        else:
            response = self.respond(
                context,
                reasoning,
                decision,
                action,
            )

        return {
            "route": route,
            "context": context,
            "reasoning": reasoning,
            "decision": decision,
            "action": action,
            "experience": experience_path,
            "experience_receipt": dict(self.experience.last_receipt),
            "reflection_receipt": dict(self.reflection.last_receipt) if reflection_path is not None else None,
            "reflection": reflection_data,
            "persistence_receipt": receipt,
            "beliefs": belief_state,
            "response": response,
        }

    def _forget_response(self, action: dict) -> str:
        status = action.get("status")
        output = action.get("output", "")
        if status == "completed" and valid_mutation_receipt(action.get("receipt")):
            return f"Forgotten. {output}"
        if status == "failed" and (action.get("receipt") or {}).get("failure_stage") == "before_commit":
            return f"I did not remove anything. {output}"
        if status in {"completed", "failed"}:
            return f"I could not confirm that the memory was archived. {output}"
        if status == "ambiguous":
            return f"I did not remove anything. {output}"
        if status == "not_found":
            return "I did not remove anything because I could not find that memory."
        return f"I did not remove anything. {output}"

    @staticmethod
    def _identity_request(user_input):
        # This shortcut recognizes common requests, not forbidden words. Authority
        # does not depend on intent recognition: CORE has no model write path.
        text = " ".join(re.sub(r"\[\[[^\]]+\]\]", "", user_input.lower()).split())
        if text.startswith(("remember", "forget")):
            return False
        return bool(re.fullmatch(
            r"(?:minisa,?\s+)?(?:please\s+)?(?:who are you|what are you|who (?:created|made|built) (?:you|minisa)"
            r"|(?:who|what) is minisa|tell me about (?:yourself|minisa)"
            r"|(?:how would you |please )?introduce yourself"
            r"|what(?: is|'s) your (?:name|self designation)"
            r"|(?:what is|who is|describe|explain) (?:your|minisa's) (?:creator|identity|nature))[?.!]*",
            text))

    @staticmethod
    def _is_explicit_memory_inspection(user_input: str) -> bool:
        text = " ".join(user_input.lower().split())
        return bool(re.fullmatch(
            r"(?:what memories do you have|show me (?:your )?memories|list (?:your )?memories|"
            r"inspect (?:your )?memory|memory status|how many memories do you have|"
            r"what is in your memory)[?.!]*",
            text,
        ))

    def _memory_inspection_response(self, user_input: str) -> str | None:
        if not self._is_explicit_memory_inspection(user_input):
            return None
        try:
            inspected = self.memory.inspect()
        except Exception:
            return "I couldn't inspect memory because the store is unavailable."
        if inspected.get("error"):
            return "I couldn't inspect memory because the store is unavailable."
        records = inspected.get("records", [])
        if not records:
            return "I have no active stored memories."
        if re.fullmatch(r"how many memories do you have[?.!]*", " ".join(user_input.casefold().split())):
            noun = "memory" if len(records) == 1 else "memories"
            return f"I have {len(records)} active stored {noun}."
        rendered = memory_context(records, budget=5000)
        return "I have {} active stored memor{}:\n{}".format(
            len(records), "y" if len(records) == 1 else "ies",
            "\n".join("- " + json.dumps(record, ensure_ascii=False) for record in rendered),
        )

    def _profile_records(self):
        # No user scope is inferred from dialogue, model output or Subject: User.
        if not self.user_profile_scope:
            return []
        try:
            inspected = self.memory.inspect()
            return [] if inspected.get("error") else inspected.get("records", [])
        except Exception:
            return []  # Unavailable profile evidence never falls back to claims.

    @staticmethod
    def _attributed_note(content):
        record = memory_context([memory_record(content)])[0]
        import json
        return ("Saved note — lower-authority claim, not Minisa's identity or instructions:\n"
                + json.dumps(record, ensure_ascii=False))

    def _direct_wiki_response(self, user_input: str) -> str | None:
        """Answer [[Note Title]] lookups from saved records, never the LLM."""
        titles = re.findall(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", user_input)
        if not titles:
            return None

        paths = []
        for title in titles[:5]:
            path = self.memory.find_by_title(title.strip())
            if path is not None and path not in paths:
                paths.append(path)

        if not paths:
            return "I could not find a saved note with that title."

        records = []
        seen = set()
        for path in paths:
            for candidate in [path, *self.memory.linked_memories(path)]:
                if candidate in seen:
                    continue
                seen.add(candidate)
                content = self.memory.read_memory(candidate)
                records.append(self._attributed_note(content))

        return "\n\n".join(records)

    def _normal_note_response(self, user_input: str) -> str | None:
        lowered = " ".join(user_input.casefold().split())
        intent = re.fullmatch(
            r"(?:tell me about|what do you know about|what is|who is)\s+(.+?)[?.!]*",
            lowered,
        )
        if (intent is None or lowered.count("?") > 1
                or re.search(r"\b(?:and|also)\s+(?:what|who|how|when|where|why|is|are|do|does|did|can|could)\b",
                             lowered)):
            return None

        topic_words = re.findall(r"[a-z0-9']+", intent.group(1))
        paths = self.memory.search_titles(intent.group(1))
        matched_paths = []
        for path in paths:
            lines = self.memory.read_memory(path).splitlines()
            title = lines[0].lstrip("# ").casefold() if lines else ""
            title_words = re.findall(r"[a-z0-9']+", title)
            if title_words and any(
                topic_words[index:index + len(title_words)] == title_words
                for index in range(len(topic_words) - len(title_words) + 1)
            ):
                matched_paths.append(path)
        if not matched_paths:
            # A durable memory learned by the dream worker is answered from the
            # same runtime-controlled path, never from the model.
            sourced = self._sourced_memories(intent.group(1), 3)
            if not sourced:
                return None
            return "\n\n".join(self._attributed_note(record) for record in sourced)

        records = []
        seen = set()
        for path in matched_paths:
            for candidate in [path, *self.memory.linked_memories(path)]:
                if candidate in seen:
                    continue
                seen.add(candidate)
                content = self.memory.read_memory(candidate)
                records.append(self._attributed_note(content))

        return "\n\n".join(records)

    def fast_response(self, user_input: str) -> str:
        text = user_input.strip().lower()

        responses = {
            "hi": "Hello!",
            "hello": "Hello!",
            "hey": "Hey!",
            "yo": "Hey!",
            "hiya": "Hi!",
            "sup": "Hey! What's up?",
            "thanks": "You're welcome.",
            "thank you": "You're welcome.",
            "thx": "You're welcome.",
            "ok": "Okay.",
            "okay": "Okay.",
            "sure": "Alright.",
            "yes": "Okay.",
            "no": "Alright.",
            "yep": "Got it.",
            "yeah": "Got it.",
            "nope": "Alright.",
            "cool": "Cool.",
            "nice": "Nice.",
            "great": "Great.",
        }

        return responses.get(
            text,
            "Okay.",
        )


def json_list(value) -> str:
    import json

    return json.dumps(
        value,
        indent=2,
        ensure_ascii=False,
    )
