from pathlib import Path


class PromptLoader:
    """Loads prompt files and records their logical version."""

    def __init__(self, prompt_dir: Path, version: str = "root") -> None:
        self.prompt_dir = prompt_dir
        self.version = version

    def load(self, name: str) -> str:
        prompt_path = self.prompt_dir / name
        if not prompt_path.exists():
            return ""
        return prompt_path.read_text(encoding="utf-8")

    def versions_for(self, names: list[str]) -> dict[str, str]:
        return {Path(name).stem: self.version for name in names}
