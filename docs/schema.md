# Canonical record schema

`CanonicalRecord` (in `kempnerpulse.translate.schema`) is KempnerPulse's
internal, vendor-neutral vocabulary for one GPU reading at one instant. The
Translate layer converts each backend's raw output (DCGM field IDs, Prometheus
metric names, …) into this single shape; everything downstream — the Real
Utilization composite, the workload classification, the terminal UI, and the
CSV export — reads canonical fields and never sees a vendor identifier.

Current `SCHEMA_VERSION`: **1**.

## Naming convention

Every field is `snake_case` and follows `<scope>_<subsystem>_<aspect>_<unit>`:

- **scope** — `record_` (record metadata), `entity_` (GPU / MIG identity),
  `gpu_` (per-GPU hardware reading).
- **ratios** end in `_fraction` and lie in `[0.0, 1.0]` — never `_pct`. A
  consumer that wants a 0–100 percentage multiplies by 100 itself.
- **throughputs** carry `_bytes_per_second`; cumulative counters use the bare
  unit (`_joules`); event counts use `_count`.
- units are spelled out: `_celsius`, `_megahertz`, `_mebibytes`, `_watts`,
  `_microseconds`.

`None` always means *the source did not provide this reading*. It is never
silently coerced to `0`; a real zero stays `0`.

## Enums

```python
class AggregationMode(Enum):
    POINT  = "point"    # one ~100 ms snapshot (e.g. dcgmi at --poll 0.1)
    WINDOW = "window"   # time-average over record_window_microseconds (e.g. prometheus)

class Provenance(Enum):
    DCGMI         = "dcgmi"
    PROMETHEUS    = "prometheus"
    NVML_FALLBACK = "nvml_fallback"   # a counter was substituted from NVML
    REPLAY        = "replay"
```

## Record metadata (required)

| Field | Type | Unit / range |
|---|---|---|
| `record_schema_version` | `int` | ≥ 1 |
| `record_timestamp_monotonic_seconds` | `float` | seconds since reader start |
| `record_timestamp_wallclock_unix_seconds` | `float` | unix seconds |
| `record_aggregation_mode` | `AggregationMode` | `POINT` / `WINDOW` |
| `record_window_microseconds` | `int` | integration window (µs) |
| `record_freshness_microseconds` | `int` | staleness at delivery (µs) |
| `record_provenance` | `Provenance` | source of the record |
| `record_hostname` | `str` | always populated |

## Entity identity

| Field | Type | Notes |
|---|---|---|
| `entity_gpu_index` | `int` | logical index as the reader sees it (required) |
| `entity_gpu_uuid` | `str` | hardware-stable identifier (required) |
| `entity_mig_instance_index` | `Optional[int]` | `None` outside MIG mode |
| `entity_process_id` | `Optional[int]` | reserved (per-process attribution) |
| `entity_process_command_line_truncated` | `Optional[str]` | reserved |

## Cluster / Slurm / MPI metadata (optional)

`record_slurm_job_id`, `record_slurm_step_id`, `record_slurm_array_job_id`,
`record_slurm_array_task_id`, `record_slurm_restart_count`,
`record_node_index_in_job`, `record_mpi_rank`,
`record_capture_clock_offset_microseconds` — all `Optional`, tolerate absence so
local / non-Slurm runs still produce valid records.

## Reserved user-annotation metadata (optional)

`record_user_annotation_iteration_index`, `_phase_label`, `_step_count`,
`_request_id`, `_token_count` — defined for forward compatibility; emitted as
`None` in v0.5.0.

## GPU hardware readings (all `Optional`)

| Field | Unit / range |
|---|---|
| `gpu_streaming_multiprocessor_active_cycle_fraction` | `[0,1]` |
| `gpu_streaming_multiprocessor_warp_occupancy_fraction` | `[0,1]` |
| `gpu_tensor_core_pipe_active_cycle_fraction` | `[0,1]` (umbrella) |
| `gpu_tensor_core_half_precision_mma_active_cycle_fraction` | `[0,1]` (HMMA: FP16/BF16) |
| `gpu_tensor_core_integer_mma_active_cycle_fraction` | `[0,1]` (IMMA: INT8) |
| `gpu_tensor_core_double_precision_fma_active_cycle_fraction` | `[0,1]` (DFMA: FP64) |
| `gpu_tensor_core_double_mma_active_cycle_fraction` | `[0,1]` (DMMA — reserved) |
| `gpu_tensor_core_quarter_mma_active_cycle_fraction` | `[0,1]` (QMMA — reserved) |
| `gpu_cuda_core_floating_point_64bit_pipe_active_cycle_fraction` | `[0,1]` |
| `gpu_cuda_core_floating_point_32bit_pipe_active_cycle_fraction` | `[0,1]` |
| `gpu_cuda_core_floating_point_16bit_pipe_active_cycle_fraction` | `[0,1]` |
| `gpu_graphics_compute_engine_active_cycle_fraction` | `[0,1]` |
| `gpu_dram_controller_active_cycle_fraction` | `[0,1]` |
| `gpu_memory_copy_engine_busy_time_fraction` | `[0,1]` |
| `gpu_pcie_transmit_throughput_bytes_per_second` | bytes/s (differenced) |
| `gpu_pcie_receive_throughput_bytes_per_second` | bytes/s (differenced) |
| `gpu_pcie_replay_count` | count (cumulative) |
| `gpu_nvlink_aggregate_throughput_bytes_per_second` | bytes/s (gauge; not differenced) |
| `gpu_board_power_draw_watts` | watts |
| `gpu_board_total_energy_joules` | joules (cumulative) |
| `gpu_board_enforced_power_limit_watts` | watts |
| `gpu_board_default_power_limit_watts` | watts |
| `gpu_die_temperature_celsius` | °C |
| `gpu_memory_die_temperature_celsius` | °C |
| `gpu_streaming_multiprocessor_clock_frequency_megahertz` | MHz |
| `gpu_memory_clock_frequency_megahertz` | MHz |
| `gpu_framebuffer_used_mebibytes` | MiB |
| `gpu_framebuffer_free_mebibytes` | MiB |
| `gpu_framebuffer_reserved_mebibytes` | MiB |
| `gpu_framebuffer_total_mebibytes` | MiB (derived: used + free + reserved) |
| `gpu_nvml_busy_time_fraction` | `[0,1]` (the `nvidia-smi` GPU-Util time-fraction) |
| `gpu_xid_error_count` | count (cumulative) |
| `gpu_uncorrectable_remapped_row_count` | count |
| `gpu_correctable_remapped_row_count` | count |
| `gpu_row_remap_failure_flag` | bool |

Per-link NVLink rates are defined as a reserved naming pattern but are not
collected in the default schema.

## Invariants

`CanonicalRecord.validate()` raises `TranslateError` if any single-record
invariant fails:

1. `record_schema_version >= 1`.
2. Every `_fraction` field, if not `None`, lies in `[0.0, 1.0]`.
3. Every count and physical magnitude (throughput, power, energy, clock,
   framebuffer), if not `None`, is `>= 0`.
4. `record_window_microseconds` and `record_freshness_microseconds` are `>= 0`.
5. `POINT` records have `record_window_microseconds <= 200_000`; `WINDOW`
   records have `record_window_microseconds > 200_000`.

One invariant is **cross-record** and therefore enforced by the Translate
differencer (which sees the sequence), not by `validate()`:
`gpu_board_total_energy_joules` is monotonically non-decreasing per entity.

## Versioning

Adding a field is a minor `SCHEMA_VERSION` bump (`N` → `N+1`); readers on an
older version tolerate unknown extra fields. Removing or renaming a field is a
major bump. The classification labels and the Real Utilization composite are
**Compute-layer outputs**, not part of `CanonicalRecord`.
