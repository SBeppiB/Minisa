class CognitiveRouter:
    """
    Determines the cognitive depth required for an input.

    FAST:
        Simple conversational input that does not require
        persistent context or substantial reasoning.

    NORMAL:
        Standard interaction requiring Minisa's context,
        memory, reasoning, and decision systems.

    DEEP:
        Input requiring extended reasoning, investigation,
        reflection, analysis, or belief examination.
    """

    FAST = "fast"
    NORMAL = "normal"
    DEEP = "deep"

    def route(self, user_input: str) -> str:
        text = user_input.strip().lower()

        if not text:
            return self.FAST

        deep_triggers = (
            "think deeply",
            "think about this",
            "reflect on",
            "analyze deeply",
            "investigate",
            "research",
            "figure out",
            "solve this",
            "why do you think",
            "what do you believe",
            "are you conscious",
            "are you actually conscious",
            "do you think you are conscious",
            "what are you",
            "who are you",
            "what do you know about yourself",
            "tell me about yourself",
        )

        if any(trigger in text for trigger in deep_triggers):
            return self.DEEP

        fast_inputs = {
            "hi",
            "hello",
            "hey",
            "yo",
            "hiya",
            "sup",
            "thanks",
            "thank you",
            "thx",
            "ok",
            "okay",
            "sure",
            "yes",
            "no",
            "yep",
            "yeah",
            "nope",
            "cool",
            "nice",
            "great",
        }

        if text in fast_inputs:
            return self.FAST

        return self.NORMAL

    def describe(self, route: str) -> str:
        descriptions = {
            self.FAST: "Minimal processing for simple interaction.",
            self.NORMAL: "Standard contextual cognition.",
            self.DEEP: "Extended reasoning and contextual analysis.",
        }

        return descriptions.get(
            route,
            descriptions[self.NORMAL],
        )