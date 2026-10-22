"""Replaying a record directory (written by ``recorder`` or ``run_report``) through the
reference.
"""

import copy
import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from kohakunode import parse
from kohakunode.modules import contract_hash
from kohakunode.sig.std import STD

from togeari.cli.loading import resolve
from togeari.conformance.replay import STEP_BUDGET, Verdict, conform
from togeari.core.construct.errors import Problems
from togeari.core.contracts.io import JobRecord
from togeari.core.program.job import JobProgram, build_job_programs, with_dependencies
from togeari.core.program.origin import restore_origins
from togeari.plugins.record.archive import Archive, import_providers


@dataclass
class Replayed:
    """One replayed job: ``weak`` lists values stored by ``repr``; ``weak_digests``
    counts the job's input digests compared by type only (type name -> count).
    """

    key: str
    entry: str
    verdict: Verdict
    weak: list
    run: str = ""
    weak_digests: dict = field(default_factory=dict)


def job_program(info: dict, libs: list[str]) -> JobProgram:
    """Rebuild the job program of one recorded entry; its semantic hash must match."""
    version = info.get("schema_version", 1)
    if version not in (1, 2):
        raise ValueError(f"unsupported recorded program schema version {version!r}")
    text = info["text"] if "\n" in info["text"] else info["text"] + "\n"
    problems = Problems()
    refs = libs or info["libraries"]
    try:
        libraries = [resolve(ref) for ref in refs]
    except (ImportError, AttributeError, ValueError) as exc:
        raise SystemExit(
            f"cannot import the node libraries {refs}: {exc}; put their module on PYTHONPATH"
            " or pass --lib module:attr"
        ) from exc
    sig_path = [Path(p) for p in info.get("sig_path") or []] or None
    jobs = build_job_programs(text, [info["entry"]], libraries, sig_path, info["passes"], problems)
    problems.raise_if_any()
    jp = jobs[0]
    dependencies = copy.deepcopy(info.get("dependencies", {}))
    if dependencies:
        if dependencies.get("version") != 1:
            raise ValueError("unsupported recorded dependency manifest version")
        payload = {k: v for k, v in dependencies.items() if k != "hash"}
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()
        if fingerprint != dependencies.get("hash"):
            raise ValueError("recorded dependency manifest hash differs")
        contracts = {lib.name: contract_hash(lib) for lib, _ in jp.env.libraries}
        contracts[STD.name] = contract_hash(STD)
        for signature in dependencies["signatures"]:
            if contracts.get(signature["library"]) != signature["hash"]:
                raise ValueError(f"recorded signature contract differs: {signature['library']}")
        jp.semantic_hash = with_dependencies(jp.semantic_hash, dependencies)
        jp.full_hash = with_dependencies(jp.full_hash, dependencies)
    if jp.semantic_hash != info["hash"]:
        raise ValueError(f"{info['source']}:{info['entry']}: rebuilt program hash differs")
    # Linked text is sufficient for execution; provenance is restored separately.
    jp.source = info["source"]
    jp.text = info.get("authored_text", info["text"])
    jp.source_program = parse(jp.text)
    jp.dependencies = dependencies
    if "origins" in info:
        jp.origins = copy.deepcopy(info["origins"])
        for name, program in (("written", jp.written), ("lowered", jp.program)):
            if name in jp.origins:
                restore_origins(program, jp.origins[name])
    return jp


def replay_dir(
    path: str | Path,
    libs: list[str] | None = None,
    runs: list[str] | None = None,
    reexecute: list[str] | None = None,
    step_budget: int = STEP_BUDGET,
    engine_errors: bool = True,
) -> Iterator[Replayed]:
    """Every recorded job of a directory (or of ``runs``), replayed against the program it ran.

    Before a run's first job, the run's digest providers are imported, so input
    digests are computed as in the run. With ``reexecute`` (node names), those
    nodes run again and must give their recorded results; every other node
    replays its record. ``engine_errors``: a recorded failure of a re-executed
    node that the node itself does not raise counts as an engine failure (True)
    or a mismatch (False). ``step_budget`` bounds the reference's instructions per job.
    """
    mode = "reexecute" if reexecute else "replay"
    pure = set(reexecute or ())
    archive = Archive(path, runs)
    programs: dict[tuple[str, str, int | None], JobProgram] = {}
    prepared: set[str] = set()
    for job in archive.jobs():
        name, program, run = job["entry"], job["program"], job["run"]
        weak_digests = dict(job["stats"].get("weak_digests") or {})

        # The run's digest providers, imported once
        if run not in prepared:
            prepared.add(run)
            providers = archive.run_info(run).get("digest_providers", [])
            failed = import_providers(providers)
            if failed:
                raise SystemExit(
                    f"run {run}: cannot import its digest providers ({'; '.join(failed)});"
                    " put their module on PYTHONPATH"
                )

        # Semantic code may have different provenance across runs and loaded regions.
        region = job["stats"].get("program_region")
        program_key = (run, program, region)
        if program_key not in programs:
            info = archive.program(program, run, region)
            if info is None:
                verdict = Verdict(False, f"no program {program!r} recorded for entry {name!r}")
                yield Replayed(job["key"], name, verdict, job["weak"], run, weak_digests)
                continue
            programs[program_key] = job_program(info, libs or [])

        rec = JobRecord(
            job["key"],
            name,
            job["ok"],
            job["outputs"],
            job["failure"],
            job["missing"],
            record=job["record"],
        )
        verdict = conform(
            programs[program_key],
            rec,
            job["inputs"] or {},
            mode=mode,
            pure=pure,
            engine_errors=engine_errors,
            step_budget=step_budget,
        )
        yield Replayed(job["key"], name, verdict, job["weak"], run, weak_digests)
