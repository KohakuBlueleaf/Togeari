"""Load a KIR program and build one job program per entry (spec/02 §2.1, §2.2)."""

import copy
import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kohakunode import KirCheckError, compile_source, write
from kohakunode.lower import full_hash, run_passes, semantic_hash
from kohakunode.sig.env import NodeEnv
from kohakunode.syntax.ast import Def, Literal, Program

from togeari.core.construct.errors import Problems
from togeari.core.program.origin import program_origins

DEFAULT_PASSES = ["order_dataflow", "inline", "strip_meta"]


@dataclass
class Param:
    name: str
    has_default: bool
    default: Any = None


@dataclass
class JobProgram:
    """One entry of one program, after the machine's passes."""

    source: str
    entry: str | None
    program: Program  # lowered job program
    written: Program  # linked checked original (replay runs on it)
    env: NodeEnv
    params: list[Param]
    outputs: list[str] | None  # None: every top-level source name
    passes: list[str]
    semantic_hash: str
    full_hash: str
    text: str = ""
    warnings: list[str] = field(default_factory=list)
    dependencies: dict = field(default_factory=dict)
    origins: dict = field(default_factory=dict)
    linked_text: str = ""
    source_program: Program | None = None

    @property
    def name(self) -> str:
        return f"{self.source}:{self.entry or '<top>'}"


def _mix(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def with_dependencies(program_hash: str, dependencies: dict) -> str:
    """Include linked dependency identity without changing legacy no-module keys."""
    return _mix(program_hash, dependencies["hash"]) if dependencies else program_hash


def program_hashes(
    program: Program, entry: str | None, passes: list[str], dependencies: dict
) -> tuple[str, str]:
    """Hash executable code with exactly one dependency fingerprint, including on replay."""
    return (
        with_dependencies(_mix(semantic_hash(program), str(entry), ",".join(passes)), dependencies),
        with_dependencies(_mix(full_hash(program), str(entry), ",".join(passes)), dependencies),
    )


def read_source(program: str | os.PathLike) -> tuple[str, str, Path | None]:
    """(text, source name, base dir): a path (``os.PathLike``) is read; a ``str`` is KIR text."""
    if isinstance(program, os.PathLike):
        path = Path(program)
        return path.read_text(encoding="utf-8"), str(path), path.parent
    if isinstance(program, str):
        return program, "<text>", None
    raise TypeError(f"a program is a path or KIR text, not {type(program).__name__}")


def build_job_programs(
    program: str | os.PathLike,
    entries: list[str | None],
    libraries: list,
    sig_path: list[Path] | None,
    passes: list[str] | None,
    problems: Problems,
) -> list[JobProgram]:
    """Check the program, then lower one job program per entry."""
    try:
        text, source, base = read_source(program)
    except (OSError, TypeError) as exc:
        problems.add("program", f"{program}: {exc}")
        return []

    # The whole program checked once, unlowered
    passes = list(DEFAULT_PASSES if passes is None else passes)
    try:
        logical_source = Path(program).name if isinstance(program, os.PathLike) else source
        compiled = compile_source(
            text, libraries, base, sig_path, passes=[], source_name=logical_source
        )
    except KirCheckError as exc:
        for d in (d for d in exc.diagnostics if d.severity == "error"):
            loc = f"{source}:{d.line}" if getattr(d, "line", None) else source
            problems.add(loc, f"{d.code} {d.message}")
        return []
    except Exception as exc:  # noqa: BLE001
        problems.add(source, str(exc))
        return []

    # One lowered job program per entry
    defs = {s.name: s for s in compiled.program.body if isinstance(s, Def)}
    out: list[JobProgram] = []
    for entry in entries:
        if entry is not None and entry not in defs:
            problems.add(source, f"entry {entry!r} is not a top-level @def")
            continue
        job = _job_program(compiled.program, defs.get(entry) if entry else None)
        try:
            lowered = run_passes(job, compiled.env, passes)
            written = (
                run_passes(compiled.program, compiled.env, ["expand_collect"])
                if "expand_collect" in passes
                else compiled.program
            )
        except Exception as exc:  # noqa: BLE001
            problems.add(source, f"entry {entry!r}: {exc}")
            continue

        # Its parameters, outputs and hashes
        d = defs.get(entry) if entry else None
        params = []
        if d is not None:
            for p in d.params:
                default = p.default.value if isinstance(p.default, Literal) else None
                params.append(Param(p.name, p.default is not None, default))
        outputs = [o.name for o in d.outputs] if d is not None else None
        sem, full = program_hashes(lowered, entry, passes, compiled.dependencies)

        out.append(
            JobProgram(
                source,
                entry,
                lowered,
                written,
                compiled.env,
                params,
                outputs,
                passes,
                sem,
                full,
                text,
                [str(w) for w in compiled.warnings],
                dependencies=copy.deepcopy(compiled.dependencies),
                origins={
                    "written": program_origins(written),
                    "lowered": program_origins(lowered),
                },
                linked_text=write(compiled.program),
                source_program=compiled.source_program,
            )
        )
    return out


def _job_program(program: Program, entry: Def | None) -> Program:
    """The entry body as the top level, with the other `@def`s kept for `inline`."""
    p = copy.deepcopy(program)
    if entry is None:
        return p
    defs = [s for s in p.body if isinstance(s, Def) and s.name != entry.name]
    body = copy.deepcopy(entry.body)
    return Program(uses=p.uses, body=[*defs, *body], mode=p.mode)
