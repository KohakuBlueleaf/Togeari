"""A positional result is stored as it was when it settled, whatever younger in-place changes do;
digests of dicts and sets do not depend on how their keys were made."""

import subprocess
import sys
from pathlib import Path

from rt_helpers import machine, sim_lane

from togeari import Inline, Use
from togeari.core.contracts.digest import digest
from togeari.stores import MemoryStore

PUSH = "@def (x: int)main(n, xs):\n    (x)mklist(xs)\n    (xs)push(n)\n"
HERE = Path(__file__).parent


def test_a_positional_result_changed_in_place_later_is_stored_unchanged():
    store = MemoryStore()
    outputs = []
    for _ in range(3):
        m = machine(
            program=PUSH,
            bind={"mklist": Use(sim_lane(), store="positional"), "push": Inline()},
            store=store,
        )
        try:
            outputs.append(m.submit({"x": 7}, key="k").result(5).outputs)
        finally:
            m.stop()

    # The first run stores `mklist`'s list before `push` changed it; reruns serve that list
    assert outputs == [{"n": 2, "xs": [7, 99]}] * 3
    assert list(store.data.values()) == [[7]]


class Tag:
    """Equal by value, hashed by value, printed with its address."""

    def __init__(self, v: int):
        self.v = v

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Tag) and other.v == self.v

    def __hash__(self) -> int:
        return hash(self.v)


def tags_digest(backwards: bool) -> bytes:
    """The digest of a set and a dict of eight tags, made in the given order."""
    order = range(7, -1, -1) if backwards else range(8)
    tags = [Tag(i) for i in order]
    return digest(({*tags}, {t: t.v for t in tags}))


def _in_process(backwards: bool) -> str:
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]);"
        "from test_positional_alias import tags_digest;"
        f"print(tags_digest({backwards}).hex())"
    )
    out = subprocess.run(
        [sys.executable, "-c", code, str(HERE)], capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def test_digests_of_sets_and_dicts_agree_across_processes_and_creation_orders():
    here = tags_digest(False).hex()
    assert _in_process(False) == here
    assert _in_process(True) == here
