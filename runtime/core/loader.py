from pathlib import Path


class CoreLoader:
    def __init__(self, root: Path):
        self.root = root
        self.core_path = root / "CORE"

    def load(self) -> dict[str, str]:
        files = [
            "identity.md",
            "values.md",
            "goals.md",
            "personality.md",
            "instructions.md",
        ]

        core = {}

        for filename in files:
            path = self.core_path / filename

            if not path.exists():
                raise FileNotFoundError(
                    f"Missing core file: {path}"
                )

            core[filename.removesuffix(".md")] = path.read_text(
                encoding="utf-8"
            )

        return core
