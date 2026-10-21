import math
import re


class MemoryNormalizer:
    """
    Converts LLM memory proposals into validated Minisa memory records.

    The LLM may suggest a memory, but the runtime controls
    how that memory is represented.
    """

    def __init__(self, memory: object):
        self.memory = memory

    def normalize(
        self,
        proposal: dict | None,
        user_input: str,
        reasoning: str = "",
    ) -> dict | None:

        if not isinstance(proposal, dict):
            return None

        memory_type = proposal.get("type")
        subject = proposal.get("subject")
        fact = proposal.get("fact")
        confidence = proposal.get("confidence")

        if not isinstance(fact, str) or not fact.strip() or len(fact) > 4000:
            return None

        # Legacy note sections cannot represent embedded section delimiters losslessly.
        if "\r" in fact or re.search(r"^## ", fact, re.MULTILINE):
            return None

        if memory_type not in {
            "general",
            "fact",
            "preference",
            "identity",
            "relationship",
            "belief",
        }:
            return None

        if not isinstance(subject, str) or not subject.strip() or len(subject) > 200 or "\n" in subject or "\r" in subject:
            return None

        if type(confidence) not in (int, float) or not math.isfinite(confidence):
            return None

        confidence = max(0.0, min(1.0, float(confidence)))

        return {
            "type": memory_type,
            "subject": subject.strip(),
            "fact": fact.strip(),
            # Titles are deterministic, single-line data, never model-selected paths.
            "title": " ".join(fact.split())[:100],
            "confidence": confidence,
        }
