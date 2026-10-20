"""Digest edges: registered container subclasses, cycles, sets inside pickled objects."""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

from togeari.core.contracts.digest import (
    digest,
    digest_with_weak,
    register_digest,
    unregister_digest,
)


class Tagged(list):
    pass


def test_a_registered_list_subclass_uses_its_function():
    register_digest(Tagged, lambda v: b"tagged")
    try:
        assert digest(Tagged([1, 2])) == digest(Tagged([3]))
        assert digest(Tagged([1, 2])) != digest([1, 2])
    finally:
        unregister_digest(Tagged)
    assert digest(Tagged([1, 2])) != digest(Tagged([3]))


def test_a_self_referencing_value_is_weak_not_an_error():
    loop: list = [1]
    loop.append(loop)
    d, weak = digest_with_weak(loop)
    assert weak == ["list"]

    # A shared, non-cyclic child is no cycle
    child = [1, 2]
    assert digest_with_weak([child, child])[1] == []


SET_IN_OBJECT = """
import sys
sys.path.insert(0, {here!r})
from digest_obj import Holder
from togeari.core.contracts.digest import digest
print(digest(Holder({{"alpha", "beta", "gamma", "delta", "epsilon"}})).hex())
"""


def test_a_set_of_strings_inside_a_pickled_object_digests_alike_in_every_process(tmp_path):
    (tmp_path / "digest_obj.py").write_text(
        "class Holder:\n    def __init__(self, names):\n        self.names = names\n",
        encoding="utf-8",
    )
    script = textwrap.dedent(SET_IN_OBJECT.format(here=str(tmp_path)))
    seen = set()
    for seed in ("1", "2", "3"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        out = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, env=env, check=True
        )
        seen.add(out.stdout.strip())
    assert len(seen) == 1, seen
    assert Path(tmp_path / "digest_obj.py").exists()
