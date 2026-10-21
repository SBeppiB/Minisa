from abc import ABC, abstractmethod


class Reasoner(ABC):

    @abstractmethod
    def reason(self, context: dict) -> dict:
        """
        Produce reasoning from the supplied cognitive context.
        """
        pass

    def reflect(self, experience: str) -> dict:
        raise NotImplementedError(
            "This reasoner does not implement reflection."
        )

    def respond(self, context: dict, reasoning: dict) -> str | dict:
        raise NotImplementedError(
            "This reasoner does not implement responses."
        )
