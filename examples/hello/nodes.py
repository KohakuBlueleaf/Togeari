"""Nodes of the hello example: a translation lookup (served by a simulated slow service)
and a card composer."""

from kohakunode import NodeLibrary

lib = NodeLibrary("hello")

WORDS = {"fr": "Bonjour", "de": "Hallo", "es": "Hola", "zh": "Ni hao"}


@lib.node(outputs=["word"])
def translate(lang: str) -> str:
    if lang not in WORDS:
        raise KeyError(f"no greeting for {lang!r}")
    return WORDS[lang]


@lib.node(outputs=["card"])
def compose(word: str, name: str) -> str:
    return f"{word}, {name}!"
