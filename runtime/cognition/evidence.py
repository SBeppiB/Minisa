"""Provenance describes origin, never independent verification of a claim.

Only runtime call sites assign these labels. Even a genuine observed operation
cannot establish the truth of a model's interpretation (including consciousness).
"""
from copy import deepcopy

PROVENANCE = frozenset({
    "runtime_observation", "user_statement", "stored_memory", "model_inference",
    "model_reflection", "unsupported_model_claim",
})


def evidence_record(provenance, content):
    if provenance not in PROVENANCE:
        raise ValueError("Unknown evidence provenance")
    return {"provenance": provenance, "content": deepcopy(content),
            "verification": "observation_only" if provenance == "runtime_observation" else "unverified",
            "independently_validates_belief": False}


def require_belief_admission():
    # There is no independently verifiable admission rule for the only current
    # belief subject: consciousness. Paths, confidence and provenance cannot
    # substitute for one. Keep proposals in audit records for operator review.
    raise ValueError("Belief proposal requires independent evidence admission and review; "
                     "model claims, reflection, confidence and recorded bytes cannot validate it")
