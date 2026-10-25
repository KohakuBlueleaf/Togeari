"""JSON provenance sidecars, separate from executable text and semantic identity."""

from collections.abc import Iterator

from kohakunode.syntax.ast import Origin, Program, Stmt, children


def origin_dict(origin: Origin | None) -> dict | None:
    """An origin as JSON primitives; call ancestry is recursive and ordered."""
    if origin is None:
        return None
    return {
        "module": origin.module,
        "node_id": origin.node_id,
        "line": origin.line,
        "definition": origin.definition,
        "calls": [origin_dict(call) for call in origin.calls],
        "generated": origin.generated,
    }


def origin_from_dict(data: dict) -> Origin:
    return Origin(
        module=data["module"],
        node_id=data["node_id"],
        line=data.get("line"),
        definition=data.get("definition"),
        calls=tuple(origin_from_dict(call) for call in data.get("calls", [])),
        generated=data.get("generated"),
    )


def statements(body: list[Stmt], prefix: str = "body") -> Iterator[tuple[str, Stmt]]:
    """Every statement with its structural path (``body/2/body/0``), the ``path`` of its node
    in ``kohakunode.graph.to_graph``."""
    for index, stmt in enumerate(body):
        path = f"{prefix}/{index}"
        yield path, stmt
        for child_index, block in enumerate(children(stmt)):
            field = "body" if child_index == 0 else "handler"
            yield from statements(block, f"{path}/{field}")


def program_origins(program: Program) -> dict:
    """Version 1 maps structural statement paths to origins, not to generated names."""
    return {
        "version": 1,
        "statements": {
            path: origin_dict(stmt.origin)
            for path, stmt in statements(program.body)
            if stmt.origin is not None
        },
    }


def restore_origins(program: Program, sidecar: dict) -> None:
    """Restore an archived sidecar onto the same checked/lowered program structure."""
    if sidecar.get("version") != 1:
        raise ValueError(f"unsupported origin sidecar version {sidecar.get('version')!r}")
    by_path = dict(statements(program.body))
    for stmt in by_path.values():
        stmt.origin = None
    for path, data in sidecar["statements"].items():
        if path not in by_path:
            raise ValueError(f"origin path {path!r} is absent from the rebuilt program")
        by_path[path].origin = origin_from_dict(data)
