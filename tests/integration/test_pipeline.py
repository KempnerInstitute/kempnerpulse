"""End-to-end: replay capture -> Read -> Translate -> Compute -> Present.

Exercises the whole standalone pipeline without GPU hardware, via the replay
backend, the same tick-sourcing the lifecycle uses, and both presenters
(CSV + dashboard render).
"""
import io
import os

from rich.console import Console

from kempnerpulse.compute import PRESETS, compute_record
from kempnerpulse.lifecycle import _tick_iterator
from kempnerpulse.present import (
    CommandController,
    HistoryStore,
    SummaryContext,
    csv_header,
    csv_row,
    render_dashboard,
    resolve_columns,
    update_history,
)
from kempnerpulse.reader import BackendKind, ReaderConfig, make_backend
from kempnerpulse.translate import make_translator

_FIXTURE = os.path.join(os.path.dirname(__file__), "..", "fixtures", "dcgmi_dmon_2tick.txt")
_MODELS = {0: "NVIDIA H100", 1: "NVIDIA H100"}


def _computed_ticks():
    cfg = ReaderConfig(backend=BackendKind.REPLAY, source=_FIXTURE, poll_seconds=0.1)
    backend = make_backend(cfg)
    backend.open(cfg)
    try:
        raw_ticks = list(_tick_iterator(backend))
    finally:
        backend.close()
    tr = make_translator(BackendKind.REPLAY, hostname="t", gpu_model_by_index=_MODELS)
    out, prev = [], {}
    for tick in raw_ticks:
        recs = []
        for canon in tr.translate_tick(tick):
            cr = compute_record(canon, prev=prev.get(canon.entity_gpu_index),
                                weights=PRESETS["ai"], preset_name="ai",
                                model_name=_MODELS.get(canon.entity_gpu_index))
            prev[canon.entity_gpu_index] = canon
            recs.append(cr)
        out.append(recs)
    return out


def test_pipeline_yields_computed_records():
    ticks = _computed_ticks()
    assert len(ticks) == 2  # two ticks in the fixture
    last = ticks[-1]
    assert [c.gpu_id for c in last] == ["0", "1"]
    for c in last:
        assert 0.0 <= c.real_util <= 100.0
        assert c.health in ("OK", "WARN", "HOT", "CRIT")
        assert c.workload_class.label  # a non-empty status label


def test_pipeline_csv_export_matches_header_width():
    last = _computed_ticks()[-1]
    assert last[0].record.gpu_nvlink_aggregate_throughput_bytes_per_second is None
    cols = resolve_columns("all")
    header = csv_header(cols)
    assert "real_util_pct" in header and "sm_active_pct" in header and "nvlink_gbps" in header
    ts = last[0].record.record_timestamp_wallclock_unix_seconds
    row = csv_row(last[0], ts, cols)
    assert len(row) == len(header)
    # N/A stays empty, never coerced to 0 (GPU 0 NVLink is unavailable in tick 2).
    assert row[header.index("nvlink_gbps")] == ""


def test_pipeline_dashboard_renders():
    last = _computed_ticks()[-1]
    history = HistoryStore()
    update_history(history, last)
    console = Console(file=io.StringIO(), width=160, height=48)
    layout = render_dashboard(
        last, history, console_width=160, console_height=48,
        controller=CommandController(), summary_context=SummaryContext(app_version="test"),
    )
    console.print(layout)
    assert console.file.getvalue().strip()  # rendered non-empty output
