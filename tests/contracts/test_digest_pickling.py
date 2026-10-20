"""Pickled digests: the C pickler for values without sets, the ordered-set pickler otherwise."""

import importlib
from dataclasses import dataclass, field

# The module itself (the package exports a function of the same name)
digest_module = importlib.import_module("togeari.core.contracts.digest")


@dataclass
class Rec:
    name: str
    tags: list = field(default_factory=list)


@dataclass
class Group:
    name: str
    members: set = field(default_factory=set)


class Counting(digest_module._OrderedSetPickler):
    made = 0

    def __init__(self, *args, **kwargs):
        type(self).made += 1
        super().__init__(*args, **kwargs)


def test_the_ordered_pickler_runs_only_for_values_that_may_hold_a_set(monkeypatch):
    monkeypatch.setattr(digest_module, "_OrderedSetPickler", Counting)
    Counting.made = 0

    # No set: the C pickler's bytes, no ordered pickler
    digest_module.digest(Rec("a", ["x", "y"]))
    assert Counting.made == 0

    # A set inside: the ordered pickler, and the digest is the same for any member order
    first = digest_module.digest(Group("a", members={"p", "q", "r"}))
    second = digest_module.digest(Group("a", members={"r", "q", "p"}))
    assert Counting.made == 2
    assert first == second
