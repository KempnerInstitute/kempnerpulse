# CSV Export Reference

KempnerPulse can export GPU metrics as CSV for offline analysis or terminal
monitoring. Rows are emitted for every GPU in the visibility set
(`CUDA_VISIBLE_DEVICES` / `SLURM_JOB_GPUS` / `--gpus` / `--show-all`),
regardless of whether a compute process is currently running. This lets you
start the recorder before a job launches so the trace covers job startup.

## Usage

```bash
# Default columns — pipe to file or watch on terminal
kempnerpulse --export > metrics.csv

# All 34 columns
kempnerpulse --export all > metrics.csv

# Custom column selection
kempnerpulse --export timestamp,gpu_id,real_util_pct,tensor_active_pct > metrics.csv

# Single snapshot
kempnerpulse --export --once

# Combine with other flags
kempnerpulse --export all --poll 5 --gpus 0,1 > metrics.csv

# High-resolution sampling via the dcgm backend (down to 100ms)
kempnerpulse --backend dcgm --export all --poll 0.1 > metrics.csv
```

## Sampling Rate (`--poll`)

`--poll` semantics depend on the backend:

| Backend | Effective range | Notes |
|---------|----------------|-------|
| `dcgm` (recommended for export) | `0.1s` – any | Drives a persistent `dcgmi dmon` stream at the requested interval. Values below 100ms are clamped with a notice — DCGM's profiling counters (`DCGM_FI_PROF_*`, i.e. SM/Tensor/DRAM Active and friends) refresh at ~10Hz via the shared hardware-counter multiplexer, so smaller intervals just produce blank profiling rows. One CSV row-set is emitted per dcgmi tick — no spawned subprocess per cycle, no skew. |
| `prometheus` (default) | `>= 1.0s` | dcgm-exporter scrapes profiling fields at ~30s, so sub-second `--poll` values produce duplicate rows with no new data. Sub-second values are rejected with a warning. |

For high-resolution profiling traces (e.g., capturing tensor activity at
100ms resolution to plot offline), use `--backend dcgm --poll 0.1`. Note
that only the profiling columns are bounded by the 10Hz internal
refresh; device columns (clocks, temps, power, framebuffer) are sampled
every tick and would update faster if the floor were lowered — but we
keep the floor at 100ms because Real Util and the workload
classification depend on the profiling counters.

## Default Columns

When using `--export` without arguments, the following 9 columns are exported:

`timestamp, gpu_id, model, gpu_util_pct, mem_used_mib, real_util_pct,
sm_active_pct, tensor_active_pct, dram_active_pct`

## All Available Columns

Use `--export all` to include every column, or `--export col1,col2,...` to
pick a custom set.

| Column | Description |
|--------|-------------|
| `timestamp` | Unix epoch seconds |
| `gpu_id` | GPU index |
| `model` | GPU model (e.g. H100, A100) |
| `real_util_pct` | Weighted Real Utilization % |
| `status` | Workload classification |
| `health` | Health state (OK/WARN/HOT/CRIT) |
| `sm_active_pct` | SM Active % |
| `tensor_active_pct` | Tensor pipe active % |
| `dram_active_pct` | DRAM active % |
| `gr_engine_active_pct` | GR Engine active % |
| `gpu_util_pct` | GPU Utilization % (nvidia-smi) |
| `mem_used_mib` | Framebuffer used (MiB) |
| `mem_total_mib` | Framebuffer total (MiB) |
| `mem_used_pct` | Framebuffer used % |
| `power_w` | Power draw (W) |
| `gpu_temp_c` | GPU temperature (°C) |
| `mem_temp_c` | Memory temperature (°C) |
| `sm_occupancy_pct` | SM Occupancy % |
| `fp16_pipe_pct` | FP16 pipe active % |
| `fp32_pipe_pct` | FP32 pipe active % |
| `fp64_pipe_pct` | FP64 pipe active % |
| `memcpy_util_pct` | Memory copy utilization % |
| `pcie_rx_bytes_s` | PCIe receive (bytes/s) |
| `pcie_tx_bytes_s` | PCIe transmit (bytes/s) |
| `nvlink_gbps` | NVLink throughput (GB/s) |
| `sm_clock_mhz` | SM clock (MHz) |
| `mem_clock_mhz` | Memory clock (MHz) |
| `pcie_replay_rate_s` | PCIe replay rate (/s) |
| `energy_j` | Cumulative energy (J) |
| `tc_hmma_pct` | TC FP16/BF16 HMMA % |
| `tc_imma_pct` | TC INT8 IMMA % |
| `tc_dfma_pct` | TC FP64 DFMA % |
| `tc_dmma_pct` | TC TF32/FP32 DMMA % |
| `tc_qmma_pct` | TC FP8 QMMA % |

## Notes

- **Timestamp**: Unix epoch seconds with centisecond precision (e.g.
  `1743782400.12`). Convert with `pd.to_datetime(df.timestamp, unit='s')`.
- **GPU filtering**: Only GPUs where the current user has at least one running
  compute process are included. If no processes are found, only the header is
  output and a diagnostic message is printed to stderr.
- **Rate fields**: `nvlink_gbps` and `pcie_replay_rate_s` require two samples
  to compute a rate, so they will be empty on the first row.
- **Missing values**: Exported as empty strings in the CSV.
- **Pipe-friendly**: Output is flushed after each poll interval. Handles
  `BrokenPipeError` gracefully (e.g. `kempnerpulse --export | head -20`).
