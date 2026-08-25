"""Layer 1 (Read) — direct DCGM backend (``dcgmi dmon``).

Streams hardware counters from a local ``dcgmi dmon`` subprocess and emits one
``RawRecord`` per GPU per sampling tick, keyed by DCGM field names (the source's
own vocabulary). An ``N/A`` reading becomes ``None``; it is never coerced to
``0``. Assigning meaning to these fields (canonical names, units, scaling) is
Layer 2's job, not this module's.

Entry points:
  * ``DcgmiBackend`` — a persistent ``dcgmi dmon -c 0`` stream for live monitoring.
  * ``read_once`` — a single synchronous ``dcgmi dmon -c 2`` collection, for
    one-shot queries where a long-lived stream is unnecessary.
  * ``parse_dmon_block`` — pure text -> ``RawRecord`` parser (the testable core).
  * ``resolve_dcgm_gpu_ids`` — map process-visible GPUs to physical dcgmi IDs.
"""
from __future__ import annotations

import math
import os
import re
import subprocess
import time
from typing import Dict, Iterator, List, Optional, Tuple

from .base import (
    Backend,
    BackendCaps,
    BackendKind,
    DcgmStreamError,
    RawRecord,
    ReaderConfig,
)

# DCGM field id -> field name. Order is significant: it fixes the column
# positions in ``dcgmi dmon`` output, which the parser reads positionally.
DCGM_DMON_FIELDS: Tuple[Tuple[int, str], ...] = (
    # Device-level metrics
    (100,  "DCGM_FI_DEV_SM_CLOCK"),                 # MHz
    (101,  "DCGM_FI_DEV_MEM_CLOCK"),                # MHz
    (140,  "DCGM_FI_DEV_MEMORY_TEMP"),              # Celsius
    (150,  "DCGM_FI_DEV_GPU_TEMP"),                 # Celsius
    (155,  "DCGM_FI_DEV_POWER_USAGE"),              # Watts
    (156,  "DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION"), # millijoules (counter)
    (202,  "DCGM_FI_DEV_PCIE_REPLAY_COUNTER"),      # counter
    (203,  "DCGM_FI_DEV_GPU_UTIL"),                 # 0-100%
    (204,  "DCGM_FI_DEV_MEM_COPY_UTIL"),            # 0-100%
    (251,  "DCGM_FI_DEV_FB_FREE"),                  # MiB
    (252,  "DCGM_FI_DEV_FB_USED"),                  # MiB
    (253,  "DCGM_FI_DEV_FB_RESERVED"),              # MiB
    (449,  "DCGM_FI_DEV_NVLINK_BANDWIDTH_TOTAL"),   # MB/s (gauge)
    # Profiling metrics (ratio 0-1)
    (1001, "DCGM_FI_PROF_GR_ENGINE_ACTIVE"),
    (1002, "DCGM_FI_PROF_SM_ACTIVE"),
    (1003, "DCGM_FI_PROF_SM_OCCUPANCY"),
    (1004, "DCGM_FI_PROF_PIPE_TENSOR_ACTIVE"),
    (1005, "DCGM_FI_PROF_DRAM_ACTIVE"),
    (1006, "DCGM_FI_PROF_PIPE_FP64_ACTIVE"),
    (1007, "DCGM_FI_PROF_PIPE_FP32_ACTIVE"),
    (1008, "DCGM_FI_PROF_PIPE_FP16_ACTIVE"),
    (1009, "DCGM_FI_PROF_PCIE_TX_BYTES"),           # bytes/sec
    (1010, "DCGM_FI_PROF_PCIE_RX_BYTES"),           # bytes/sec
    (1011, "DCGM_FI_PROF_NVLINK_TX_BYTES"),         # bytes/sec
    (1012, "DCGM_FI_PROF_NVLINK_RX_BYTES"),         # bytes/sec
)

DCGM_DMON_FIELD_IDS = ",".join(str(fid) for fid, _ in DCGM_DMON_FIELDS)
DCGM_DMON_METRIC_NAMES = [name for _, name in DCGM_DMON_FIELDS]

# DCGM profiling counters (DCGM_FI_PROF_*) refresh at ~10 Hz through the shared
# hardware-counter multiplexer. Below ~100 ms, ``dcgmi dmon`` returns mostly
# N/A profiling rows, so the streaming interval is clamped to this floor.
DCGM_STREAM_MIN_INTERVAL_MS = 100


def resolve_dcgm_gpu_ids(discovery_stdout: str) -> Tuple[List[str], Dict[str, str]]:
    """Resolve the physical GPU IDs visible to this process via dcgmi discovery.

    Inside a SLURM cgroup, ``CUDA_VISIBLE_DEVICES`` is remapped to ``0``, but
    ``dcgmi`` operates outside the cgroup and uses physical GPU indices. The two
    are reconciled by matching on GPU UUID.

    Args:
        discovery_stdout: stdout from ``dcgmi discovery -l``.

    Returns:
        ``(physical_ids, physical_to_local_map)``. ``physical_to_local_map``
        maps each physical GPU ID to its local (cgroup) GPU ID, so that exports
        can match dcgmi GPU IDs against ``nvidia-smi`` process IDs.
    """
    try:
        nvsmi = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,uuid", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5,
        )
        if nvsmi.returncode != 0:
            return [], {}
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return [], {}

    local_uuids: Dict[str, str] = {}  # uuid -> local_index
    for line in nvsmi.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",", 1)]
        if len(parts) == 2:
            local_uuids[parts[1]] = parts[0]

    if not local_uuids:
        return [], {}

    # Parse dcgmi discovery output to map UUID -> physical GPU ID.
    # Format: "| 3      | Name: ...  \n|        | Device UUID: GPU-xxxx..."
    physical_ids: List[str] = []
    phys_to_local: Dict[str, str] = {}
    current_gpu_id: Optional[str] = None
    for line in discovery_stdout.splitlines():
        m_id = re.search(r"^\|\s*(\d+)\s*\|", line)
        if m_id:
            current_gpu_id = m_id.group(1)
        m_uuid = re.search(r"UUID:\s*(GPU-[0-9a-fA-F-]+)", line)
        if m_uuid and current_gpu_id is not None:
            if m_uuid.group(1) in local_uuids:
                physical_ids.append(current_gpu_id)
                phys_to_local[current_gpu_id] = local_uuids[m_uuid.group(1)]
            current_gpu_id = None

    if not physical_ids:
        ids = list(local_uuids.values())
        return ids, {i: i for i in ids}
    return physical_ids, phys_to_local


def _parse_value(token: str) -> Optional[float]:
    """Deserialize one dcgmi token to a float, or ``None`` if not a usable value.

    ``"N/A"``, non-numeric tokens, and non-finite values all map to ``None`` — a
    reading the source could not provide, never silently coerced to ``0``.
    """
    if token == "N/A":
        return None
    try:
        value = float(token)
    except ValueError:
        return None
    if math.isinf(value) or math.isnan(value):
        return None
    return value


def parse_dmon_block(
    text: str,
    *,
    source_version: str = "dcgmi",
    timestamp: Optional[float] = None,
    wallclock: Optional[float] = None,
) -> List[RawRecord]:
    """Parse ``dcgmi dmon`` rows into ``RawRecord`` objects, one per ``GPU <id>`` row.

    Each record's ``fields`` map DCGM field names to raw values (``None`` for
    ``N/A``). Header (``#``), ``ID``, and non-data lines are skipped. Every
    record produced from one call shares the same ``timestamp``/``wallclock``
    (they belong to one sampling tick).

    Layout (columns follow ``DCGM_DMON_FIELDS`` order)::

        #Entity   GPUTL  POWER  GTEMP  MTEMP  ...
        ID
        GPU 0     72     155.3  65     58     ...
    """
    ts = time.monotonic() if timestamp is None else timestamp
    wc = time.time() if wallclock is None else wallclock
    records: List[RawRecord] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("ID"):
            continue
        parts = line.split()
        if len(parts) < 2 or parts[0] != "GPU":
            continue
        gpu_id = parts[1]
        fields: Dict[str, Optional[float]] = {}
        for col_idx, metric_name in enumerate(DCGM_DMON_METRIC_NAMES):
            val_idx = col_idx + 2  # skip "GPU" and the id
            if val_idx >= len(parts):
                break
            fields[metric_name] = _parse_value(parts[val_idx])
        records.append(RawRecord(
            timestamp=ts,
            wallclock=wc,
            entity_id=gpu_id,
            fields=fields,
            source="dcgmi",
            source_version=source_version,
        ))
    return records


def run_dmon_once(
    gpu_ids: Optional[List[str]] = None,
    interval_ms: int = 100,
    timeout: float = 15.0,
) -> str:
    """Run one ``dcgmi dmon -c 2`` collection and return its raw stdout text.

    Two samples are requested because profiling fields (1001-1012) return
    ``N/A`` on the first sample of a cold invocation; the valid second tick lets
    a downstream last-non-``None``-wins merge recover the real values.
    """
    cmd = ["dcgmi", "dmon", "-c", "2", "-d", str(interval_ms),
           "-e", DCGM_DMON_FIELD_IDS]
    if gpu_ids:
        cmd.extend(["-i", ",".join(f"gpu:{gid}" for gid in gpu_ids)])
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise DcgmStreamError(
            f"dcgmi dmon failed (exit {result.returncode}): {result.stderr.strip()}",
            remediation="Check that the DCGM host engine is running and that "
                        "profiling is permitted for this user.",
        )
    return result.stdout


def read_once(
    gpu_ids: Optional[List[str]] = None,
    interval_ms: int = 100,
    timeout: float = 15.0,
) -> List[RawRecord]:
    """Collect a single tick from ``dcgmi dmon`` as ``RawRecord`` objects.

    Returns the records from both requested samples in order; the caller's
    last-non-``None``-wins merge keeps the valid second-tick values.
    """
    return parse_dmon_block(run_dmon_once(gpu_ids, interval_ms, timeout))


class DcgmiBackend:
    """Persistent ``dcgmi dmon -c 0`` stream emitting ``RawRecord`` objects.

    The reader is thread-free: ``stream_ticks`` is a blocking generator that
    iterates the subprocess stdout, groups rows into ticks, and yields one list
    of records per tick. ``stream`` flattens that to the per-record ``Backend``
    contract. The first tick is dropped (profiling fields are ``N/A`` on a cold
    start). Concurrency, if needed, is the caller's responsibility.
    """

    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen] = None
        self._gpu_ids: Optional[List[str]] = None
        self._poll_ms: int = DCGM_STREAM_MIN_INTERVAL_MS
        self._source_version: str = "dcgmi"
        self._closed: bool = False

    # ── lifecycle ────────────────────────────────────────────────────────

    def open(self, config: ReaderConfig) -> None:
        self._gpu_ids = list(config.gpu_ids) if config.gpu_ids else None
        self._poll_ms = max(
            DCGM_STREAM_MIN_INTERVAL_MS, int(round(config.poll_seconds * 1000))
        )
        self._closed = False
        cmd = ["dcgmi", "dmon", "-c", "0", "-d", str(self._poll_ms),
               "-e", DCGM_DMON_FIELD_IDS]
        if self._gpu_ids:
            cmd.extend(["-i", ",".join(f"gpu:{gid}" for gid in self._gpu_ids)])
        # Dropping CUDA_VISIBLE_DEVICES suppresses dcgmi's multi-line stdout
        # warning preamble; dcgmi targets the host engine, not CUDA, so the
        # variable has no effect on which GPUs it reports.
        env = {k: v for k, v in os.environ.items() if k != "CUDA_VISIBLE_DEVICES"}
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                start_new_session=True,
                env=env,
            )
        except FileNotFoundError as exc:
            raise DcgmStreamError(
                f"dcgmi not found: {exc}",
                remediation="Install NVIDIA DCGM, or use the prometheus backend.",
            ) from exc

    def close(self) -> None:
        """Terminate the subprocess. Safe to call after a failed ``open``."""
        self._closed = True
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass

    # ── streaming ────────────────────────────────────────────────────────

    def stream_ticks(self) -> Iterator[List[RawRecord]]:
        """Yield one list of ``RawRecord`` objects per sampling tick.

        A tick ends when a ``GPU <id>`` row repeats an id already buffered for
        the current tick. The first tick is dropped. Raises ``DcgmStreamError``
        if the subprocess exits non-zero (unless ``close`` was called).
        """
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        current: Dict[str, str] = {}   # gpu_id -> most-recent row (this tick)
        order: List[str] = []          # gpu_id order within the tick
        skipped_first = False
        for raw_line in proc.stdout:
            if self._closed:
                break
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#") or stripped.startswith("ID"):
                continue
            parts = stripped.split()
            if len(parts) < 2 or parts[0] != "GPU":
                continue
            gpu_id = parts[1]
            if gpu_id in current:
                # Boundary: this id already has a row -> flush the tick.
                if skipped_first:
                    yield self._make_tick(current, order)
                else:
                    skipped_first = True  # drop the cold-start tick
                current = {}
                order = []
            current[gpu_id] = stripped
            order.append(gpu_id)
        # stdout closed -> subprocess exited; surface a non-zero exit.
        rc = proc.poll()
        if rc is not None and rc != 0 and not self._closed:
            raise DcgmStreamError(
                f"dcgmi dmon exited with code {rc}: {self._read_stderr_tail()}",
                remediation="Check the DCGM host engine and profiling permissions.",
            )

    def stream(self) -> Iterator[RawRecord]:
        for tick in self.stream_ticks():
            yield from tick

    def _make_tick(self, current: Dict[str, str], order: List[str]) -> List[RawRecord]:
        block = "\n".join(current[gid] for gid in order)
        return parse_dmon_block(
            block,
            source_version=self._source_version,
            timestamp=time.monotonic(),
            wallclock=time.time(),
        )

    def _read_stderr_tail(self) -> str:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return ""
        try:
            return (proc.stderr.read() or "").strip()
        except Exception:
            return ""

    # ── introspection ──────────────────────────────────────────────────────

    @property
    def stderr(self):
        """The subprocess stderr stream (for a consumer that drains it)."""
        return self._proc.stderr if self._proc is not None else None

    @property
    def caps(self) -> BackendCaps:
        return BackendCaps(
            kind=BackendKind.DCGMI,
            fields=frozenset(DCGM_DMON_METRIC_NAMES),
        )


# Fail fast at import time if the class drifts from the Backend contract.
assert isinstance(DcgmiBackend(), Backend)
