"""Parser tests for the dcgmi (direct DCGM) reader."""
import os

from kempnerpulse.reader.dcgmi import parse_dmon_block

_FIXTURE = os.path.join(
    os.path.dirname(__file__), "..", "fixtures", "dcgmi_dmon_2tick.txt"
)


def _load():
    with open(_FIXTURE, encoding="utf-8") as f:
        return f.read()


def test_one_record_per_gpu_row_in_order():
    records = parse_dmon_block(_load())
    # 2 ticks x 2 GPUs = 4 rows, in file order.
    assert [r.entity_id for r in records] == ["0", "1", "0", "1"]
    assert all(r.source == "dcgmi" for r in records)


def test_na_becomes_none_never_zero():
    records = parse_dmon_block(_load())
    cold_gpu0 = records[0]  # first tick: profiling fields are N/A
    assert cold_gpu0.fields["DCGM_FI_PROF_SM_ACTIVE"] is None
    assert cold_gpu0.fields["DCGM_FI_PROF_PIPE_TENSOR_ACTIVE"] is None
    assert cold_gpu0.fields["DCGM_FI_PROF_NVLINK_TX_BYTES"] is None
    assert cold_gpu0.fields["DCGM_FI_PROF_NVLINK_RX_BYTES"] is None
    # device fields in the same row are real values
    assert cold_gpu0.fields["DCGM_FI_DEV_POWER_USAGE"] == 350.5
    assert cold_gpu0.fields["DCGM_FI_DEV_NVLINK_BANDWIDTH_TOTAL"] == 25000.0


def test_nvlink_profile_fields_are_parsed():
    records = parse_dmon_block(_load())
    warm_gpu1 = records[3]
    assert warm_gpu1.fields["DCGM_FI_PROF_NVLINK_TX_BYTES"] == 210000000000.0
    assert warm_gpu1.fields["DCGM_FI_PROF_NVLINK_RX_BYTES"] == 220000000000.0


def test_skips_headers_and_blank_lines():
    text = "#Entity ...\nID\n\n   \nGPU 0 1\n"
    # Only one data row, one metric column present.
    records = parse_dmon_block(text)
    assert len(records) == 1
    assert records[0].entity_id == "0"
    assert records[0].fields["DCGM_FI_DEV_SM_CLOCK"] == 1.0
