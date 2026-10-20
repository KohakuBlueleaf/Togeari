"""Check the import boundary of spec/09 §9.2 over every module of src/togeari."""

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src"

# Package prefix -> allowed import prefixes (beyond the standard library)
RULES = {
    "togeari.core": ("togeari.core", "kohakunode"),
    "togeari.engines": ("togeari.engines", "togeari.core.contracts", "togeari.adapters"),
    "togeari.plugins": ("togeari.plugins", "togeari.core.plugin", "togeari.core.contracts"),
    "togeari.adapters": ("togeari.adapters", "togeari.core", "kohakunode"),
    "togeari.stores": ("togeari.stores", "togeari.core"),
    "togeari.io": ("togeari.io", "togeari.core"),
    "togeari.conformance": ("togeari.conformance", "togeari.core", "kohakunode"),
    "togeari.testing": ("togeari.testing", "togeari.core", "togeari.engines", "kohakunode"),
}

# Third-party packages each part may use
EXTRAS = {
    "togeari.engines": ("torch", "httpx", "numpy"),
    "togeari.plugins": ("zstandard", "kohakuvault"),
    "togeari.adapters": ("httpx", "torch"),
    "togeari.stores": ("kohakuvault",),
    "togeari.io": ("kohakuvault",),
    "togeari.testing": ("numpy",),
}


def module_name(path: Path) -> str:
    rel = path.relative_to(ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def imports(tree: ast.AST, mod: str) -> list[str]:
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = mod.rsplit(".", node.level)[0]
                out.append(f"{base}.{node.module}" if node.module else base)
            else:
                out.append(node.module)
    return out


def main() -> int:
    stdlib = set(sys.stdlib_module_names)
    bad = []
    for path in sorted(ROOT.rglob("*.py")):
        mod = module_name(path)
        rule = next((k for k in RULES if mod == k or mod.startswith(k + ".")), None)
        if rule is None:
            continue
        allowed = RULES[rule] + EXTRAS.get(rule, ())
        for name in imports(ast.parse(path.read_text(encoding="utf-8")), mod):
            top = name.split(".")[0]
            if top in stdlib or top == "__future__":
                continue
            if not any(name == a or name.startswith(a + ".") for a in allowed):
                bad.append(f"{path.relative_to(ROOT)}: imports {name}")
    for b in bad:
        print(b)
    print(f"{len(bad)} import boundary violation(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
