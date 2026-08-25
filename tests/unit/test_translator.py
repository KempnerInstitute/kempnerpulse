"""Translator tests — RawRecord -> CanonicalRecord (behavior-preserving units)."""
import os

from kempnerpulse.reader.base import BackendKind, RawRecord
from kempnerpulse.reader.dcgmi import parse_dmon_block
from kempnerpulse.translate import AggregationMode, Provenance, make_translator

_FIXTURE = os.path.join(
    os.path.dirname(__file__), "..", "fixtures", "dcgmi_dmon_2tick.txt"
)


def _records():
    with open(_FIXTURE, encoding="utf-8") as f:
        return parse_dmon_block(f.read())


def _translate_all():
    tr = make_translator(BackendKind.DCGMI, hostname="testnode")
    return tr, [tr.translate(r) for r in _records()]


def test_units_normalized_to_canonical():
    tr, recs = _translate_all()
    g1t2 = recs[3]  # gpu 1, second (full) tick
    assert g1t2.entity_gpu_index == 1
    # ratios pass through as fractions
    assert g1t2.gpu_streaming_multiprocessor_active_cycle_fraction == 0.97
    # NVML percent -> fraction
    assert g1t2.gpu_nvml_busy_time_fraction == 0.92
    # NVLink MB/s gauge -> bytes/s (behavior-preserving: ×1e6, not differenced)
    assert g1t2.gpu_nvlink_aggregate_throughput_bytes_per_second == 25600 * 1e6
    # millijoules -> joules
    assert abs(g1t2.gpu_board_total_energy_joules - 223466.789) < 1e-6
    # PCIe already bytes/s -> unchanged
    assert g1t2.gpu_pcie_transmit_throughput_bytes_per_second == 1600000000.0


def test_nvlink_profile_fields_fallback_when_aggregate_missing():
    assert _nvlink_raw(
        DCGM_FI_PROF_NVLINK_TX_BYTES=110e9,
        DCGM_FI_PROF_NVLINK_RX_BYTES=120e9,
    ) == 230e9


def test_nvlink_aggregate_field_takes_precedence_over_profile_fields():
    _, recs = _translate_all()
    g1t2 = recs[3]  # aggregate field is present, so TX/RX are ignored
    assert g1t2.gpu_nvlink_aggregate_throughput_bytes_per_second == 25600 * 1e6


def _nvlink_raw(**fields):
    tr = make_translator(BackendKind.DCGMI, hostname="testnode")
    raw = RawRecord(
        timestamp=0.0, wallclock=0.0, entity_id=0,
        fields=fields, source="dcgm", source_version="test",
    )
    return tr.translate(raw).gpu_nvlink_aggregate_throughput_bytes_per_second


def test_nvlink_zero_aggregate_is_idle_not_missing():
    """A real 0.0 from field 449 means idle, so the fallback must not fire."""
    assert _nvlink_raw(
        DCGM_FI_DEV_NVLINK_BANDWIDTH_TOTAL=0.0,
        DCGM_FI_PROF_NVLINK_TX_BYTES=110e9,
        DCGM_FI_PROF_NVLINK_RX_BYTES=120e9,
    ) == 0.0


def test_nvlink_one_sided_profile_reading_is_not_reported():
    """Half a sum would under-report the total, so report nothing instead."""
    assert _nvlink_raw(DCGM_FI_PROF_NVLINK_TX_BYTES=110e9) is None
    assert _nvlink_raw(DCGM_FI_PROF_NVLINK_RX_BYTES=120e9) is None


def test_nvlink_negative_profile_reading_is_clamped():
    """Matches the clamp the mapped 449 path gets, so validation cannot fail."""
    assert _nvlink_raw(
        DCGM_FI_PROF_NVLINK_TX_BYTES=-5e9,
        DCGM_FI_PROF_NVLINK_RX_BYTES=1e9,
    ) == 0.0


def test_nvlink_absent_everywhere_stays_none():
    assert _nvlink_raw(DCGM_FI_DEV_POWER_USAGE=100.0) is None


def test_framebuffer_total_derived():
    _, recs = _translate_all()
    g1t2 = recs[3]
    assert g1t2.gpu_framebuffer_total_mebibytes == 12050 + 69000 + 512


def test_na_stays_none():
    _, recs = _translate_all()
    g0t1 = recs[0]  # cold first tick: profiling fields were N/A
    assert g0t1.gpu_streaming_multiprocessor_active_cycle_fraction is None
    assert g0t1.gpu_tensor_core_pipe_active_cycle_fraction is None


def test_metadata_stamped_from_context():
    _, recs = _translate_all()
    rec = recs[3]
    assert rec.record_aggregation_mode is AggregationMode.POINT
    assert rec.record_window_microseconds == 100_000
    assert rec.record_provenance is Provenance.DCGMI
    assert rec.record_hostname == "testnode"
    assert rec.record_schema_version == 1


def test_all_records_schema_valid():
    _, recs = _translate_all()
    for rec in recs:
        rec.validate()  # must not raise (translate already validated)


def test_non_gpu_entity_is_dropped():
    tr = make_translator(BackendKind.DCGMI)
    raw = RawRecord(
        timestamp=0.0, wallclock=0.0, entity_id="global",
        fields={"some_metric": 1.0}, source="prometheus", source_version="x",
    )
    assert tr.translate(raw) is None
