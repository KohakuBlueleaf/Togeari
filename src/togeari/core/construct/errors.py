"""One error that lists every construction problem (spec/06 §6.2)."""

from dataclasses import dataclass


@dataclass
class Problem:
    where: str
    message: str

    def __str__(self) -> str:
        return f"  {self.where:<14} {self.message}"


class ConstructionError(Exception):
    def __init__(self, problems: list[Problem]):
        self.problems = problems
        lines = [f"ConstructionError: {len(problems)} problem{'s' if len(problems) != 1 else ''}"]
        lines += [str(p) for p in problems]
        super().__init__("\n".join(lines))


class Problems:
    """Collects problems; ``raise_if_any`` raises them all at once."""

    def __init__(self) -> None:
        self.items: list[Problem] = []
        self.warnings: list[Problem] = []

    def add(self, where: str, message: str) -> None:
        self.items.append(Problem(where, message))

    def warn(self, where: str, message: str) -> None:
        self.warnings.append(Problem(where, message))

    def raise_if_any(self) -> None:
        if self.items:
            raise ConstructionError(self.items)
