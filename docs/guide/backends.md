# Backends

KempnerPulse can read DCGM metrics from three sources, selected with
`--backend`. All three flow through the same Translate → Compute → Present
pipeline; they differ only in how raw samples are acquired and at what time
resolution.

## `dcgm` (default)

Streams directly from a persistent `dcgmi dmon` subprocess on the local node.

- **Resolution:** honored down to a **100 ms** floor. DCGM profiling counters
  refresh at ~10 Hz through the shared hardware-counter multiplexer, so smaller
  intervals would yield mostly-blank profiling rows.
- **Aggregation:** each sample is a **point** snapshot of one multiplexer cycle.
- **Requires:** the DCGM host engine (`nv-hostengine`) and `dcgmi` on `PATH`,
  with profiling permitted for your user.
- **Best for:** single-node, high-resolution workload profiling.

```bash
kempnerpulse --backend dcgm --poll 0.1
```

## `prometheus`

Scrapes a [`dcgm-exporter`](https://github.com/NVIDIA/dcgm-exporter) `/metrics`
HTTP endpoint (or reads a saved exposition-format file).

- **Resolution:** bounded by the exporter's scrape interval, which is typically
  ~30 s for profiling fields. `--poll` must be `>= 1.0`; sub-second polling only
  duplicates samples.
- **Aggregation:** each sample is a **window** average over the exporter's
  collection interval.
- **Requires:** a reachable exporter endpoint (`--source`).
- **NVLink fallback:** the shipped `dcgm-exporter` counters CSVs omit
  `DCGM_FI_PROF_NVLINK_TX_BYTES` and `DCGM_FI_PROF_NVLINK_RX_BYTES`. Add both
  fields to the exporter's counters CSV to enable the TX+RX fallback when field
  449 is unavailable; otherwise field 449 remains the only NVLink source.
- **Best for:** fleet-level monitoring where an exporter is already deployed.

```bash
kempnerpulse --backend prometheus --source http://localhost:9400/metrics --poll 2
```

## `replay`

Replays a previously captured `dcgmi dmon` text file as a deterministic sample
stream — **no GPU or DCGM required**.

- **Best for:** reproducing a capture, demos, and continuous-integration tests.
- Point `--source` at the capture file.

```bash
kempnerpulse --backend replay --source capture.txt --once
kempnerpulse --backend replay --source capture.txt --export all
```

A capture suitable for replay is just the stdout of a `dcgmi dmon` run, or any
CSV/text the tool itself recorded; the replay backend re-emits its ticks with
synthetic, monotonically increasing timestamps so runs are reproducible.
