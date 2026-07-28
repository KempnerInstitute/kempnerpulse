"""Layer 2 orchestrator — turn a ``RawRecord`` into a ``CanonicalRecord``.

Per record: map source field names to canonical names, normalize units, drop
missing readings to ``None``, resolve entity identity, stamp the static source
context (provenance, aggregation mode, host/cluster metadata), assemble the
immutable ``CanonicalRecord``, and validate it.

Value-changing source corrections (e.g. treating the NVLink gauge as a counter
to difference) are intentionally *not* applied here — NVLink is normalized as a
gauge (MB/s → bytes/s) so the observable values are preserved.
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional

from ..reader.base import BackendKind, RawRecord
from .context import SourceContext, make_source_context
from .mapping import GPU_UUID_LABELS, convert, map_field
from .schema import SCHEMA_VERSION, CanonicalRecord

NVLINK_AGGREGATE_FIELD = "gpu_nvlink_aggregate_throughput_bytes_per_second"
NVLINK_TX_FIELD = "DCGM_FI_PROF_NVLINK_TX_BYTES"
NVLINK_RX_FIELD = "DCGM_FI_PROF_NVLINK_RX_BYTES"

# Static-metadata keys read from SourceContext.slurm_metadata -> record fields.
_SLURM_FIELDS = (
    "record_slurm_job_id",
    "record_slurm_step_id",
    "record_slurm_array_job_id",
    "record_slurm_array_task_id",
    "record_slurm_restart_count",
    "record_node_index_in_job",
    "record_mpi_rank",
)


class Translator:
    """Maps ``RawRecord`` objects to ``CanonicalRecord`` objects under a fixed ``SourceContext``."""

    def __init__(self, ctx: SourceContext) -> None:
        self.ctx = ctx

    def translate(self, raw: RawRecord) -> Optional[CanonicalRecord]:
        """Translate one record. Returns ``None`` for non-GPU entities."""
        try:
            gpu_index = int(raw.entity_id)
        except (TypeError, ValueError):
            return None  # e.g. the prometheus "global" bucket — not a GPU

        canonical: Dict[str, object] = {}
        uuid_from_label: Optional[str] = None
        for source_name, value in raw.fields.items():
            if source_name in GPU_UUID_LABELS and isinstance(value, str):
                uuid_from_label = value
                continue
            mapped = map_field(source_name)
            if mapped is None:
                continue  # unknown source field or a non-identity label
            canonical_name, unit_kind = mapped
            canonical[canonical_name] = convert(unit_kind, value)

        # DCGM field 449 is the preferred aggregate NVLink gauge. On some
        # systems it reports N/A while profiling fields 1011/1012 still expose
        # usable TX/RX rates, so synthesize the same canonical aggregate.
        # Both directions are required: summing a one-sided reading would
        # silently under-report the total rather than admit it is unknown.
        if canonical.get(NVLINK_AGGREGATE_FIELD) is None:
            tx = convert("number", raw.fields.get(NVLINK_TX_FIELD))
            rx = convert("number", raw.fields.get(NVLINK_RX_FIELD))
            if tx is not None and rx is not None:
                canonical[NVLINK_AGGREGATE_FIELD] = max(0.0, tx + rx)

        # Derived: total framebuffer = used + free + reserved (when all present).
        used = canonical.get("gpu_framebuffer_used_mebibytes")
        free = canonical.get("gpu_framebuffer_free_mebibytes")
        reserved = canonical.get("gpu_framebuffer_reserved_mebibytes")
        if used is not None and free is not None and reserved is not None:
            canonical["gpu_framebuffer_total_mebibytes"] = used + free + reserved

        uuid = (uuid_from_label
                or self.ctx.gpu_uuid_by_index.get(gpu_index)
                or "")

        meta = self.ctx.slurm_metadata
        slurm_kwargs = {f: meta.get(f) for f in _SLURM_FIELDS if f in meta}

        record = CanonicalRecord(
            record_schema_version=SCHEMA_VERSION,
            record_timestamp_monotonic_seconds=raw.timestamp,
            record_timestamp_wallclock_unix_seconds=raw.wallclock,
            record_aggregation_mode=self.ctx.aggregation_mode,
            record_window_microseconds=self.ctx.window_microseconds,
            record_freshness_microseconds=0,
            record_provenance=self.ctx.provenance,
            record_hostname=self.ctx.hostname,
            entity_gpu_index=gpu_index,
            entity_gpu_uuid=uuid,
            **slurm_kwargs,
            **canonical,
        )
        record.validate()
        return record

    def translate_tick(self, records: Iterable[RawRecord]) -> List[CanonicalRecord]:
        """Translate a tick's worth of records, dropping non-GPU entities."""
        out: List[CanonicalRecord] = []
        for raw in records:
            rec = self.translate(raw)
            if rec is not None:
                out.append(rec)
        return out


def make_translator(backend: BackendKind, **context_kwargs) -> Translator:
    """Construct a ``Translator`` with a backend-derived ``SourceContext``."""
    return Translator(make_source_context(backend, **context_kwargs))
