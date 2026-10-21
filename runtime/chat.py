"""Interactive awake client. The configured server owns all cognitive writes."""
from runtime.client import MinisaClient


def main():
    client = MinisaClient()  # MINISA_AWAKE_URL is required; never guesses K/local Ollama.
    print("\nMINISA AC\n=========\nType 'exit' to end the conversation.\n")
    while True:
        try:
            message = input("You: ").strip()
        except (KeyboardInterrupt, EOFError):
            break
        if message.lower() in {"exit", "quit"}:
            break
        if not message:
            continue
        try:
            print(f"\nMinisa: {client.chat(message)['response']}\n")
        except Exception as error:
            print(f"\n[ERROR] {error}\n")


if __name__ == "__main__":
    main()
