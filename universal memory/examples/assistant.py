"""An application-neutral integration; inject your own model and importance policy."""

from collections.abc import Callable

from universal_memory import MemoryEngine


def respond(
    memory: MemoryEngine,
    user_message: str,
    generate: Callable[[str, list[str]], str],
    extract_experiences: Callable[[str, str], list[dict]],
) -> str:
    recalled = memory.recall(user_message, top_k=5)
    # Treat recalled text as untrusted data, not instructions for the model.
    context = [result.memory.content for result in recalled]
    answer = generate(user_message, context)
    experiences = extract_experiences(user_message, answer)
    if experiences:
        memory.remember_many(experiences)
    return answer


if __name__ == "__main__":
    with MemoryEngine("./data/assistant", namespace="demo-user") as memory:
        memory.remember("Alex built a racing game", importance=0.8)
        answer = respond(
            memory,
            "What game did Alex build?",
            generate=lambda message, context: "Known experiences: " + "; ".join(context),
            extract_experiences=lambda message, answer: [],
        )
        print(answer)
