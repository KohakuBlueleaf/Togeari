"""The ``recorder`` plugin: conformance records of sampled jobs, kept for ``togeari conform``.

The machine's ``record`` knob samples the jobs and records their node calls in
retire order. The recorder keeps each recorded job's inputs (as admitted),
its call records, its outcome, and the programs needed to replay it; each
run writes its own files (``archive``), with the modules that registered
digest functions (``digest_providers``) in the run's ``run.json``.
"""

import pickle
from pathlib import Path

from togeari.core.contracts.digest import digest_providers
from togeari.core.contracts.io import JobRecord
from togeari.core.plugin.base import BasePlugin, PluginContext
from togeari.plugins.common.worker import left
from togeari.plugins.record.archive import ArchiveWriter, library_refs, program_info


class Recorder(BasePlugin):
    """Writes the conformance records of every job the machine records into ``path``.

    At most ``queue_size`` records wait for the writer; records past it are
    dropped and counted (``recorder`` query, ``dropped``). At unload the writer drains and
    ends by the unload's deadline (``close_s``); records still queued then are dropped.
    """

    name = "recorder"
    priority = 900

    def __init__(
        self,
        path: str | Path,
        libraries: list[str] | None = None,
        queue_size: int = 100_000,
        close_s: float = 10.0,
    ):
        self.path = Path(path)
        self.libraries = libraries
        self.queue_size = queue_size
        self.close_s = close_s
        self.ctx: PluginContext | None = None
        self.writer: ArchiveWriter | None = None
        self.inputs: dict[str, bytes | None] = {}
        self.known: set[str] = set()
        self.refs: list[str] = []
        self.providers: list[str] = []

    def on_load(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self.inputs.clear()
        self.known.clear()
        self.refs = self.libraries or library_refs(ctx.libraries)
        self.writer = ArchiveWriter(self.path, queue_size=self.queue_size)
        self.providers = self.writer.info["digest_providers"]

    def on_unload(self) -> None:
        if self.writer is not None:
            self.writer.close(timeout=left(self.unload_by(), self.close_s))
            self.writer = None

    def contribute_queries(self) -> dict:
        return {"recorder": lambda m: self.status()}

    def status(self) -> dict:
        writer = self.writer
        return {
            "path": str(self.path),
            "run": writer.run if writer is not None else None,
            "written": writer.written if writer is not None else 0,
            "dropped": writer.dropped if writer is not None else 0,
            "weak_jobs": writer.weak_jobs if writer is not None else 0,
            "weak_values": writer.weak_values if writer is not None else 0,
        }

    def on_job_start(self, job: dict) -> None:
        # Inputs are kept only for the jobs the machine records
        if not job.get("recorded"):
            return
        try:
            self.inputs[job["key"]] = pickle.dumps(job["inputs"])
        except Exception:  # noqa: BLE001
            self.inputs[job["key"]] = None

    def on_job_end(self, record: JobRecord) -> None:
        inputs = self.inputs.pop(record.key, None)
        if record.record is None or self.writer is None:
            return

        # Digest providers registered since the last job go into the run's info
        providers = digest_providers()
        if providers != self.providers:
            self.providers = providers
            self.writer.add_providers(providers)

        # The program the job started in, written once per semantic hash before its first job
        program = record.stats.get("program")
        if program is not None and program not in self.known:
            region = self.ctx.code.by_hash.get(program)
            if region is not None:
                self.known.add(program)
                info = program_info(region.job, self.refs, self.ctx.sig_path)
                self.writer.add_program(info)
        self.writer.add_job(record, inputs, program)
