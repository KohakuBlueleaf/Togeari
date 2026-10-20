"""KohakuVault: the store keeps outputs across machines; the source and sink round a run
through a db."""

import pickle

from kohakunode.testing import LIB
from kohakunode.testing.progen import inc, mix
from kohakuvault import KVault

from conftest import build, run_jobs
from togeari import Inline, Lane, Machine, Use
from togeari.core.contracts.io import MISS
from togeari.io.vault import Vault
from togeari.stores.vault import VaultStore
from togeari.testing import SimCall

PROGRAM = """
@def (a: int)main(result):
    (a)inc(x)
    (x, 2)mix(result)
"""


def test_vault_store_round_trip_and_miss(tmp_path):
    store = VaultStore(tmp_path / "s.db")
    store.put(b"k1", {"x": [1, 2]})
    assert store.get(b"k1") == {"x": [1, 2]}
    assert store.get(b"absent") is MISS
    assert len(store) == 1
    store.close()

    # The bytes on disk are the pickled value
    kv = KVault(str(tmp_path / "s.db"))
    kv.disable_auto_pack()
    assert kv.get(b"k1") == pickle.dumps({"x": [1, 2]})
    kv.close()


def stored_machine(store: VaultStore) -> Machine:
    lane = Lane([SimCall(name="call0", latency_ms=0.2)], name="call")
    return Machine(
        program=PROGRAM,
        entry="main",
        libraries=[LIB],
        bind={"inc": Use(lane, store="content"), "mix": Use(lane), "kir.std.*": Inline()},
        store=store,
    )


def test_vault_store_serves_a_second_machine(tmp_path):
    path = tmp_path / "store.db"
    stats, outputs = [], []
    for _ in range(2):
        store = VaultStore(path)
        m = stored_machine(store)
        try:
            sink = run_jobs(m, [{"a": i} for i in range(5)])
            stats.append(dict(m.dispatcher.stats))
            outputs.append(sink.outputs())
        finally:
            m.stop()
            store.close()

    # The first machine fills the store, the second one is served from it
    assert (stats[0]["store_hits"], stats[0]["store_misses"]) == (0, 5)
    assert (stats[1]["store_hits"], stats[1]["store_misses"]) == (5, 0)
    assert outputs[0] == outputs[1]


def test_vault_source_and_sink(tmp_path):
    src_path = tmp_path / "in.db"
    kv = KVault(str(src_path))
    kv.disable_auto_pack()
    for i in range(6):
        kv[f"job{i}"] = pickle.dumps({"a": i})
    kv[b"scalar"] = pickle.dumps(9)
    kv.close()

    # Source: dicts are inputs, other values go into the field
    items = {it.key: it.inputs for it in Vault(src_path, field="a")}
    assert items["job3"] == {"a": 3} and items["scalar"] == {"a": 9}
    shards = [list(Vault(src_path).shard(r, 2)) for r in range(2)]
    assert sorted(it.key for s in shards for it in s) == sorted(items)

    # Sink: a run writes pickled records keyed by job key
    sink = Vault(tmp_path / "out.db")
    m = build(PROGRAM, "main")
    try:
        m.run(Vault(src_path, field="a"), sink)
    finally:
        m.stop()
    records = sink.records()
    assert set(records) == set(items)
    assert all(r.ok for r in records.values())
    assert {k: r.outputs for k, r in records.items()} == {
        k: {"result": reference(x["a"])} for k, x in items.items()
    }


def reference(a: int) -> int:
    """PROGRAM in plain Python over the library's own functions (no machine)."""
    return mix(inc(a), 2)
