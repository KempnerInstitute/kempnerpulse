# DCGM Metrics Reference

KempnerPulse reads
[DCGM](https://developer.nvidia.com/dcgm) metrics exposed by
[dcgm-exporter](https://github.com/NVIDIA/dcgm-exporter) in Prometheus
text format. This page lists every metric the dashboard consumes, grouped by
category.

> **NVIDIA docs:**
> [DCGM API Reference: Field Identifiers](https://docs.nvidia.com/datacenter/dcgm/latest/dcgm-api/dcgm-api-field-ids.html)

---

## Profiling Metrics (ratio 0 to 1, displayed as 0 to 100 %)

These come from hardware performance counters and give the most accurate
picture of what the GPU is actually doing.

| Metric | DCGM Field | Description | Used In |
|--------|-----------|-------------|---------|
| SM Active | `DCGM_FI_PROF_SM_ACTIVE` | Fraction of cycles where at least one warp is active on an SM. Primary compute signal for **Real Utilization**. | Real Util, Classification, Fleet / Focus / Plot views |
| Tensor Active | `DCGM_FI_PROF_PIPE_TENSOR_ACTIVE` | Fraction of cycles the tensor core pipeline is active. Critical for AI/LLM workloads. | Real Util, Classification, Fleet / Focus / Plot views |
| DRAM Active | `DCGM_FI_PROF_DRAM_ACTIVE` | Fraction of cycles device memory (HBM) is actively moving data. Practical peak ~80 %. | Real Util, Classification, Fleet / Focus / Plot views |
| GR Engine Active | `DCGM_FI_PROF_GR_ENGINE_ACTIVE` | Fraction of time the graphics/compute engine is active (profiling-level). Falls back to `DCGM_FI_DEV_GPU_UTIL` when unavailable. | Real Util, Classification, Fleet / Focus / Plot views |
| SM Occupancy | `DCGM_FI_PROF_SM_OCCUPANCY` | Ratio of resident warps to theoretical maximum on SMs. | Focus view |
| FP16 Pipe | `DCGM_FI_PROF_PIPE_FP16_ACTIVE` | Fraction of cycles FP16 (half-precision) pipe is active. | Focus view |
| FP32 Pipe | `DCGM_FI_PROF_PIPE_FP32_ACTIVE` | Fraction of cycles FP32 (single-precision) pipe is active. | Focus view |
| FP64 Pipe | `DCGM_FI_PROF_PIPE_FP64_ACTIVE` | Fraction of cycles FP64 (double-precision) pipe is active. Used in HPC classification. | Classification, Focus view |
| TC FP16/BF16 (HMMA) | `DCGM_FI_PROF_PIPE_TENSOR_HMMA_ACTIVE` | Tensor core FP16/BF16 HMMA activity (Hopper/Ada+). | Focus view (if available) |
| TC INT8 (IMMA) | `DCGM_FI_PROF_PIPE_TENSOR_IMMA_ACTIVE` | Tensor core INT8 IMMA activity. | Focus view (if available) |
| TC FP64 (DFMA) | `DCGM_FI_PROF_PIPE_TENSOR_DFMA_ACTIVE` | Tensor core FP64 DFMA activity. | Focus view (if available) |
| TC TF32/FP32 (DMMA) | `DCGM_FI_PROF_PIPE_TENSOR_DMMA_ACTIVE` | Tensor core TF32/FP32 DMMA activity. | Focus view (if available) |
| TC FP8 (QMMA) | `DCGM_FI_PROF_PIPE_TENSOR_QMMA_ACTIVE` | Tensor core FP8 QMMA activity. | Focus view (if available) |

## Device-Level Utilization (0 to 100 %)

| Metric | DCGM Field | Description | Used In |
|--------|-----------|-------------|---------|
| GPU Util | `DCGM_FI_DEV_GPU_UTIL` | Percentage of time a kernel was running (same as `nvidia-smi`). Can stay high during overhead/sync/stalls. | Fleet / Focus / Plot views, fallback for GR Engine |
| Mem Copy Util | `DCGM_FI_DEV_MEM_COPY_UTIL` | Memory-copy engine utilization; ≥ 40 % triggers I/O classification. | Classification |

## Memory (absolute MiB)

| Metric | DCGM Field | Description | Used In |
|--------|-----------|-------------|---------|
| FB Used | `DCGM_FI_DEV_FB_USED` | Frame-buffer (VRAM) in use, MiB. | Memory bar, Fleet / Focus views |
| FB Free | `DCGM_FI_DEV_FB_FREE` | Frame-buffer free, MiB. | Memory total calculation |
| FB Reserved | `DCGM_FI_DEV_FB_RESERVED` | Frame-buffer reserved by the driver, MiB. | Memory total calculation |

Memory total = `FB_USED + FB_FREE + FB_RESERVED`.
Memory used % = `100 × FB_USED / total`.

## Temperature & Power

| Metric | DCGM Field | Description | Used In |
|--------|-----------|-------------|---------|
| GPU Temp | `DCGM_FI_DEV_GPU_TEMP` | GPU core temperature, °C. | Health check (HOT), Fleet / Focus views |
| Memory Temp | `DCGM_FI_DEV_MEMORY_TEMP` | HBM / VRAM temperature, °C. | Health check (HOT), Focus view |
| Power Usage | `DCGM_FI_DEV_POWER_USAGE` | Current power draw, watts. Shown alongside `nvidia-smi` power limit. | Fleet / Focus views |

## Clocks

| Metric | DCGM Field | Description | Used In |
|--------|-----------|-------------|---------|
| SM Clock | `DCGM_FI_DEV_SM_CLOCK` | Streaming Multiprocessor clock, MHz. | Focus view |
| Mem Clock | `DCGM_FI_DEV_MEM_CLOCK` | Memory clock, MHz. | Focus view |

## PCIe I/O (bytes/sec)

| Metric | DCGM Field | Description | Used In |
|--------|-----------|-------------|---------|
| PCIe RX | `DCGM_FI_PROF_PCIE_RX_BYTES` | PCIe receive throughput. ≥ 1 GB/s triggers I/O classification. | Classification, Fleet / Focus / Plot views |
| PCIe TX | `DCGM_FI_PROF_PCIE_TX_BYTES` | PCIe transmit throughput. ≥ 1 GB/s triggers I/O classification. | Classification, Fleet / Focus / Plot views |

## NVLink (bytes, monotonic counter converted to rate)

| Metric | DCGM Field | Description | Used In |
|--------|-----------|-------------|---------|
| NVLink BW Total | `DCGM_FI_DEV_NVLINK_BANDWIDTH_TOTAL` | Cumulative NVLink bytes. Dashboard computes Δ/s and displays GB/s. | Fleet / Focus views |

## Energy (monotonic counter)

| Metric | DCGM Field | Description | Used In |
|--------|-----------|-------------|---------|
| Total Energy | `DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION` | Cumulative energy in millijoules. Displayed as joules. | Focus view |

## Health & Error Counters

| Metric | DCGM Field | Description | Used In |
|--------|-----------|-------------|---------|
| PCIe Replay | `DCGM_FI_DEV_PCIE_REPLAY_COUNTER` | Monotonic PCIe replay count. Rate > 0/s triggers **WARN** health. | Health check |
| Row Remap Failure | `DCGM_FI_DEV_ROW_REMAP_FAILURE` | Row-remap failures. > 0 triggers **CRIT** health. | Health check |
| Uncorrectable Remapped Rows | `DCGM_FI_DEV_UNCORRECTABLE_REMAPPED_ROWS` | Uncorrectable row remaps. > 0 triggers **CRIT** health. | Health check |

---

## Further Reading

- [DCGM Field Identifiers (API Reference)](https://docs.nvidia.com/datacenter/dcgm/latest/dcgm-api/dcgm-api-field-ids.html)
- [dcgm-exporter GitHub](https://github.com/NVIDIA/dcgm-exporter)
- [DCGM Profiling Metrics Guide](https://docs.nvidia.com/datacenter/dcgm/latest/user-guide/feature-overview.html#profiling)
- [GPU Monitoring and Diagnostics Guide](https://docs.nvidia.com/datacenter/dcgm/latest/user-guide/getting-started.html)
