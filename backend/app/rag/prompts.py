from pathlib import Path


class PromptRepository:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or Path("prompts")
        self._cache: dict[str, str] = {}

    def load(self, relative_path: str) -> str:
        if relative_path in self._cache:
            return self._cache[relative_path]
        path = (self.root / relative_path).resolve()
        root = self.root.resolve()
        if root not in path.parents:
            raise ValueError("Prompt path escapes the prompt directory")
        content = path.read_text(encoding="utf-8").strip()
        self._cache[relative_path] = content
        return content
