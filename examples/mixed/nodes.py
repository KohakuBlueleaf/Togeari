"""Nodes of the mixed example: their signatures, and bodies that run when a node is inline."""

import threading
from pathlib import Path

import scorer
import service
import spectra
import torch
from kohakunode import NodeLibrary

lib = NodeLibrary("stations")

# The survey log the ordered nodes append to (set by the machine module); one writer at a time
LOG = {"path": None}
LOG_LOCK = threading.Lock()
REFERENCE = scorer.make_module().eval()


@lib.node(outputs=["gain"])
def lookup(station: str) -> float:
    if not service.known(station):
        raise KeyError(f"unknown station {station!r}")
    return service.gain(station)


lib.node(outputs=["feats"])(spectra.features)


@lib.node(outputs=["scores"])
def embed(feats: list) -> list:
    with torch.inference_mode():
        return REFERENCE(torch.tensor([feats])).tolist()[0]


@lib.node(outputs=["label"])
def classify(scores: list, gain: float) -> str:
    best = max(range(len(scores)), key=lambda i: scores[i])
    level = "strong" if scores[best] * gain > 1.0 else "weak"
    return f"{scorer.CLASSES[best]}/{level}"


@lib.node(outputs=["verdict"])
def majority(labels: list) -> str:
    if not labels:
        return "silent"
    return max(sorted(set(labels)), key=labels.count)


@lib.node(outputs=["line"], ordered=True)
def note(station: str, window: dict, label: str) -> str:
    return _append(f"{station} window {window['i']}: {label}")


@lib.node(outputs=["line"], ordered=True)
def close(station: str, verdict: str) -> str:
    return _append(f"{station} verdict: {verdict}")


def _append(line: str) -> str:
    if LOG["path"] is not None:
        with LOG_LOCK, Path(LOG["path"]).open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    return line
