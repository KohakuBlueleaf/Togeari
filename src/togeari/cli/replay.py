"""Replaying a record directory (written by ``recorder`` or ``run_report``) through the
reference.
"""

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from togeari.cli.loading import resolve
from togeari.conformance.replay import Verdict, conform
from togeari.core.construct.errors import Problems
from togeari.core.contracts.io import JobRecord
from togeari.core.program.job import JobProgram, build_job_programs
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
    if jp.semantic_hash != info["hash"]:
        raise ValueError(f"{info['source']}:{info['entry']}: rebuilt program hash differs")
    return jp


def replay_dir(
    path: str | Path,
    libs: list[str] | None = None,
    runs: list[str] | None = None,
    reexecute: list[str] | None = None,
) -> Iterator[Replayed]:
    """Every recorded job of a directory (or of ``runs``), replayed against the program it ran.

    Before a run's first job, the run's digest providers are imported, so input
    digests are computed as in the run. With ``reexecute`` (node names), those
    nodes run again and must give their recorded results; every other node
    replays its record.
    """
    mode = "reexecute" if reexecute else "replay"
    pure = set(reexecute or ())
    archive = Archive(path, runs)
    programs: dict[str, JobProgram] = {}
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

        # The job's program, by semantic hash, rebuilt once
        if program not in programs:
            info = archive.programs.get(program)
            if info is None:
                verdict = Verdict(False, f"no program {program!r} recorded for entry {name!r}")
                yield Replayed(job["key"], name, verdict, job["weak"], run, weak_digests)
                continue
            programs[program] = job_program(info, libs or [])

        rec = JobRecord(
            job["key"],
            name,
            job["ok"],
            job["outputs"],
            job["failure"],
            job["missing"],
            record=job["record"],
        )
        verdict = conform(programs[program], rec, job["inputs"] or {}, mode=mode, pure=pure)
        yield Replayed(job["key"], name, verdict, job["weak"], run, weak_digests)
