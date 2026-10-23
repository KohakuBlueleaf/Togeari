"""Verify wheel imports do not mix checkout Python sources with unrelated native binaries."""

import argparse
import importlib
import importlib.metadata
import json
import platform
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kir-revision", required=True)
    args = parser.parse_args()
    if len(args.kir_revision) != 40 or any(c not in "0123456789abcdef" for c in args.kir_revision):
        parser.error("--kir-revision must be the full immutable commit SHA built by this job")

    # Wheel RECORD membership verifies both imports come from the installed distribution.
    report = {"python": platform.python_version(), "kir_revision": args.kir_revision}
    for name in ("kohakunode", "togeari"):
        package = importlib.import_module(name)
        native = importlib.import_module(name + "._rs")
        distribution = importlib.metadata.distribution(name)
        owned = {Path(distribution.locate_file(f)).resolve() for f in distribution.files or ()}
        paths = [Path(package.__file__).resolve(), Path(native.__file__).resolve()]
        if any(path not in owned for path in paths):
            raise RuntimeError(f"{name}: imports are not owned by the installed wheel: {paths}")
        if paths[0].parent != paths[1].parent:
            raise RuntimeError(f"{name}: Python/native package directories differ: {paths}")
        report[name] = {
            "version": distribution.version,
            "python_source": str(paths[0]),
            "native_extension": str(paths[1]),
        }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
