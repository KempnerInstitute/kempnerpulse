#!/usr/bin/env python3
"""KempnerPulse – real-time GPU monitoring and dashboard for DCGM Prometheus metrics.

Single-file Rich-based TUI that streams DCGM-exporter /metrics and renders
Fleet View, Focus View, Plot View, and Job View in the terminal.
"""
from __future__ import annotations

import sys
if sys.version_info < (3, 9):
    sys.exit("KempnerPulse requires Python 3.9 or later.")

import argparse
import atexit
import csv
import grp
import math
import os
import pwd
import re
import signal
import socket
import threading
import time
import subprocess
import urllib.request
import select
import termios
import tty
from collections import defaultdict, deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Deque, Dict, Iterable, List, Optional, Set, Tuple

try:
    from rich import box
    from rich.console import Console, Group
    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
except ImportError:
    print("This program requires the 'rich' package. Install it with: pip install rich", file=sys.stderr)
    raise

PERCENT_0_100 = {
    "DCGM_FI_DEV_GPU_UTIL",
    "DCGM_FI_DEV_MEM_COPY_UTIL",
}

RATIO_0_1 = {
    "DCGM_FI_PROF_GR_ENGINE_ACTIVE",
    "DCGM_FI_PROF_SM_ACTIVE",
    "DCGM_FI_PROF_SM_OCCUPANCY",
    "DCGM_FI_PROF_PIPE_TENSOR_ACTIVE",
    "DCGM_FI_PROF_DRAM_ACTIVE",
    "DCGM_FI_PROF_PIPE_FP64_ACTIVE",
    "DCGM_FI_PROF_PIPE_FP32_ACTIVE",
    "DCGM_FI_PROF_PIPE_FP16_ACTIVE",
}

COUNTER_METRICS = {
    "DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION",
    "DCGM_FI_DEV_PCIE_REPLAY_COUNTER",
    "DCGM_FI_DEV_NVLINK_BANDWIDTH_TOTAL",
}

NVLINK_TOTAL_METRIC = "DCGM_FI_DEV_NVLINK_BANDWIDTH_TOTAL"

# ── Direct DCGM backend (dcgmi dmon) ─────────────────────────────────────────
# Maps DCGM field IDs to the metric names used throughout KempnerPulse.
# Order matters: it determines column positions in dcgmi dmon output.
DCGM_DMON_FIELDS: Tuple[Tuple[int, str], ...] = (
    # Device-level metrics
    (100,  "DCGM_FI_DEV_SM_CLOCK"),               # MHz
    (101,  "DCGM_FI_DEV_MEM_CLOCK"),              # MHz
    (140,  "DCGM_FI_DEV_MEMORY_TEMP"),             # Celsius
    (150,  "DCGM_FI_DEV_GPU_TEMP"),                # Celsius
    (155,  "DCGM_FI_DEV_POWER_USAGE"),             # Watts
    (156,  "DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION"),# millijoules (counter)
    (202,  "DCGM_FI_DEV_PCIE_REPLAY_COUNTER"),     # counter
    (203,  "DCGM_FI_DEV_GPU_UTIL"),                # 0-100%
    (204,  "DCGM_FI_DEV_MEM_COPY_UTIL"),           # 0-100%
    (251,  "DCGM_FI_DEV_FB_FREE"),                 # MiB
    (252,  "DCGM_FI_DEV_FB_USED"),                 # MiB
    (253,  "DCGM_FI_DEV_FB_RESERVED"),             # MiB
    (449,  "DCGM_FI_DEV_NVLINK_BANDWIDTH_TOTAL"),  # bytes (counter)
    # Profiling metrics (ratio 0-1)
    (1001, "DCGM_FI_PROF_GR_ENGINE_ACTIVE"),
    (1002, "DCGM_FI_PROF_SM_ACTIVE"),
    (1003, "DCGM_FI_PROF_SM_OCCUPANCY"),
    (1004, "DCGM_FI_PROF_PIPE_TENSOR_ACTIVE"),
    (1005, "DCGM_FI_PROF_DRAM_ACTIVE"),
    (1006, "DCGM_FI_PROF_PIPE_FP64_ACTIVE"),
    (1007, "DCGM_FI_PROF_PIPE_FP32_ACTIVE"),
    (1008, "DCGM_FI_PROF_PIPE_FP16_ACTIVE"),
    (1009, "DCGM_FI_PROF_PCIE_TX_BYTES"),          # bytes/sec
    (1010, "DCGM_FI_PROF_PCIE_RX_BYTES"),          # bytes/sec
)

DCGM_DMON_FIELD_IDS = ",".join(str(fid) for fid, _ in DCGM_DMON_FIELDS)
DCGM_DMON_METRIC_NAMES = [name for _, name in DCGM_DMON_FIELDS]

SPARK_BLOCKS = "▁▂▃▄▅▆▇█"
APP_NAME = "KempnerPulse GPU Dashboard"
__version__ = "0.3.0"

EXPORT_DEFAULT_COLUMNS = (
    "timestamp", "gpu_id", "model", "gpu_util_pct", "mem_used_mib",
    "real_util_pct", "sm_active_pct", "tensor_active_pct", "dram_active_pct",
)

EXPORT_CSV_COLUMNS: Tuple[Tuple[str, str], ...] = (
    # Tier A: identity + key derived
    ("timestamp", "_timestamp"),
    ("gpu_id", "_gpu_id"),
    ("model", "_model"),
    ("real_util_pct", "_real_util"),
    ("status", "_status"),
    ("health", "_health"),
    # Tier B: core profiling (Real Util components)
    ("sm_active_pct", "DCGM_FI_PROF_SM_ACTIVE"),
    ("tensor_active_pct", "DCGM_FI_PROF_PIPE_TENSOR_ACTIVE"),
    ("dram_active_pct", "DCGM_FI_PROF_DRAM_ACTIVE"),
    ("gr_engine_active_pct", "DCGM_FI_PROF_GR_ENGINE_ACTIVE"),
    ("gpu_util_pct", "DCGM_FI_DEV_GPU_UTIL"),
    # Tier C: memory/power/thermal
    ("mem_used_mib", "DCGM_FI_DEV_FB_USED"),
    ("mem_total_mib", "_mem_total_mib"),
    ("mem_used_pct", "_mem_used_pct"),
    ("power_w", "DCGM_FI_DEV_POWER_USAGE"),
    ("gpu_temp_c", "DCGM_FI_DEV_GPU_TEMP"),
    ("mem_temp_c", "DCGM_FI_DEV_MEMORY_TEMP"),
    # Tier D: detailed/secondary
    ("sm_occupancy_pct", "DCGM_FI_PROF_SM_OCCUPANCY"),
    ("fp16_pipe_pct", "DCGM_FI_PROF_PIPE_FP16_ACTIVE"),
    ("fp32_pipe_pct", "DCGM_FI_PROF_PIPE_FP32_ACTIVE"),
    ("fp64_pipe_pct", "DCGM_FI_PROF_PIPE_FP64_ACTIVE"),
    ("memcpy_util_pct", "DCGM_FI_DEV_MEM_COPY_UTIL"),
    ("pcie_rx_bytes_s", "DCGM_FI_PROF_PCIE_RX_BYTES"),
    ("pcie_tx_bytes_s", "DCGM_FI_PROF_PCIE_TX_BYTES"),
    ("nvlink_gbps", "_nvlink_gbps"),
    ("sm_clock_mhz", "DCGM_FI_DEV_SM_CLOCK"),
    ("mem_clock_mhz", "DCGM_FI_DEV_MEM_CLOCK"),
    ("pcie_replay_rate_s", "_pcie_replay_rate"),
    ("energy_j", "_energy_j"),
    ("tc_hmma_pct", "DCGM_FI_PROF_PIPE_TENSOR_HMMA_ACTIVE"),
    ("tc_imma_pct", "DCGM_FI_PROF_PIPE_TENSOR_IMMA_ACTIVE"),
    ("tc_dfma_pct", "DCGM_FI_PROF_PIPE_TENSOR_DFMA_ACTIVE"),
    ("tc_dmma_pct", "DCGM_FI_PROF_PIPE_TENSOR_DMMA_ACTIVE"),
    ("tc_qmma_pct", "DCGM_FI_PROF_PIPE_TENSOR_QMMA_ACTIVE"),
)

_RE_METRIC_LINE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)\{([^}]*)\}\s+([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)$')
_RE_BARE_LINE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)\s+([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)$')
_RE_LABEL = re.compile(r'(\w+)="((?:\\.|[^"\\])*)"')

# ── Box-drawing characters and colors for line charts ─────────────
_CH_HLINE = '─'
_CH_VLINE = '│'
_CH_ULCORNER = '┌'
_CH_URCORNER = '┐'
_CH_LLCORNER = '└'
_CH_LRCORNER = '┘'

LINE_PLOT_COLORS = [
    "green", "cyan", "yellow", "magenta", "red", "blue", "white", "bright_green",
]

GPU_TEMP_THRESHOLDS = {
    "A100": {"normal": 85, "warning": 93, "critical": 95},
    "H100": {"normal": 85, "warning": 95, "critical": 105},
    "H200": {"normal": 80, "warning": 95, "critical": 105},
    "RTX 6000": {"normal": 85, "warning": 92, "critical": 105},
}

_DEFAULT_TEMP_THRESHOLDS = {"normal": 85, "warning": 93, "critical": 105}


def _get_temp_thresholds(model_name: Optional[str] = None) -> Dict[str, int]:
    if model_name:
        upper = model_name.upper()
        for key in GPU_TEMP_THRESHOLDS:
            if key.upper() in upper:
                return GPU_TEMP_THRESHOLDS[key]
    return _DEFAULT_TEMP_THRESHOLDS


# ── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class GpuProcess:
    """A single compute process running on a GPU."""
    pid: int
    user: str
    gid: str
    gpu_id: str
    gpu_mem_mib: Optional[float]
    command: str


@dataclass
class Sample:
    ts: float
    metrics: Dict[str, Dict[str, float]]
    labels: Dict[str, Dict[str, str]]


@dataclass
class DerivedGPUState:
    gpu_id: str
    identity: Dict[str, str]
    values: Dict[str, float] = field(default_factory=dict)
    rates: Dict[str, float] = field(default_factory=dict)
    health: str = "OK"
    health_style: str = "green"
    status_line: str = "idle"
    real_util: float = 0.0
    memory_total_mib: Optional[float] = None
    memory_used_pct: Optional[float] = None
    energy_j: Optional[float] = None


class HistoryStore:
    def __init__(self, maxlen: int = 120):
        self.maxlen = maxlen
        self.data: Dict[str, Dict[str, Deque[float]]] = defaultdict(lambda: defaultdict(lambda: deque(maxlen=maxlen)))

    def push(self, gpu_id: str, metric: str, value: float) -> None:
        self.data[gpu_id][metric].append(value)

    def get(self, gpu_id: str, metric: str) -> Deque[float]:
        gpu_data = self.data.get(gpu_id)
        if gpu_data is None:
            return deque(maxlen=self.maxlen)
        return gpu_data.get(metric, deque(maxlen=self.maxlen))


def query_accessible_gpus() -> Optional[Set[str]]:
    """Query nvidia-smi for GPU indices accessible to the current user.

    nvidia-smi respects cgroup restrictions, so it only returns GPUs the
    user's process can actually access.  Returns None if nvidia-smi is
    unavailable (fall back to no filtering).
    """
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=index", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return None
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    ids: Set[str] = set()
    for line in r.stdout.strip().splitlines():
        token = line.strip()
        if token.isdigit():
            ids.add(token)
    return ids if ids else None


class GPUSelector:
    def __init__(self, explicit: Optional[str], disable_auto: bool = False,
                 accessible: Optional[Set[str]] = None):
        self.explicit = explicit
        self.disable_auto = disable_auto
        self.accessible = accessible
        self.allowed: Optional[Set[str]] = None
        self.reason: str = "all"
        self.source_value: Optional[str] = None

    def _clamp(self, ids: Optional[Set[str]]) -> Optional[Set[str]]:
        """Intersect ids with the accessible set."""
        if ids is None or self.accessible is None:
            return ids
        return ids & self.accessible

    def resolve(self) -> Tuple[Optional[Set[str]], str, Optional[str]]:
        if self.explicit:
            ids = self._clamp(self._parse_gpu_list(self.explicit))
            self.allowed = ids
            self.reason = "--gpus"
            self.source_value = self.explicit
            return self.allowed, self.reason, self.source_value

        if self.disable_auto:
            self.allowed = self._clamp(self.accessible)
            self.reason = "all"
            self.source_value = None
            return self.allowed, self.reason, self.source_value

        env_candidates = [
            "CUDA_VISIBLE_DEVICES",
            "NVIDIA_VISIBLE_DEVICES",
            "SLURM_STEP_GPUS",
            "SLURM_JOB_GPUS",
        ]
        for key in env_candidates:
            raw = os.environ.get(key, "").strip()
            if not raw:
                continue
            ids = self._clamp(self._parse_gpu_list(raw))
            if ids:
                self.allowed = ids
                self.reason = key
                self.source_value = raw
                return self.allowed, self.reason, self.source_value

        self.allowed = self._clamp(self.accessible)
        self.reason = "all"
        self.source_value = None
        return self.allowed, self.reason, self.source_value

    @staticmethod
    def _parse_gpu_list(raw: str) -> Set[str]:
        raw = raw.strip()
        if not raw or raw.lower() in {"all", "none", "void"}:
            return set()

        ids: Set[str] = set()
        for part in raw.split(","):
            token = part.strip()
            if not token:
                continue

            bracket = re.match(r"^[^\[]*\[(.+)\]$", token)
            if bracket:
                ids |= GPUSelector._expand_ranges(bracket.group(1))
                continue

            if re.fullmatch(r"\d+(?:-\d+)?", token):
                ids |= GPUSelector._expand_ranges(token)
                continue

            suffix_num = re.search(r"(?:^|[:/])(?:gpu)?(\d+)$", token, flags=re.IGNORECASE)
            if suffix_num:
                ids.add(suffix_num.group(1))
                continue

            embedded_nums = re.findall(r"\d+", token)
            if embedded_nums and token.lower().startswith("gpu"):
                ids.add(embedded_nums[-1])
                continue

        return ids

    @staticmethod
    def _expand_ranges(raw: str) -> Set[str]:
        out: Set[str] = set()
        for chunk in raw.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            m = re.fullmatch(r"(\d+)-(\d+)", chunk)
            if m:
                start, end = int(m.group(1)), int(m.group(2))
                low, high = min(start, end), max(start, end)
                out |= {str(i) for i in range(low, high + 1)}
            elif chunk.isdigit():
                out.add(chunk)
        return out


class CommandController:
    def __init__(self, initial_focus: Optional[str]):
        self.focus_gpu: Optional[str] = initial_focus
        self.line_mode = False
        self.jobs_mode = False
        self.command_mode = False
        self.buffer = ""
        self.should_exit = False
        self.last_message = ""

    def hint(self) -> str:
        if self.command_mode:
            return f":{self.buffer}"
        return "Type :focus <gpu>, :plot, :job, :q, or :exit"

    def handle_input(self, available_gpu_ids: Set[str]) -> None:
        if not sys.stdin.isatty():
            return
        while True:
            rlist, _, _ = select.select([sys.stdin], [], [], 0)
            if not rlist:
                break
            ch = sys.stdin.read(1)
            if not ch:
                break
            self._process_char(ch, available_gpu_ids)

    def _process_char(self, ch: str, available_gpu_ids: Set[str]) -> None:
        if not self.command_mode:
            if ch == ":":
                self.command_mode = True
                self.buffer = ""
                self.last_message = ""
            return

        if ch in ("\r", "\n"):
            self._execute_command(self.buffer.strip(), available_gpu_ids)
            self.command_mode = False
            self.buffer = ""
            return
        if ch in ("\x1b",):
            self.command_mode = False
            self.buffer = ""
            self.last_message = ""
            return
        if ch in ("\x7f", "\b"):
            self.buffer = self.buffer[:-1]
            return
        if ch == "\x03":
            self.should_exit = True
            return
        if ch.isprintable():
            self.buffer += ch

    def _execute_command(self, cmd: str, available_gpu_ids: Set[str]) -> None:
        if not cmd:
            return
        lower = cmd.lower()
        if lower in {"q", "quit"}:
            if self.line_mode:
                self.line_mode = False
                self.last_message = "Returned to fleet view"
            elif self.jobs_mode:
                self.jobs_mode = False
                self.last_message = "Returned to fleet view"
            elif self.focus_gpu is not None:
                self.focus_gpu = None
                self.last_message = "Returned to fleet view"
            else:
                self.should_exit = True
            return
        if lower == "exit":
            self.should_exit = True
            return
        if lower == "plot":
            self.line_mode = True
            self.jobs_mode = False
            self.focus_gpu = None
            self.last_message = "Plot view"
            return
        if lower == "job":
            self.jobs_mode = True
            self.line_mode = False
            self.focus_gpu = None
            self.last_message = "Job view"
            return
        if lower.startswith("focus"):
            parts = cmd.split()
            if len(parts) != 2:
                self.last_message = "Usage: :focus <gpu_id>"
                return
            gpu_id = parts[1]
            if gpu_id not in available_gpu_ids:
                self.last_message = f"GPU {gpu_id} is not visible"
                return
            self.line_mode = False
            self.jobs_mode = False
            self.focus_gpu = gpu_id
            self.last_message = f"Focused GPU {gpu_id}"
            return
        self.last_message = f"Unknown command: {cmd}"


# ── Terminal input ───────────────────────────────────────────────────────────

@contextmanager
def cbreak_stdin(enabled: bool):
    if not enabled or not sys.stdin.isatty():
        yield
        return
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


# ── Prometheus parsing & data loading ────────────────────────────────────────

def parse_prometheus_text(text: str) -> Sample:
    metrics: Dict[str, Dict[str, float]] = defaultdict(dict)
    labels: Dict[str, Dict[str, str]] = defaultdict(dict)

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _RE_METRIC_LINE.match(line)
        if m:
            metric, label_blob, value_s = m.groups()
            pairs = {k: v.replace('\\"', '"') for k, v in _RE_LABEL.findall(label_blob)}
            gpu_id = pairs.get("gpu") or pairs.get("UUID") or pairs.get("device")
            if gpu_id is None:
                continue
            try:
                value = float(value_s)
            except ValueError:
                continue
            if math.isinf(value) or math.isnan(value):
                continue
            metrics[gpu_id][metric] = value
            labels[gpu_id].update(pairs)
            continue

        m2 = _RE_BARE_LINE.match(line)
        if m2:
            metric, value_s = m2.groups()
            try:
                value = float(value_s)
            except ValueError:
                continue
            if math.isinf(value) or math.isnan(value):
                continue
            metrics["global"][metric] = value

    return Sample(ts=time.time(), metrics=dict(metrics), labels=dict(labels))


def load_source(source: str, timeout: float = 5.0) -> str:
    if source.startswith("http://") or source.startswith("https://"):
        with urllib.request.urlopen(source, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    with open(source, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


# ── Direct DCGM backend (dcgmi dmon) ─────────────────────────────────────────

def _resolve_dcgm_gpu_ids(discovery_stdout: str) -> Tuple[List[str], Dict[str, str]]:
    """Resolve physical GPU IDs visible to this process via dcgmi discovery.

    Inside a SLURM cgroup, CUDA_VISIBLE_DEVICES is remapped to 0, but dcgmi
    operates outside the cgroup and uses physical GPU indices.  We match on
    GPU UUID to find the correct physical ID(s).

    Args:
        discovery_stdout: stdout from ``dcgmi discovery -l``.

    Returns (physical_ids, physical_to_local_map).
    physical_to_local_map maps physical GPU ID -> local (cgroup) GPU ID,
    needed so that --export can match dcgmi GPU IDs against nvidia-smi process IDs.
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

    # Parse dcgmi discovery output to map UUID -> physical GPU ID
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


def load_dcgm_direct(gpu_ids: Optional[List[str]] = None,
                     interval_ms: int = 100) -> str:
    """Collect a sample from dcgmi dmon (direct DCGM query, no Prometheus).

    Requests 2 samples because profiling fields (1001-1010) return N/A on the
    first sample of each invocation (warmup).  The parser keeps the last value
    per GPU, so the valid second sample overwrites the N/A first.

    Args:
        gpu_ids: Physical GPU IDs to monitor.  If None, monitors all GPUs.
        interval_ms: Sampling interval in milliseconds (default 100).

    Returns:
        Raw dcgmi dmon stdout text.
    """
    cmd = ["dcgmi", "dmon", "-c", "2", "-d", str(interval_ms),
           "-e", DCGM_DMON_FIELD_IDS]
    if gpu_ids:
        cmd.extend(["-i", ",".join(f"gpu:{gid}" for gid in gpu_ids)])

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    if result.returncode != 0:
        raise RuntimeError(f"dcgmi dmon failed (exit {result.returncode}): {result.stderr.strip()}")
    return result.stdout


def parse_dcgm_dmon(text: str, gpu_models: Optional[Dict[str, str]] = None) -> Sample:
    """Parse dcgmi dmon output into a Sample (same format as Prometheus parser).

    dcgmi dmon output format (columns match DCGM_DMON_FIELDS order):
        #Entity   GPUTL  POWER  GTEMP  MTEMP  ...
        ID
        GPU 0     72     155.3  65     58     ...

    Values marked N/A or that fail float conversion are skipped.
    """
    metrics: Dict[str, Dict[str, float]] = defaultdict(dict)
    labels: Dict[str, Dict[str, str]] = defaultdict(dict)

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("ID"):
            continue
        parts = line.split()
        if len(parts) < 2 or parts[0] != "GPU":
            continue

        gpu_id = parts[1]
        # Remaining columns correspond 1:1 to DCGM_DMON_METRIC_NAMES
        for col_idx, metric_name in enumerate(DCGM_DMON_METRIC_NAMES):
            val_idx = col_idx + 2  # skip "GPU" and gpu_id
            if val_idx >= len(parts):
                break
            raw = parts[val_idx]
            if raw == "N/A":
                continue
            try:
                value = float(raw)
            except ValueError:
                continue
            if math.isinf(value) or math.isnan(value):
                continue
            metrics[gpu_id][metric_name] = value

        # Populate labels for compatibility with the rest of the pipeline
        labels[gpu_id]["gpu"] = gpu_id
        if gpu_models and gpu_id in gpu_models:
            labels[gpu_id]["modelName"] = gpu_models[gpu_id]

    return Sample(ts=time.time(), metrics=dict(metrics), labels=dict(labels))


# ── Streaming DCGM reader ────────────────────────────────────────────────────
#
# One persistent ``dcgmi dmon -c 0 -d <poll_ms>`` subprocess feeds both the TUI
# and the CSV export writer. A reader thread parses each tick block into a
# ``Sample`` and publishes the latest pair (current + previous) under a
# condition variable so consumers can either poll (TUI) or block until a new
# sample arrives (export).
#
# Tick boundary detection (empirically verified on H200):
#   * dcgmi emits no blank lines between ticks.
#   * The ``#Entity`` header is printed once at start and again every ~15 data
#     rows. Header and ``ID`` lines are ignored.
#   * A tick ends when we see a ``GPU <id>`` whose ``<id>`` is already present
#     in the current buffer — so we flush the buffer and start a new one with
#     the repeating line.
#   * The first tick is dropped: profiling fields return N/A on the very first
#     sample of a cold dcgmi process.

DCGM_STREAM_MIN_INTERVAL_MS = 100   # DCGM profiling refresh floor (see probe notes)


def fmt_duration(seconds: float, *, signed: bool = False) -> str:
    """Compact duration label that picks units to keep the number readable.

    Used by the footer's ``poll=`` indicator and the line-plot x-axis so that
    sub-second values do not all collapse to ``0s``.

    Examples:
        fmt_duration(1.0)    -> "1s"
        fmt_duration(0.5)    -> "500ms"
        fmt_duration(0.05)   -> "50ms"
        fmt_duration(0.001)  -> "1ms"
        fmt_duration(0.0)    -> "0s"
        fmt_duration(-2.5, signed=True) -> "-2.5s"
    """
    if seconds == 0:
        return "0s"
    sign = "-" if (signed and seconds < 0) else ""
    val = abs(seconds)
    if val >= 1.0:
        # 1.5s, 30s, 600s — drop the decimal once we're past 10s.
        if val >= 10:
            return f"{sign}{val:.0f}s"
        return f"{sign}{val:.1f}s".replace(".0s", "s")
    ms = val * 1000.0
    if ms >= 1.0:
        if ms >= 10:
            return f"{sign}{ms:.0f}ms"
        return f"{sign}{ms:.1f}ms".replace(".0ms", "ms")
    # Sub-millisecond: round up to 1ms rather than printing "0ms".
    return f"{sign}1ms"


class DcgmStreamError(RuntimeError):
    """Raised when the dcgmi streaming subprocess fails or exits unexpectedly."""


class DcgmStreamReader:
    """Background reader around a persistent ``dcgmi dmon -c 0`` subprocess."""

    def __init__(self,
                 gpu_ids: Optional[List[str]],
                 poll_ms: int,
                 gpu_models: Optional[Dict[str, str]] = None):
        self._gpu_ids = gpu_ids
        self._poll_ms = max(DCGM_STREAM_MIN_INTERVAL_MS, int(poll_ms))
        self._gpu_models = gpu_models or {}
        self._proc: Optional[subprocess.Popen] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._latest: Optional[Sample] = None
        self._prev: Optional[Sample] = None
        self._counter: int = 0
        self._skipped_first: bool = False
        self._error: Optional[BaseException] = None
        self._started = False

    # ── lifecycle ───────────────────────────────────────────────────────

    def start(self) -> None:
        if self._started:
            return
        cmd = ["dcgmi", "dmon", "-c", "0", "-d", str(self._poll_ms),
               "-e", DCGM_DMON_FIELD_IDS]
        if self._gpu_ids:
            cmd.extend(["-i", ",".join(f"gpu:{gid}" for gid in self._gpu_ids)])
        # Stripping CUDA_VISIBLE_DEVICES suppresses dcgmi's multi-line stdout
        # warning preamble; dcgmi targets the hostengine, not CUDA, so the
        # variable has no functional effect on which GPUs it reports.
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
            raise DcgmStreamError(f"dcgmi not found: {exc}") from exc

        self._reader_thread = threading.Thread(
            target=self._read_stdout, name="dcgm-stream", daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, name="dcgm-stream-err", daemon=True,
        )
        self._reader_thread.start()
        self._stderr_thread.start()
        self._started = True

    def stop(self) -> None:
        if not self._started:
            return
        self._stop.set()
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
        # Wake any blocked consumers
        with self._cond:
            self._cond.notify_all()
        for t in (self._reader_thread, self._stderr_thread):
            if t is not None and t.is_alive():
                t.join(timeout=1.0)
        self._started = False

    # ── consumer APIs ───────────────────────────────────────────────────

    def get_pair(self) -> Tuple[Optional[Sample], Optional[Sample]]:
        """Non-blocking snapshot of (latest, prev). Used by the TUI."""
        with self._cond:
            if self._error is not None and self._latest is None:
                raise DcgmStreamError(str(self._error))
            return self._latest, self._prev

    def last_counter(self) -> int:
        """Current value of the sample counter (for pairing with wait_for_new)."""
        with self._cond:
            return self._counter

    def wait_for_new(self,
                     last_counter: int,
                     timeout: float = 2.0,
                     ) -> Tuple[Optional[Sample], Optional[Sample], int]:
        """Block until the sample counter advances past ``last_counter``.

        Returns (latest, prev, new_counter). If the reader has stopped or
        errored before a new sample arrives, returns (None, None, last_counter).
        """
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._counter <= last_counter and not self._stop.is_set() and self._error is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(timeout=remaining)
            if self._error is not None and self._latest is None:
                raise DcgmStreamError(str(self._error))
            if self._stop.is_set() and self._counter <= last_counter:
                return None, None, last_counter
            return self._latest, self._prev, self._counter

    def wait_first_sample(self, timeout: float = 5.0) -> bool:
        """Block until the first valid sample is published. Returns True on success."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._counter == 0 and not self._stop.is_set() and self._error is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(timeout=remaining)
            if self._error is not None:
                raise DcgmStreamError(str(self._error))
            return self._counter > 0

    # ── internal: stdout reader ─────────────────────────────────────────

    def _read_stdout(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        current: Dict[str, List[str]] = {}  # gpu_id -> full line (most recent wins)
        order: List[str] = []                # gpu_id order in current tick

        try:
            for raw_line in proc.stdout:
                if self._stop.is_set():
                    break
                line = raw_line.rstrip("\n")
                stripped = line.strip()
                if not stripped:
                    continue
                # Skip header lines and any dcgmi preamble that isn't a GPU data row
                if stripped.startswith("#") or stripped.startswith("ID"):
                    continue
                parts = stripped.split()
                if len(parts) < 2 or parts[0] != "GPU":
                    continue
                gpu_id = parts[1]
                if gpu_id in current:
                    # Boundary: this id already has a row → flush and start new tick
                    self._publish(current, order)
                    current = {}
                    order = []
                current[gpu_id] = line
                order.append(gpu_id)
            # stdout closed → subprocess exited
            rc = proc.poll()
            if rc is not None and rc != 0 and not self._stop.is_set():
                stderr_tail = ""
                if proc.stderr is not None:
                    try:
                        stderr_tail = proc.stderr.read() or ""
                    except Exception:
                        pass
                self._set_error(DcgmStreamError(
                    f"dcgmi dmon exited with code {rc}: {stderr_tail.strip()}"
                ))
        except Exception as exc:
            self._set_error(exc)
        finally:
            # Wake any blocked consumers on shutdown/error
            with self._cond:
                self._cond.notify_all()

    def _publish(self, current: Dict[str, List[str]], order: List[str]) -> None:
        if not current:
            return
        # Reassemble block text in original order, then parse once
        block_text = "\n".join(current[gid] for gid in order)
        sample = parse_dcgm_dmon(block_text, self._gpu_models)
        if not self._skipped_first:
            # Drop the first tick — profiling fields often N/A on cold dcgmi start
            self._skipped_first = True
            return
        with self._cond:
            self._prev = self._latest
            self._latest = sample
            self._counter += 1
            self._cond.notify_all()

    # ── internal: stderr drain ──────────────────────────────────────────

    def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for line in proc.stderr:
                if self._stop.is_set():
                    break
                if line.strip():
                    sys.stderr.write(f"[dcgmi] {line}")
                    sys.stderr.flush()
        except Exception:
            pass

    # ── internal: error ─────────────────────────────────────────────────

    def _set_error(self, exc: BaseException) -> None:
        with self._cond:
            if self._error is None:
                self._error = exc
            self._cond.notify_all()


class OwnershipCache:
    """Caches the result of ``query_gpu_processes`` with a short TTL.

    Used by the export loop so that nvidia-smi is not spawned every tick when
    ``--poll`` is small. Process ownership rarely changes at sub-second scales.
    """

    def __init__(self, bus_id_map: Dict[str, str], ttl: float = 2.0):
        self._bus_id_map = bus_id_map
        self._ttl = ttl
        self._last_refresh: float = 0.0
        self._cached: Dict[str, List["GpuProcess"]] = {}

    def get(self, now: Optional[float] = None) -> Dict[str, List["GpuProcess"]]:
        t = now if now is not None else time.time()
        if t - self._last_refresh >= self._ttl or not self._cached:
            self._cached = query_gpu_processes(self._bus_id_map)
            self._last_refresh = t
        return self._cached


# ── nvidia-smi hardware queries ──────────────────────────────────────────────

def query_power_limits() -> Dict[str, float]:
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,power.max_limit", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return {}
        limits: Dict[str, float] = {}
        for line in result.stdout.strip().splitlines():
            parts = line.split(",")
            if len(parts) == 2:
                gpu_id = parts[0].strip()
                try:
                    limits[gpu_id] = float(parts[1].strip())
                except ValueError:
                    continue
        return limits
    except (OSError, subprocess.TimeoutExpired):
        return {}


def query_gpu_models() -> Dict[str, str]:
    try:
        result = subprocess.run(
            ["nvidia-smi", "-L"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return {}
        models: Dict[str, str] = {}
        for line in result.stdout.strip().splitlines():
            m = re.match(r"^GPU\s+(\d+):\s+(.+?)\s*\(UUID:", line)
            if m:
                models[m.group(1)] = m.group(2)
        return models
    except (OSError, subprocess.TimeoutExpired):
        return {}


def query_bus_id_mapping() -> Dict[str, str]:
    """Query nvidia-smi for PCI bus-id to GPU index mapping (static hardware info)."""
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,pci.bus_id", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return {}
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return {}
    mapping: Dict[str, str] = {}
    for line in r.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 2:
            mapping[parts[1].upper()] = parts[0]
    return mapping


def resolve_dcgm_mapping(source: str) -> Tuple[Optional[Set[str]], Dict[str, str]]:
    """Resolve physical GPU indices by matching nvidia-smi bus IDs against dcgm-exporter.

    Inside a SLURM cgroup, nvidia-smi sees remapped indices (e.g. GPU 0) but
    the same PCI bus IDs as the physical hardware.  dcgm-exporter runs outside
    the cgroup and reports physical indices.  Matching on bus ID bridges the gap.

    Returns (accessible_physical_ids, bus_id_to_physical_gpu_id).
    accessible_physical_ids is None if resolution fails.
    """
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=pci.bus_id", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return None, {}
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None, {}
    local_bus_ids: Set[str] = set()
    for line in r.stdout.strip().splitlines():
        bid = line.strip().upper()
        if bid:
            local_bus_ids.add(bid)
    if not local_bus_ids:
        return None, {}
    try:
        raw = load_source(source)
    except Exception:
        return None, {}
    sample = parse_prometheus_text(raw)
    accessible_ids: Set[str] = set()
    bus_to_physical: Dict[str, str] = {}
    for gpu_id, labels in sample.labels.items():
        bus_id = labels.get("pci_bus_id", "").upper()
        if bus_id:
            bus_to_physical[bus_id] = gpu_id
            if bus_id in local_bus_ids:
                accessible_ids.add(gpu_id)
    return accessible_ids, bus_to_physical


def query_gpu_processes(bus_to_idx: Dict[str, str]) -> Dict[str, List[GpuProcess]]:
    """Query running GPU compute processes via nvidia-smi.

    Uses --query-compute-apps to list processes (instant, no sampling delay).
    Requires a pre-built bus_to_idx mapping from query_bus_id_mapping().

    For each PID, user/group are resolved from /proc and the full command line
    is read from /proc/<pid>/cmdline.

    Returns {gpu_index_str: [GpuProcess, ...]}.
    """
    if not bus_to_idx:
        return {}
    # Compute processes
    try:
        r2 = subprocess.run(
            ["nvidia-smi",
             "--query-compute-apps=gpu_bus_id,pid,used_gpu_memory,process_name",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if r2.returncode != 0:
            return {}
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return {}

    procs: Dict[str, List[GpuProcess]] = defaultdict(list)
    for line in r2.stdout.strip().splitlines():
        if not line.strip():
            continue
        parts = [p.strip() for p in line.split(",", 3)]
        if len(parts) < 3:
            continue
        bus_id = parts[0].upper()
        try:
            pid = int(parts[1])
        except ValueError:
            continue
        try:
            mem_mib: Optional[float] = float(parts[2])
        except ValueError:
            mem_mib = None
        proc_name = parts[3].strip() if len(parts) > 3 else "?"

        gpu_id = bus_to_idx.get(bus_id, "?")

        # Resolve user / group from /proc
        user, group = "?", "?"
        try:
            st = os.stat(f"/proc/{pid}")
            try:
                user = pwd.getpwuid(st.st_uid).pw_name
            except KeyError:
                user = str(st.st_uid)
            try:
                group = grp.getgrgid(st.st_gid).gr_name
            except KeyError:
                group = str(st.st_gid)
        except (FileNotFoundError, PermissionError, OSError):
            pass

        # Full command line
        cmd = proc_name
        try:
            with open(f"/proc/{pid}/cmdline", "r") as f:
                raw = f.read().replace("\x00", " ").strip()
                if raw:
                    cmd = raw
        except (FileNotFoundError, PermissionError, OSError):
            pass

        procs[gpu_id].append(GpuProcess(
            pid=pid, user=user, gid=group,
            gpu_id=gpu_id, gpu_mem_mib=mem_mib,
            command=cmd,
        ))
    return dict(procs)


# PCIe per-lane rates in bytes/sec for each generation
_PCIE_GEN_LANE_RATE: Dict[int, float] = {
    1: 250e6,       # 2.5 GT/s * 8b/10b
    2: 500e6,       # 5   GT/s * 8b/10b
    3: 984.6e6,     # 8   GT/s * 128b/130b
    4: 1969.2e6,    # 16  GT/s * 128b/130b
    5: 3938.5e6,    # 32  GT/s * 128b/130b
    6: 7563.0e6,    # 64  GT/s * 242b/256b
}


def query_pcie_bandwidth() -> Tuple[Dict[str, float], str]:
    """Query nvidia-smi for PCIe gen/width and compute max bidirectional bandwidth per GPU.

    Returns (limits, info_string):
      limits:      {gpu_id: max_bytes_per_sec_bidirectional}
      info_string: e.g. 'Gen5 x16  63.0 GB/s'
    """
    try:
        result = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=index,pcie.link.gen.max,pcie.link.width.max",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return {}, ""
        limits: Dict[str, float] = {}
        gens: List[int] = []
        widths: List[int] = []
        for line in result.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) != 3:
                continue
            gpu_id = parts[0]
            try:
                gen = int(parts[1])
                width = int(parts[2])
            except ValueError:
                continue
            lane_rate = _PCIE_GEN_LANE_RATE.get(gen, 0.0)
            bw = lane_rate * width * 2  # bidirectional
            limits[gpu_id] = bw
            gens.append(gen)
            widths.append(width)
        if gens:
            gen_v = max(gens)
            width_v = max(widths)
            max_bw = max(limits.values()) if limits else 0.0
            info = f"Gen{gen_v} x{width_v}  {max_bw / 1e9:.1f} GB/s bidir"
        else:
            info = ""
        return limits, info
    except (OSError, subprocess.TimeoutExpired):
        return {}, ""


def query_nvlink_bandwidth() -> Dict[str, float]:
    """Query nvidia-smi nvlink -s to get per-GPU aggregate NVLink bandwidth in GB/s.

    Returns {gpu_id: total_gbps} where total_gbps is 2× the sum of all link speeds
    (each link is full-duplex, and DCGM_FI_DEV_NVLINK_BANDWIDTH_TOTAL counts TX+RX).
    """
    try:
        result = subprocess.run(
            ["nvidia-smi", "nvlink", "-s"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return {}
        limits: Dict[str, float] = {}
        current_gpu: Optional[str] = None
        total: float = 0.0
        for line in result.stdout.splitlines():
            m = re.match(r"^GPU\s+(\d+):", line)
            if m:
                if current_gpu is not None:
                    limits[current_gpu] = total * 2
                current_gpu = m.group(1)
                total = 0.0
                continue
            m2 = re.match(r"^\s+Link\s+\d+:\s+([\d.]+)\s+GB/s", line)
            if m2 and current_gpu is not None:
                total += float(m2.group(1))
        if current_gpu is not None:
            limits[current_gpu] = total * 2
        return limits
    except (OSError, subprocess.TimeoutExpired):
        return {}


# ── Metric math & formatting ─────────────────────────────────────────────────

def rate(prev_value: Optional[float], prev_ts: Optional[float], cur_value: float, cur_ts: float) -> Optional[float]:
    if prev_value is None or prev_ts is None or cur_ts <= prev_ts:
        return None
    delta = cur_value - prev_value
    if delta < 0:
        return None
    return delta / (cur_ts - prev_ts)


def to_percent(metric: str, value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    if metric in PERCENT_0_100:
        return max(0.0, min(100.0, value))
    if metric in RATIO_0_1:
        return max(0.0, min(100.0, value * 100.0))
    return value


def fmt_pct(v: Optional[float], digits: int = 0) -> str:
    if v is None or math.isnan(v):
        return "--"
    return f"{v:.{digits}f}%"


def fmt_num(v: Optional[float], digits: int = 1) -> str:
    if v is None or math.isnan(v):
        return "--"
    return f"{v:.{digits}f}"


def fmt_temp(v: Optional[float]) -> str:
    if v is None:
        return "--"
    return f"{v:.0f}°C"


def fmt_watts(v: Optional[float]) -> str:
    if v is None:
        return "--"
    return f"{v:.0f}W"


def fmt_mhz(v: Optional[float]) -> str:
    if v is None:
        return "--"
    return f"{v:.0f}MHz"


def fmt_mib(v: Optional[float]) -> str:
    if v is None:
        return "--"
    if v >= 1024:
        return f"{v / 1024:.1f}GiB"
    return f"{v:.0f}MiB"


def fmt_bytes_per_s(v: Optional[float]) -> str:
    if v is None:
        return "--"
    units = ["B/s", "KiB/s", "MiB/s", "GiB/s", "TiB/s"]
    x = float(v)
    idx = 0
    while abs(x) >= 1024 and idx < len(units) - 1:
        x /= 1024
        idx += 1
    return f"{x:.1f}{units[idx]}"


def bytes_per_s_to_gbps(v: Optional[float]) -> Optional[float]:
    if v is None:
        return None
    return float(v) / 1e9


def fmt_gbps(v: Optional[float], digits: int = 2) -> str:
    if v is None or math.isnan(v):
        return "--"
    return f"{v:.{digits}f}GB/s"


def fmt_joules(v: Optional[float]) -> str:
    if v is None:
        return "--"
    if v > 1000:
        return f"{v/1000:.1f}kJ"
    return f"{v:.0f}J"


# ── Style functions ──────────────────────────────────────────────────────────

def usage_style(p: Optional[float]) -> str:
    if p is None:
        return "dim"
    if p >= 90:
        return "bold red"
    if p >= 75:
        return "bold yellow"
    if p >= 40:
        return "bold green"
    if p >= 10:
        return "cyan"
    return "dim"


def temp_style(t: Optional[float], model_name: Optional[str] = None) -> str:
    if t is None:
        return "dim"
    th = _get_temp_thresholds(model_name)
    if t >= th["critical"]:
        return "bold red"
    if t >= th["warning"]:
        return "bold yellow"
    if t >= th["normal"]:
        return "green"
    return "cyan"


def power_style(w: Optional[float]) -> str:
    if w is None:
        return "dim"
    if w >= 600:
        return "bold red"
    if w >= 450:
        return "bold yellow"
    if w >= 200:
        return "green"
    return "cyan"


def io_rate_style_gbps(v: Optional[float]) -> str:
    if v is None:
        return "dim"
    if v >= 200:
        return "bold red"
    if v >= 100:
        return "bold yellow"
    if v >= 25:
        return "bold green"
    if v > 0:
        return "cyan"
    return "dim"


def nvlink_util_style(gbps: Optional[float], limit_gbps: Optional[float]) -> str:
    """Color NVLink value by percentage of theoretical max, using usage_style thresholds."""
    if gbps is None or limit_gbps is None or limit_gbps <= 0:
        return io_rate_style_gbps(gbps)
    pct = gbps / limit_gbps * 100.0
    return usage_style(pct)


# ── Health & workload classification ─────────────────────────────────────────

def health_from_metrics(values: Dict[str, float], rates: Dict[str, float], model_name: Optional[str] = None) -> Tuple[str, str]:
    remap_fail = values.get("DCGM_FI_DEV_ROW_REMAP_FAILURE", 0)
    uncorr = values.get("DCGM_FI_DEV_UNCORRECTABLE_REMAPPED_ROWS", 0)
    replay_rate = rates.get("DCGM_FI_DEV_PCIE_REPLAY_COUNTER", 0)
    gpu_temp = values.get("DCGM_FI_DEV_GPU_TEMP")
    mem_temp = values.get("DCGM_FI_DEV_MEMORY_TEMP")
    th = _get_temp_thresholds(model_name)

    if remap_fail > 0 or uncorr > 0:
        return "CRIT", "bold red"
    if replay_rate > 0:
        return "WARN", "yellow"
    if (gpu_temp is not None and gpu_temp >= th["warning"]) or (mem_temp is not None and mem_temp >= th["warning"]):
        return "HOT", "yellow"
    return "OK", "green"


def derive_real_util(values: Dict[str, float], weights: Tuple[float, float, float, float]) -> Tuple[float, str, str]:
    """Classify GPU workload using NVIDIA DCGM profiling metric guidance.

    Thresholds based on NVIDIA documentation:
      SM_ACTIVE >= 80%  — necessary for effective GPU use
      SM_ACTIVE <  50%  — likely ineffective GPU use
      DRAM_ACTIVE       — practical peak ~80%; >= 50% is heavy memory traffic
      TENSOR_ACTIVE     — ~93% at full saturation (dcgmproftester)
    """
    w_sm, w_tensor, w_dram, w_gr = weights
    gr_raw = to_percent("DCGM_FI_PROF_GR_ENGINE_ACTIVE", values.get("DCGM_FI_PROF_GR_ENGINE_ACTIVE"))
    if gr_raw is None:
        gr_raw = to_percent("DCGM_FI_DEV_GPU_UTIL", values.get("DCGM_FI_DEV_GPU_UTIL"))
    gr_active = gr_raw or 0.0
    sm_active = to_percent("DCGM_FI_PROF_SM_ACTIVE", values.get("DCGM_FI_PROF_SM_ACTIVE")) or 0.0
    tensor = to_percent("DCGM_FI_PROF_PIPE_TENSOR_ACTIVE", values.get("DCGM_FI_PROF_PIPE_TENSOR_ACTIVE")) or 0.0
    dram = to_percent("DCGM_FI_PROF_DRAM_ACTIVE", values.get("DCGM_FI_PROF_DRAM_ACTIVE")) or 0.0
    fp64 = to_percent("DCGM_FI_PROF_PIPE_FP64_ACTIVE", values.get("DCGM_FI_PROF_PIPE_FP64_ACTIVE")) or 0.0
    memcpy = to_percent("DCGM_FI_DEV_MEM_COPY_UTIL", values.get("DCGM_FI_DEV_MEM_COPY_UTIL")) or 0.0
    pcie_rx = values.get("DCGM_FI_PROF_PCIE_RX_BYTES", 0.0) or 0.0
    pcie_tx = values.get("DCGM_FI_PROF_PCIE_TX_BYTES", 0.0) or 0.0

    real_util = w_sm * sm_active + w_tensor * tensor + w_dram * dram + w_gr * gr_active
    real_util = max(0.0, min(100.0, real_util))
    io_heavy = memcpy >= 40 or max(pcie_rx, pcie_tx) >= 1e9

    # 1. Idle — nothing running
    if real_util < 5 and gr_active < 5 and dram < 5 and not io_heavy:
        return real_util, "idle", "idle"

    # 2. Tensor-heavy compute — DL training / large-scale inference
    #    tensor ~93% at full saturation; >= 50% is clearly tensor-dominated
    if tensor >= 50 and sm_active >= 60:
        return real_util, "compute", "tensor-heavy compute"

    # 3. Tensor compute — meaningful tensor core activity
    if tensor >= 15 and sm_active >= 40:
        return real_util, "compute", "tensor compute"

    # 4. FP64 / HPC compute — double-precision scientific workload
    if fp64 >= 20 and sm_active >= 50:
        return real_util, "compute", "FP64 / HPC compute"

    # 5. I/O or data-loading — heavy PCIe/memcpy, SMs mostly idle
    if io_heavy and sm_active < 30:
        return real_util, "io", "I/O or data-loading"

    # 6. Memory-bound — high DRAM activity, SMs below effective threshold
    #    NVIDIA: DRAM practical peak ~80%; SM < 50% = likely ineffective
    if dram >= 50 and sm_active < 50:
        return real_util, "memory", "memory-bound"

    # 7. Compute-heavy — SMs well utilized (NVIDIA: >= 80% necessary for effective use)
    if sm_active >= 80:
        return real_util, "compute", "compute-heavy"

    # 8. Compute-active — moderate SM use (NVIDIA: >= 50% threshold)
    if sm_active >= 50:
        return real_util, "compute", "compute-active"

    # 9. Memory-active — significant DRAM use with some SM activity
    if dram >= 40:
        return real_util, "memory", "memory-active"

    # 10. GR engine busy but SMs underutilized — overhead, sync, small kernels
    if gr_active >= 40 and sm_active < 25:
        return real_util, "mixed", "busy, low SM use"

    # 11. Low utilization — some activity but minimal
    if gr_active < 15 and sm_active < 15 and dram < 15:
        return real_util, "mixed", "low utilization"

    return real_util, "mixed", "mixed / moderate"


# ── Sparkline & bar helpers ──────────────────────────────────────────────────

def sparkline(values: Iterable[float], width: int = 24, vmax: Optional[float] = None) -> str:
    seq = list(values)
    if not seq:
        return " " * width
    if len(seq) > width:
        out: List[float] = []
        for i in range(width):
            start = int(i * len(seq) / width)
            end = max(start + 1, int((i + 1) * len(seq) / width))
            chunk = seq[start:end]
            out.append(sum(chunk) / len(chunk))
        seq = out
    if len(seq) < width:
        seq = [seq[0]] * (width - len(seq)) + seq
    local_max = max(seq) if seq else 1.0
    if vmax is None:
        vmax = local_max if local_max > 0 else 1.0
    vmax = max(vmax, 1e-9)
    chars = []
    for v in seq:
        idx = int(round((len(SPARK_BLOCKS) - 1) * max(0.0, min(1.0, v / vmax))))
        chars.append(SPARK_BLOCKS[idx])
    return "".join(chars)


def make_bar(pct: Optional[float], width: int = 18, style_override: Optional[str] = None) -> Text:
    pct = 0.0 if pct is None else max(0.0, min(100.0, pct))
    filled = int(round(width * pct / 100.0))
    style = style_override if style_override is not None else usage_style(pct)
    t = Text()
    t.append("▓" * filled, style=style)
    t.append("░" * (width - filled), style="bright_black")
    return t


# ── GPU state construction ───────────────────────────────────────────────────

def filter_sample_to_gpu_ids(sample: Sample, allowed_gpu_ids: Optional[Set[str]]) -> Sample:
    if allowed_gpu_ids is None:
        return sample
    allowed = {str(x) for x in allowed_gpu_ids}
    metrics = {gpu_id: vals for gpu_id, vals in sample.metrics.items() if gpu_id == "global" or str(gpu_id) in allowed}
    labels = {gpu_id: vals for gpu_id, vals in sample.labels.items() if str(gpu_id) in allowed}
    return Sample(ts=sample.ts, metrics=metrics, labels=labels)


def build_gpu_states(sample: Sample, prev: Optional[Sample], weights: Tuple[float, float, float, float], gpu_models: Optional[Dict[str, str]] = None) -> List[DerivedGPUState]:
    states: List[DerivedGPUState] = []
    gpu_ids = sorted(sample.metrics.keys(), key=lambda x: (x != "global", int(x) if x.isdigit() else x))
    for gpu_id in gpu_ids:
        if gpu_id == "global":
            continue
        values = dict(sample.metrics[gpu_id])
        identity = dict(sample.labels.get(gpu_id, {}))
        if "modelName" not in identity and gpu_models and gpu_id in gpu_models:
            identity["modelName"] = gpu_models[gpu_id]
        rates: Dict[str, float] = {}
        if prev and gpu_id in prev.metrics:
            for metric in COUNTER_METRICS:
                cur = values.get(metric)
                prv = prev.metrics[gpu_id].get(metric)
                if cur is None or prv is None:
                    continue
                r = rate(prv, prev.ts, cur, sample.ts)
                if r is not None:
                    rates[metric] = r

        health, health_style = health_from_metrics(values, rates, identity.get("modelName"))
        real_util, _, status_line = derive_real_util(values, weights)

        fb_used = values.get("DCGM_FI_DEV_FB_USED")
        fb_free = values.get("DCGM_FI_DEV_FB_FREE")
        fb_reserved = values.get("DCGM_FI_DEV_FB_RESERVED")
        memory_total = None
        memory_used_pct = None
        if fb_used is not None and fb_free is not None:
            memory_total = fb_used + fb_free + (fb_reserved or 0.0)
            if memory_total > 0:
                memory_used_pct = 100.0 * fb_used / memory_total

        energy_mj = values.get("DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION")
        energy_j = energy_mj / 1000.0 if energy_mj is not None else None

        states.append(DerivedGPUState(
            gpu_id=str(gpu_id),
            identity=identity,
            values=values,
            rates=rates,
            health=health,
            health_style=health_style,
            status_line=status_line,
            real_util=real_util,
            memory_total_mib=memory_total,
            memory_used_pct=memory_used_pct,
            energy_j=energy_j,
        ))
    return states


def update_history(history: HistoryStore, states: List[DerivedGPUState]) -> None:
    for s in states:
        history.push(s.gpu_id, "real_util", s.real_util)
        gpu_util = to_percent("DCGM_FI_DEV_GPU_UTIL", s.values.get("DCGM_FI_DEV_GPU_UTIL"))
        if gpu_util is not None:
            history.push(s.gpu_id, "gpu_util", gpu_util)
        gr_active = to_percent("DCGM_FI_PROF_GR_ENGINE_ACTIVE", s.values.get("DCGM_FI_PROF_GR_ENGINE_ACTIVE"))
        if gr_active is not None:
            history.push(s.gpu_id, "gr_active", gr_active)
        sm_active = to_percent("DCGM_FI_PROF_SM_ACTIVE", s.values.get("DCGM_FI_PROF_SM_ACTIVE"))
        if sm_active is not None:
            history.push(s.gpu_id, "sm_active", sm_active)
        sm_occ = to_percent("DCGM_FI_PROF_SM_OCCUPANCY", s.values.get("DCGM_FI_PROF_SM_OCCUPANCY"))
        if sm_occ is not None:
            history.push(s.gpu_id, "sm_occupancy", sm_occ)
        tensor = to_percent("DCGM_FI_PROF_PIPE_TENSOR_ACTIVE", s.values.get("DCGM_FI_PROF_PIPE_TENSOR_ACTIVE"))
        if tensor is not None:
            history.push(s.gpu_id, "tensor", tensor)
        dram = to_percent("DCGM_FI_PROF_DRAM_ACTIVE", s.values.get("DCGM_FI_PROF_DRAM_ACTIVE"))
        if dram is not None:
            history.push(s.gpu_id, "dram", dram)
        memcpy = to_percent("DCGM_FI_DEV_MEM_COPY_UTIL", s.values.get("DCGM_FI_DEV_MEM_COPY_UTIL"))
        if memcpy is not None:
            history.push(s.gpu_id, "memcpy", memcpy)
        if s.memory_used_pct is not None:
            history.push(s.gpu_id, "mem_used_pct", s.memory_used_pct)
        power = s.values.get("DCGM_FI_DEV_POWER_USAGE")
        if power is not None:
            history.push(s.gpu_id, "power", power)
        gpu_temp = s.values.get("DCGM_FI_DEV_GPU_TEMP")
        if gpu_temp is not None:
            history.push(s.gpu_id, "gpu_temp", gpu_temp)
        pcie_rx = s.values.get("DCGM_FI_PROF_PCIE_RX_BYTES")
        pcie_tx = s.values.get("DCGM_FI_PROF_PCIE_TX_BYTES")
        if pcie_rx is not None:
            history.push(s.gpu_id, "pcie_rx", pcie_rx)
        if pcie_tx is not None:
            history.push(s.gpu_id, "pcie_tx", pcie_tx)
        if pcie_rx is not None and pcie_tx is not None:
            history.push(s.gpu_id, "pcie_rxtx", pcie_rx + pcie_tx)
        nvlink_gbps = bytes_per_s_to_gbps(s.rates.get(NVLINK_TOTAL_METRIC))
        if nvlink_gbps is not None:
            history.push(s.gpu_id, "nvlink_gbps", nvlink_gbps)
        fp64 = to_percent("DCGM_FI_PROF_PIPE_FP64_ACTIVE", s.values.get("DCGM_FI_PROF_PIPE_FP64_ACTIVE"))
        if fp64 is not None:
            history.push(s.gpu_id, "fp64", fp64)
        fp32 = to_percent("DCGM_FI_PROF_PIPE_FP32_ACTIVE", s.values.get("DCGM_FI_PROF_PIPE_FP32_ACTIVE"))
        if fp32 is not None:
            history.push(s.gpu_id, "fp32", fp32)
        fp16 = to_percent("DCGM_FI_PROF_PIPE_FP16_ACTIVE", s.values.get("DCGM_FI_PROF_PIPE_FP16_ACTIVE"))
        if fp16 is not None:
            history.push(s.gpu_id, "fp16", fp16)
        for field, key in [
            ("DCGM_FI_PROF_PIPE_TENSOR_HMMA_ACTIVE", "tc_hmma"),
            ("DCGM_FI_PROF_PIPE_TENSOR_IMMA_ACTIVE", "tc_imma"),
            ("DCGM_FI_PROF_PIPE_TENSOR_DFMA_ACTIVE", "tc_dfma"),
            ("DCGM_FI_PROF_PIPE_TENSOR_DMMA_ACTIVE", "tc_dmma"),
            ("DCGM_FI_PROF_PIPE_TENSOR_QMMA_ACTIVE", "tc_qmma"),
        ]:
            val = to_percent(field, s.values.get(field))
            if val is not None:
                history.push(s.gpu_id, key, val)


# ── CSV export helpers ────────────────────────────────────────────────────────

# Lookup: column name → extraction key
_EXPORT_COL_MAP: Dict[str, str] = {col: key for col, key in EXPORT_CSV_COLUMNS}


def _short_model_name(name: str) -> str:
    """Shorten GPU model for CSV: 'NVIDIA H100 80GB HBM3' → 'H100'."""
    s = re.sub(r'^NVIDIA\s+', '', name).strip()
    m = re.match(r'(RTX)\s*(\d+)', s)
    if m:
        return m.group(1) + m.group(2)
    parts = s.split()
    return parts[0].split('-')[0] if parts else "GPU"


def resolve_export_columns(spec: str) -> List[Tuple[str, str]]:
    """Resolve --export value to a list of (col_name, extraction_key) pairs."""
    all_names = {col for col, _ in EXPORT_CSV_COLUMNS}
    if spec == "default":
        names = list(EXPORT_DEFAULT_COLUMNS)
    elif spec == "all":
        return list(EXPORT_CSV_COLUMNS)
    else:
        names = [c.strip() for c in spec.split(",") if c.strip()]
    bad = [n for n in names if n not in all_names]
    if bad:
        print(f"kempnerpulse: unknown export column(s): {', '.join(bad)}", file=sys.stderr)
        print(f"Available: {', '.join(col for col, _ in EXPORT_CSV_COLUMNS)}", file=sys.stderr)
        sys.exit(1)
    return [(n, _EXPORT_COL_MAP[n]) for n in names]


def export_gpu_row(state: DerivedGPUState, timestamp: float,
                   columns: List[Tuple[str, str]]) -> List[str]:
    """Extract one CSV row from a DerivedGPUState for the given columns."""
    row: List[str] = []
    for _col_name, key in columns:
        if key == "_timestamp":
            row.append(f"{timestamp:.2f}")
        elif key == "_gpu_id":
            row.append(state.gpu_id)
        elif key == "_model":
            row.append(_short_model_name(state.identity.get("modelName", "")))
        elif key == "_real_util":
            row.append(f"{state.real_util:.2f}")
        elif key == "_status":
            row.append(state.status_line)
        elif key == "_health":
            row.append(state.health)
        elif key == "_mem_total_mib":
            row.append(f"{state.memory_total_mib:.1f}" if state.memory_total_mib is not None else "")
        elif key == "_mem_used_pct":
            row.append(f"{state.memory_used_pct:.2f}" if state.memory_used_pct is not None else "")
        elif key == "_nvlink_gbps":
            gbps = bytes_per_s_to_gbps(state.rates.get(NVLINK_TOTAL_METRIC))
            row.append(f"{gbps:.4f}" if gbps is not None else "")
        elif key == "_pcie_replay_rate":
            rr = state.rates.get("DCGM_FI_DEV_PCIE_REPLAY_COUNTER")
            row.append(f"{rr:.2f}" if rr is not None else "")
        elif key == "_energy_j":
            row.append(f"{state.energy_j:.1f}" if state.energy_j is not None else "")
        else:
            raw = state.values.get(key)
            if raw is None:
                row.append("")
            elif key in RATIO_0_1 or key in PERCENT_0_100:
                pct = to_percent(key, raw)
                row.append(f"{pct:.2f}" if pct is not None else "")
            else:
                row.append(f"{raw:.4f}" if isinstance(raw, float) else str(raw))
    return row


def filter_states_for_current_user(
    states: List[DerivedGPUState],
    gpu_processes: Dict[str, List[GpuProcess]],
    phys_to_local: Optional[Dict[str, str]] = None,
) -> List[DerivedGPUState]:
    """Keep only GPUs where the current user has at least one running compute process.

    phys_to_local: when using the dcgm backend inside a SLURM cgroup, state gpu_ids
    are physical (from dcgmi) while gpu_processes keys are local (from nvidia-smi).
    This map bridges the two so the filter can match correctly.
    """
    current_user = pwd.getpwuid(os.getuid()).pw_name
    user_gpu_ids: Set[str] = set()
    for gpu_id, procs in gpu_processes.items():
        for p in procs:
            if p.user == current_user:
                user_gpu_ids.add(gpu_id)
                break
    if phys_to_local:
        return [s for s in states if phys_to_local.get(s.gpu_id, s.gpu_id) in user_gpu_ids]
    return [s for s in states if s.gpu_id in user_gpu_ids]


# ── System metrics ───────────────────────────────────────────────────────────

def query_system_cpu() -> Tuple[Optional[int], Optional[int], Optional[float], Optional[int]]:
    """Return (num_threads, num_cores, cpu_percent, busy_cores) from /proc/stat.

    num_threads = os.cpu_count() (logical CPUs).
    num_cores = nproc --all (Slurm-aware total cores).
    cpu_percent = overall CPU utilization % (None on first call).
    busy_cores = number of cores with >5% utilization in the last interval.
    """
    try:
        aggregate_idle = aggregate_total = 0
        per_core: List[Tuple[int, int]] = []  # (idle, total) per core
        with open("/proc/stat", "r") as f:
            for line in f:
                if line.startswith("cpu "):
                    parts = line.split()
                    aggregate_idle = int(parts[4]) + int(parts[5])
                    aggregate_total = sum(int(x) for x in parts[1:])
                elif line.startswith("cpu"):
                    parts = line.split()
                    c_idle = int(parts[4]) + int(parts[5])
                    c_total = sum(int(x) for x in parts[1:])
                    per_core.append((c_idle, c_total))
                elif not line.startswith("cpu"):
                    break  # past all cpu lines
        if aggregate_total == 0:
            return None, None, None, None
        num_threads = os.cpu_count() or len(per_core)
        cached = getattr(query_system_cpu, "_nproc_cache", None)
        if cached is not None:
            num_cores = cached
        else:
            try:
                result = subprocess.run(["nproc", "--all"], capture_output=True, text=True, timeout=2)
                num_cores = int(result.stdout.strip()) if result.returncode == 0 else num_threads
            except (OSError, ValueError, subprocess.TimeoutExpired):
                num_cores = num_threads
            query_system_cpu._nproc_cache = num_cores
        prev = getattr(query_system_cpu, "_prev", None)
        query_system_cpu._prev = (aggregate_idle, aggregate_total, per_core)
        if prev is None:
            return num_threads, num_cores, None, None
        prev_idle, prev_total, prev_per_core = prev
        d_total = aggregate_total - prev_total
        d_idle = aggregate_idle - prev_idle
        if d_total <= 0:
            return num_threads, num_cores, None, None
        pct = 100.0 * (1.0 - d_idle / d_total)
        # Count busy cores
        busy = 0
        for i, (c_idle, c_total) in enumerate(per_core):
            if i < len(prev_per_core):
                pc_idle, pc_total = prev_per_core[i]
                dc_total = c_total - pc_total
                dc_idle = c_idle - pc_idle
                if dc_total > 0 and (1.0 - dc_idle / dc_total) > 0.05:
                    busy += 1
        return num_threads, num_cores, max(0.0, min(100.0, pct)), busy
    except (OSError, ValueError):
        return None, None, None, None


def query_system_ram() -> Tuple[Optional[float], Optional[float]]:
    """Return (used_gb, total_gb) from /proc/meminfo."""
    try:
        info: Dict[str, int] = {}
        with open("/proc/meminfo", "r") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    key = parts[0].rstrip(":")
                    info[key] = int(parts[1])  # kB
        total_kb = info.get("MemTotal", 0)
        avail_kb = info.get("MemAvailable", info.get("MemFree", 0))
        total_gb = total_kb / (1024 * 1024)
        used_gb = (total_kb - avail_kb) / (1024 * 1024)
        return used_gb, total_gb
    except (OSError, ValueError):
        return None, None


# ── Summary panel ────────────────────────────────────────────────────────────

def summary_panel(
    states: List[DerivedGPUState], source: str, poll: float, selection_desc: str,
    cpu_info: Tuple[Optional[int], Optional[int], Optional[float], Optional[int]] = (None, None, None, None),
    ram_info: Tuple[Optional[float], Optional[float]] = (None, None),
) -> Panel:
    n = len(states)
    avg_real = sum(s.real_util for s in states) / n if n else 0.0
    avg_power = sum((s.values.get("DCGM_FI_DEV_POWER_USAGE") or 0.0) for s in states) / n if n else 0.0
    total_power = sum((s.values.get("DCGM_FI_DEV_POWER_USAGE") or 0.0) for s in states)
    total_fb_used = sum((s.values.get("DCGM_FI_DEV_FB_USED") or 0.0) for s in states)
    total_fb = sum((s.memory_total_mib or 0.0) for s in states)
    mem_pct = 100.0 * total_fb_used / total_fb if total_fb > 0 else 0.0
    active = sum(1 for s in states if s.real_util >= 20 or (s.memory_used_pct or 0) >= 20)
    critical = sum(1 for s in states if s.health != "OK")
    cpu_threads, cpu_cores, cpu_pct, cpu_busy = cpu_info
    ram_used_gb, ram_total_gb = ram_info

    # Format CPU text: " 32 / 64 (50.0%)" - fixed width
    if cpu_cores is not None and cpu_busy is not None and cpu_pct is not None:
        core_w = len(str(cpu_cores))
        cpu_text = f"{cpu_busy:>{core_w}} / {cpu_cores} ({cpu_pct:5.1f}%)"
    elif cpu_cores is not None:
        core_w = len(str(cpu_cores))
        cpu_text = f"{'--':>{core_w}} / {cpu_cores} ( -- %)"
    else:
        cpu_text = "--"

    # Format RAM text: "500.0GB / 1.5TB (30.0%)" - fixed width with 1 decimal
    def _fmt_ram(gb: Optional[float]) -> str:
        if gb is None:
            return "  -- "
        if gb >= 1024:
            return f"{gb / 1024:5.1f}TB"
        return f"{gb:5.1f}GB"
    if ram_used_gb is not None and ram_total_gb is not None and ram_total_gb > 0:
        ram_pct = 100.0 * ram_used_gb / ram_total_gb
        ram_text = f"{_fmt_ram(ram_used_gb)} / {_fmt_ram(ram_total_gb)} ({ram_pct:5.1f}%)"
    else:
        ram_text = "--"
        ram_pct = 0.0

    # Format FB used text with 1 decimal, fixed width
    def _fmt_fb(mib: Optional[float]) -> str:
        if mib is None:
            return "   --  "
        if mib >= 1024:
            return f"{mib / 1024:6.1f}GiB"
        return f"{mib:6.0f}MiB"
    fb_text = f"{_fmt_fb(total_fb_used)} / {_fmt_fb(total_fb)} ({mem_pct:5.1f}%)"

    grid = Table.grid(expand=True)
    for _ in range(8):
        grid.add_column(justify="center")
    grid.add_row(
        Text(f"GPUs\n{n}", style="bold cyan"),
        Text(f"Active\n{active}", style="bold green" if active else "dim"),
        Text(f"Avg real util\n{avg_real:5.1f}%", style=usage_style(avg_real)),
        Text(f"Power (tot/avg)\n{total_power:7.0f}W / {avg_power:5.0f}W", style=power_style(avg_power) if n else "dim"),
        Text(f"FB used\n{fb_text}", style=usage_style(mem_pct)),
        Text(f"CPU\n{cpu_text}", style=usage_style(cpu_pct) if cpu_pct is not None else "dim"),
        Text(f"RAM\n{ram_text}", style=usage_style(ram_pct)),
        Text(f"Health\n{critical} warn/crit", style="bold red" if critical else "green"),
    )
    return Panel(grid, title=f"{APP_NAME} (v{__version__})", border_style="cyan", box=box.ROUNDED)


# ── Fleet View (GPU cards) ───────────────────────────────────────────────────

def gpu_card(state: DerivedGPUState, history: HistoryStore, power_limit: Optional[float] = None, nvlink_limit: Optional[float] = None) -> Panel:
    gpu = state.gpu_id
    name = state.identity.get("modelName", "GPU")
    device = state.identity.get("device", f"gpu{gpu}")

    gpu_util = to_percent("DCGM_FI_DEV_GPU_UTIL", state.values.get("DCGM_FI_DEV_GPU_UTIL"))
    gr_active = to_percent("DCGM_FI_PROF_GR_ENGINE_ACTIVE", state.values.get("DCGM_FI_PROF_GR_ENGINE_ACTIVE"))
    sm_active = to_percent("DCGM_FI_PROF_SM_ACTIVE", state.values.get("DCGM_FI_PROF_SM_ACTIVE"))
    sm_occ = to_percent("DCGM_FI_PROF_SM_OCCUPANCY", state.values.get("DCGM_FI_PROF_SM_OCCUPANCY"))
    tensor = to_percent("DCGM_FI_PROF_PIPE_TENSOR_ACTIVE", state.values.get("DCGM_FI_PROF_PIPE_TENSOR_ACTIVE"))
    dram = to_percent("DCGM_FI_PROF_DRAM_ACTIVE", state.values.get("DCGM_FI_PROF_DRAM_ACTIVE"))

    table = Table.grid(expand=True)
    table.add_column(ratio=1)
    table.add_column(ratio=1)

    left = Table.grid(padding=(0, 1))
    left.add_column(justify="left", min_width=11, no_wrap=True)
    left.add_column(justify="right", min_width=18, no_wrap=True)
    left.add_row("Real util", Text(fmt_pct(state.real_util), style=usage_style(state.real_util)))
    left.add_row("GPU util", Text(fmt_pct(gpu_util), style=usage_style(gpu_util)))
    left.add_row("GR active", Text(fmt_pct(gr_active), style=usage_style(gr_active)))
    sm_combo_style = usage_style(sm_active)
    left.add_row("SM actv/occ", Text(f"{fmt_pct(sm_active)} / {fmt_pct(sm_occ)}", style=sm_combo_style))
    left.add_row("Tensor", Text(fmt_pct(tensor), style=usage_style(tensor)))
    left.add_row("DRAM", Text(fmt_pct(dram), style=usage_style(dram)))
    left.add_row("Memory", Text(
        f"{fmt_mib(state.values.get('DCGM_FI_DEV_FB_USED'))} / {fmt_mib(state.memory_total_mib)} ({fmt_pct(state.memory_used_pct)})",
        style=usage_style(state.memory_used_pct),
    ))
    _power_w = state.values.get("DCGM_FI_DEV_POWER_USAGE")
    _power_max = power_limit
    _power_text = f"{fmt_watts(_power_w)} / {fmt_watts(_power_max)}" if _power_max else fmt_watts(_power_w)
    left.add_row("Power", Text(_power_text, style=power_style(_power_w)))
    _gpu_t = state.values.get('DCGM_FI_DEV_GPU_TEMP')
    _mem_t = state.values.get('DCGM_FI_DEV_MEMORY_TEMP')
    _max_t = max(t for t in (_gpu_t, _mem_t) if t is not None) if (_gpu_t is not None or _mem_t is not None) else None
    left.add_row("Temps", Text(
        f"GPU {fmt_temp(_gpu_t)} | MEM {fmt_temp(_mem_t)}",
        style=temp_style(_max_t, state.identity.get("modelName")),
    ))

    right = Table.grid(padding=(0, 1))
    right.add_column(justify="left", min_width=11, no_wrap=True)
    right.add_column(justify="right", min_width=22, no_wrap=True)
    memcpy_pct = to_percent("DCGM_FI_DEV_MEM_COPY_UTIL", state.values.get("DCGM_FI_DEV_MEM_COPY_UTIL"))
    nvlink_gbps = bytes_per_s_to_gbps(state.rates.get(NVLINK_TOTAL_METRIC))
    right.add_row("Memcpy", Text(fmt_pct(memcpy_pct), style=usage_style(memcpy_pct)))
    right.add_row("PCIe RX", Text(fmt_bytes_per_s(state.values.get("DCGM_FI_PROF_PCIE_RX_BYTES")), style="cyan"))
    right.add_row("PCIe TX", Text(fmt_bytes_per_s(state.values.get("DCGM_FI_PROF_PCIE_TX_BYTES")), style="cyan"))
    nvlink_text = "N/A" if nvlink_limit is None and nvlink_gbps is None else fmt_gbps(nvlink_gbps)
    right.add_row("NVLink Δ", Text(nvlink_text, style=nvlink_util_style(nvlink_gbps, nvlink_limit)))
    right.add_row("PCIe replay", Text(fmt_num(state.rates.get("DCGM_FI_DEV_PCIE_REPLAY_COUNTER"), 2) + "/s", style="yellow" if (state.rates.get("DCGM_FI_DEV_PCIE_REPLAY_COUNTER") or 0) > 0 else "dim"))
    right.add_row("SM clock", Text(fmt_mhz(state.values.get("DCGM_FI_DEV_SM_CLOCK")), style="green"))
    right.add_row("MEM clock", Text(fmt_mhz(state.values.get("DCGM_FI_DEV_MEM_CLOCK")), style="green"))
    right.add_row("Energy", Text(fmt_joules(state.energy_j), style="magenta"))
    right.add_row("Status", Text(f"{state.status_line:<22}", style=state.health_style))

    table.add_row(left, right)

    bars = Table.grid(expand=True)
    bars.add_column(width=4, no_wrap=True)
    bars.add_column(ratio=1, no_wrap=True)
    bars.add_column(width=1)
    bars.add_column(width=4, no_wrap=True)
    bars.add_column(ratio=1, no_wrap=True)
    bars.add_column(width=1)
    bars.add_column(width=4, no_wrap=True)
    bars.add_column(ratio=1, no_wrap=True)
    power_w = state.values.get("DCGM_FI_DEV_POWER_USAGE") or 0.0
    power_cap = power_limit if power_limit and power_limit > 0 else 700.0
    power_pct = min(100.0, power_w / power_cap * 100.0)
    bw = 12  # bar width for fleet cards
    bars.add_row(
        Text("real", style="dim"), make_bar(state.real_util, width=bw),
        Text(""),
        Text("mem ", style="dim"), make_bar(state.memory_used_pct, width=bw),
        Text(""),
        Text("pwr ", style="dim"), make_bar(power_pct, width=bw, style_override=power_style(power_w)),
    )

    body = Group(
        Text.assemble(
            (f"GPU {gpu}  ", "bold"),
            (device, "cyan"),
            (f"  {name}  ", "dim"),
            (f"[{state.health}]", state.health_style),
        ),
        table,
        bars,
    )
    border = "red" if state.health == "CRIT" else "yellow" if state.health != "OK" else "blue"
    return Panel(body, box=box.ROUNDED, border_style=border)


# ── Line chart renderer ──────────────────────────────────────────

def _data_level(rows: int, value: float) -> int:
    """Convert a 0-100 percentage to a screen row (0 = top = 100%, rows-1 = bottom = 0%)."""
    if rows <= 1:
        return 0
    level = rows - 1 - round(value * (rows - 1) / 100.0)
    return max(0, min(rows - 1, int(level)))


def _render_line_chart(
    gpu_data: List[Tuple[str, List[float]]],
    chart_rows: int,
    chart_cols: int,
    vmax: float = 100.0,
) -> List[List[Tuple[str, int]]]:
    """
    Render a line chart into a 2D character grid.

    gpu_data:   [(gpu_id, [values ...]), ...]  one entry per GPU line.
    chart_rows: height of the chart area in rows.
    chart_cols: width  of the chart area in columns.
    vmax:       the value that maps to 100% on the Y axis.

    Returns grid[row][col] = (character, color_index).
    color_index == -1 means empty / no color.
    """
    # Priority: empty=0, horizontal=1, vertical/corner=2.
    # Higher-priority characters are not overwritten by lower ones,
    # so corners/verticals of one GPU aren't erased by another's horizontals.
    _PRI = {' ': 0, _CH_HLINE: 1, _CH_VLINE: 2,
            _CH_ULCORNER: 2, _CH_URCORNER: 2, _CH_LLCORNER: 2, _CH_LRCORNER: 2}

    grid: List[List[Tuple[str, int]]] = [[(' ', -1)] * chart_cols for _ in range(chart_rows)]
    pri:  List[List[int]]             = [[0] * chart_cols for _ in range(chart_rows)]
    if chart_rows < 2 or chart_cols < 1 or not gpu_data:
        return grid

    def _put(r: int, c: int, ch: str, cidx: int) -> None:
        p = _PRI.get(ch, 1)
        if p >= pri[r][c]:
            grid[r][c] = (ch, cidx)
            pri[r][c] = p

    for line_idx, (_gpu_id, values) in enumerate(gpu_data):
        if not values:
            continue

        # Normalise to 0-100 range
        if vmax > 0 and vmax != 100.0:
            norm = [max(0.0, min(100.0, v / vmax * 100.0)) for v in values]
        else:
            norm = [max(0.0, min(100.0, v)) for v in values]

        # Pad with zeros on the left so the line is continuous from column 0
        # or truncate to show only the most recent chart_cols values
        if len(norm) < chart_cols:
            norm = [0.0] * (chart_cols - len(norm)) + norm
        elif len(norm) > chart_cols:
            norm = norm[-chart_cols:]

        prev_row: Optional[int] = None
        for col in range(chart_cols):
            cur_row = _data_level(chart_rows, norm[col])

            if prev_row is None or cur_row == prev_row:
                # First point or flat segment: horizontal line
                _put(cur_row, col, _CH_HLINE, line_idx)
            elif cur_row < prev_row:
                # Value went UP (smaller row number = higher on screen)
                _put(cur_row, col, _CH_ULCORNER, line_idx)
                _put(prev_row, col, _CH_LRCORNER, line_idx)
                for r in range(cur_row + 1, prev_row):
                    _put(r, col, _CH_VLINE, line_idx)
            else:
                # Value went DOWN (larger row number = lower on screen)
                _put(prev_row, col, _CH_URCORNER, line_idx)
                _put(cur_row, col, _CH_LLCORNER, line_idx)
                for r in range(prev_row + 1, cur_row):
                    _put(r, col, _CH_VLINE, line_idx)

            prev_row = cur_row

    return grid


class LinePlotRenderable:
    """Rich renderable that draws a line chart, adapting to available width."""

    def __init__(
        self,
        gpu_data: List[Tuple[str, List[float]]],
        chart_rows: int = 10,
        vmax: float = 100.0,
        poll: float = 1.0,
    ):
        self.gpu_data = gpu_data
        self.chart_rows = chart_rows
        self.vmax = vmax
        self.poll = poll

    # ── Rich protocol ──────────────────────────────────────────────────

    def __rich_console__(self, console, options):
        from rich.measure import Measurement

        width = options.max_width
        y_label_w = 4                         # "100 " is 4 chars
        chart_cols = max(1, width - y_label_w)

        grid = _render_line_chart(self.gpu_data, self.chart_rows, chart_cols, self.vmax)

        # Pre-compute which rows get Y-axis labels
        label_rows: Dict[int, int] = {}
        for pct in (100, 75, 50, 25, 0):
            r = _data_level(self.chart_rows, float(pct))
            if r not in label_rows:
                label_rows[r] = pct

        for row_idx in range(self.chart_rows):
            line = Text()
            # Y-axis label
            if row_idx in label_rows:
                line.append(f"{label_rows[row_idx]:>3} ", style="dim")
            else:
                line.append("    ", style="dim")
            # Chart characters
            for char, cidx in grid[row_idx]:
                if cidx >= 0:
                    line.append(char, style=LINE_PLOT_COLORS[cidx % len(LINE_PLOT_COLORS)])
                else:
                    line.append(char)
            yield line

        # X-axis time labels
        if self.poll > 0:
            x_line = Text()
            x_line.append(" " * y_label_w)  # indent to match Y-axis label width
            # Build a ruler string, then place time labels at evenly spaced positions
            ruler = [" "] * chart_cols
            total_s = chart_cols * self.poll
            # Pick ~4-5 tick marks from left (oldest) to right (now)
            n_ticks = min(5, max(2, chart_cols // 20))
            for i in range(n_ticks + 1):
                frac = i / n_ticks
                col = int(frac * (chart_cols - 1))
                secs = total_s * (1.0 - frac)
                label = fmt_duration(-secs, signed=True) if secs > 0 else "0s"
                # Place label starting at col, but don't overflow
                start = max(0, min(col, chart_cols - len(label)))
                for j, ch in enumerate(label):
                    if start + j < chart_cols:
                        ruler[start + j] = ch
            x_line.append("".join(ruler), style="dim")
            yield x_line

    def __rich_measure__(self, console, options):
        from rich.measure import Measurement
        return Measurement(10, options.max_width)


def _line_plot_legend(gpu_ids: List[str], states: List[DerivedGPUState]) -> Text:
    """Shared legend mapping GPU colour → GPU id/model, displayed once above the charts."""
    legend = Text()
    for idx, gid in enumerate(gpu_ids):
        if idx > 0:
            legend.append("   ")
        color = LINE_PLOT_COLORS[idx % len(LINE_PLOT_COLORS)]
        model = ""
        for s in states:
            if s.gpu_id == gid:
                model = s.identity.get("modelName", "")
                break
        legend.append("━━", style=color)
        legend.append(f" GPU{gid}", style=f"bold {color}")
        if model:
            legend.append(f" {model}", style="dim")
    return legend


def line_plot_view_panel(
    states: List[DerivedGPUState],
    history: HistoryStore,
    pcie_bw_limits: Optional[Dict[str, float]] = None,
    pcie_info: str = "",
    poll: float = 1.0,
    power_limits: Optional[Dict[str, float]] = None,
    console_height: int = 50,
) -> Panel:
    """Build the full Plot View: shared legend + 3×3 grid of line charts."""
    gpu_ids = sorted({s.gpu_id for s in states}, key=lambda x: int(x) if x.isdigit() else x)

    # Dynamic chart_rows based on terminal height.
    # Vertical budget: summary(5) + footer(3) + outer Plot View panel borders(2)
    #   + legend(1) + blank(1) + 2 spacer rows between chart rows
    #   + 3 chart panels, each: chart_rows + x-axis(1) + panel borders(2)
    # So: 5 + 3 + 2 + 1 + 1 + 2 + 3*(chart_rows + 3) = 23 + 3*chart_rows
    # chart_rows = (console_height - 23) / 3
    chart_rows = max(3, (console_height - 23) // 3)
    # PCIe vmax: use theoretical max from nvidia-smi, fall back to observed peak
    pcie_vmax = 0.0
    if pcie_bw_limits:
        for gid in gpu_ids:
            if gid in pcie_bw_limits:
                pcie_vmax = max(pcie_vmax, pcie_bw_limits[gid])
    if pcie_vmax <= 0:
        # Fallback to observed peak
        for gid in gpu_ids:
            hist = list(history.get(gid, "pcie_rxtx"))
            if hist:
                pcie_vmax = max(pcie_vmax, max(hist))
        pcie_vmax = max(pcie_vmax, 1.0)

    def _chart_panel(title: str, hist_key: str, vmax: float = 100.0) -> Panel:
        gpu_data = [(gid, list(history.get(gid, hist_key))) for gid in gpu_ids]
        return Panel(
            LinePlotRenderable(gpu_data, chart_rows=chart_rows, vmax=vmax, poll=poll),
            title=title,
            border_style="blue",
            box=box.ROUNDED,
        )

    pcie_title = f"PCIe RX+TX %  ({pcie_info})" if pcie_info else f"PCIe RX+TX %  (max {fmt_bytes_per_s(pcie_vmax)})"

    panels = [
        # Row 1: Real Utilization, GPU Utilization, GR Engine Active
        _chart_panel("Real util %", "real_util"),
        _chart_panel("GPU util %", "gpu_util"),
        _chart_panel("GR active %", "gr_active"),
        # Row 2: SM Active, SM Occupancy, Tensor Active
        _chart_panel("SM active %", "sm_active"),
        _chart_panel("SM occupancy %", "sm_occupancy"),
        _chart_panel("Tensor active %", "tensor"),
        # Row 3: DRAM Active, Memcpy, PCIe RX+TX
        _chart_panel("DRAM active %", "dram"),
        _chart_panel("Memcpy %", "memcpy"),
        _chart_panel(pcie_title, "pcie_rxtx", vmax=pcie_vmax),
    ]

    grid = Table.grid(expand=True)
    grid.add_column(ratio=1)
    grid.add_column(width=1)
    grid.add_column(ratio=1)
    grid.add_column(width=1)
    grid.add_column(ratio=1)
    grid.add_row(panels[0], Text(""), panels[1], Text(""), panels[2])
    grid.add_row(Text(""), Text(""), Text(""), Text(""), Text(""))
    grid.add_row(panels[3], Text(""), panels[4], Text(""), panels[5])
    grid.add_row(Text(""), Text(""), Text(""), Text(""), Text(""))
    grid.add_row(panels[6], Text(""), panels[7], Text(""), panels[8])

    legend = _line_plot_legend(gpu_ids, states)
    return Panel(Group(legend, Text(""), grid), title="Plot View", border_style="cyan", box=box.ROUNDED)


# ── Focus View ───────────────────────────────────────────────────────────────

def selected_gpu_panel(state: DerivedGPUState, history: HistoryStore, power_limit: Optional[float] = None, nvlink_limit: Optional[float] = None) -> Panel:
    gpu = state.gpu_id
    title = f"Focused GPU {gpu}   {state.identity.get('device', '')}   {state.identity.get('pci_bus_id', '')}"

    nvlink_gbps = bytes_per_s_to_gbps(state.rates.get(NVLINK_TOTAL_METRIC))
    nvlink_max = nvlink_limit or 400.0

    # Helper to read a profiling-level percent metric; returns None when absent.
    def _pct(field: str) -> Optional[float]:
        return to_percent(field, state.values.get(field))

    # Rows that are always shown (value may be None → displays "—").
    # Tuple: (label, value, hist_key, vmax)
    # A string entry acts as a section header.
    metric_rows: list = [
        "Utilization",
        ("Real util", state.real_util, "real_util", 100),
        ("GPU util", _pct("DCGM_FI_DEV_GPU_UTIL"), "gpu_util", 100),
        ("GR active", _pct("DCGM_FI_PROF_GR_ENGINE_ACTIVE"), "gr_active", 100),
        "Streaming Multiprocessors",
        ("SM active", _pct("DCGM_FI_PROF_SM_ACTIVE"), "sm_active", 100),
        ("SM occupancy", _pct("DCGM_FI_PROF_SM_OCCUPANCY"), "sm_occupancy", 100),
        "Compute Pipelines",
        ("Tensor", _pct("DCGM_FI_PROF_PIPE_TENSOR_ACTIVE"), "tensor", 100),
        ("FP16 pipe", _pct("DCGM_FI_PROF_PIPE_FP16_ACTIVE"), "fp16", 100),
        ("FP32 pipe", _pct("DCGM_FI_PROF_PIPE_FP32_ACTIVE"), "fp32", 100),
        ("FP64 pipe", _pct("DCGM_FI_PROF_PIPE_FP64_ACTIVE"), "fp64", 100),
    ]

    # Tensor-core breakdown metrics — only shown when the exporter provides them.
    tc_metrics = [
        ("TC FP16/BF16", "DCGM_FI_PROF_PIPE_TENSOR_HMMA_ACTIVE", "tc_hmma"),
        ("TC INT8", "DCGM_FI_PROF_PIPE_TENSOR_IMMA_ACTIVE", "tc_imma"),
        ("TC FP64", "DCGM_FI_PROF_PIPE_TENSOR_DFMA_ACTIVE", "tc_dfma"),
        ("TC TF32/FP32", "DCGM_FI_PROF_PIPE_TENSOR_DMMA_ACTIVE", "tc_dmma"),
        ("TC FP8", "DCGM_FI_PROF_PIPE_TENSOR_QMMA_ACTIVE", "tc_qmma"),
    ]
    tc_rows = [(lbl, _pct(field), hk, 100) for lbl, field, hk in tc_metrics if _pct(field) is not None]
    if tc_rows:
        metric_rows.append("Tensor Core Detail")
        metric_rows.extend(tc_rows)

    metric_rows.extend([
        "Memory",
        ("DRAM", _pct("DCGM_FI_PROF_DRAM_ACTIVE"), "dram", 100),
        ("Memory used", state.memory_used_pct, "mem_used_pct", 100),
        "Interconnect & Power",
        ("NVLink Δ", nvlink_gbps, None, nvlink_max),
        ("Power", state.values.get("DCGM_FI_DEV_POWER_USAGE"), "power", None),
        ("GPU temp", state.values.get("DCGM_FI_DEV_GPU_TEMP"), "gpu_temp", None),
    ])

    table = Table(box=box.SIMPLE_HEAVY, expand=True)
    table.add_column("Metric", style="bold")
    table.add_column("Now", justify="right")
    table.add_column("Bar", justify="left")
    table.add_column("Trend", justify="left")

    for entry in metric_rows:
        if isinstance(entry, str):
            table.add_row(Text(""), Text(""), Text(""), Text(""))
            table.add_row(Text(f"── {entry}", style="dim italic"), Text(""), Text(""), Text(""))
            continue
        label, value, hist_key, vmax = entry
        if label == "Power":
            now = Text(fmt_watts(value), style=power_style(value))
            max_w = power_limit or 700.0
            bar = make_bar(min(100.0, (value or 0.0) / max_w * 100.0), 22, style_override=power_style(value))
            trend = Text(sparkline(history.get(gpu, hist_key or "power"), 28), style=power_style(value))
        elif label == "NVLink Δ":
            nv_style = nvlink_util_style(value, nvlink_max)
            if nvlink_limit is None and value is None:
                now = Text("N/A", style="dim")
                bar = Text("")
                trend = Text("")
            else:
                now = Text(fmt_gbps(value), style=nv_style)
                nv_cap = nvlink_max if nvlink_max and nvlink_max > 0 else 400.0
                pct_for_bar = 0.0 if value is None else min(100.0, value / nv_cap * 100.0)
                bar = make_bar(pct_for_bar, 22, style_override=nv_style)
                trend = Text(sparkline(history.get(gpu, "nvlink_gbps"), 28, vmax), style=nv_style)
        elif "temp" in label.lower():
            model = state.identity.get("modelName")
            now = Text(fmt_temp(value), style=temp_style(value, model))
            bar = make_bar(min(100.0, (value or 0.0)), 22, style_override=temp_style(value, model))
            trend = Text(sparkline(history.get(gpu, hist_key or "gpu_temp"), 28), style=temp_style(value, model))
        else:
            now = Text(fmt_pct(value), style=usage_style(value))
            bar = make_bar(value, 22)
            trend = Text(sparkline(history.get(gpu, hist_key or "real_util"), 28, vmax), style=usage_style(value))
        table.add_row(label, now, bar, trend)

    info = Table.grid(expand=True)
    for _ in range(4):
        info.add_column()
    info.add_row(
        Text(f"Status: {state.status_line}", style=state.health_style),
        Text(f"PCIe RX: {fmt_bytes_per_s(state.values.get('DCGM_FI_PROF_PCIE_RX_BYTES'))}", style="cyan"),
        Text(f"PCIe TX: {fmt_bytes_per_s(state.values.get('DCGM_FI_PROF_PCIE_TX_BYTES'))}", style="cyan"),
        Text(f"NVLink Δ: {'N/A' if nvlink_max is None and nvlink_gbps is None else fmt_gbps(nvlink_gbps)}", style=nvlink_util_style(nvlink_gbps, nvlink_max)),
    )
    info.add_row(
        Text(f"Energy: {fmt_joules(state.energy_j)}", style="magenta"),
        Text(f"Power: {fmt_watts(state.values.get('DCGM_FI_DEV_POWER_USAGE'))}", style=power_style(state.values.get('DCGM_FI_DEV_POWER_USAGE'))),
        Text(f"SM clk: {fmt_mhz(state.values.get('DCGM_FI_DEV_SM_CLOCK'))}", style="green"),
        Text(f"MEM clk: {fmt_mhz(state.values.get('DCGM_FI_DEV_MEM_CLOCK'))}", style="green"),
    )
    info.add_row(
        Text(f"Replay rate: {fmt_num(state.rates.get('DCGM_FI_DEV_PCIE_REPLAY_COUNTER'), 2)}/s", style="yellow" if (state.rates.get('DCGM_FI_DEV_PCIE_REPLAY_COUNTER') or 0) > 0 else "dim"),
        Text("", style="dim"), Text("", style="dim"), Text("", style="dim"),
    )

    return Panel(Group(info, table), title=title, border_style="cyan", box=box.ROUNDED)


# ── Job View ─────────────────────────────────────────────────────────────────

def jobs_view_panel(states: List[DerivedGPUState], gpu_processes: Dict[str, List[GpuProcess]]) -> Panel:
    """Render a table of all running GPU compute processes with per-GPU metrics."""
    state_map = {s.gpu_id: s for s in states}

    jtable = Table(
        title="Running GPU Processes",
        box=box.SIMPLE_HEAVY,
        expand=True,
        show_lines=False,
        padding=(0, 1),
    )
    jtable.add_column("PID", justify="right", style="bold", no_wrap=True, width=8)
    jtable.add_column("User", justify="left", style="green", no_wrap=True, width=12)
    jtable.add_column("GPU", justify="right", style="cyan", no_wrap=True, width=4)
    jtable.add_column("GID", justify="left", style="yellow", no_wrap=True, width=14)
    jtable.add_column("*Status", justify="left", no_wrap=True, width=22)
    jtable.add_column("GPU Mem", justify="right", no_wrap=True, width=8)
    jtable.add_column("*GPU Util", justify="right", no_wrap=True, width=9)
    jtable.add_column("*Real Util", justify="right", no_wrap=True, width=10)
    jtable.add_column("*Tensor", justify="right", no_wrap=True, width=8)
    jtable.add_column("Command", justify="left", ratio=1, no_wrap=True)

    # Collect all processes sorted by GPU id then PID
    all_procs: List[Tuple[GpuProcess, DerivedGPUState]] = []
    for gpu_id in sorted(gpu_processes.keys(), key=lambda x: int(x) if x.isdigit() else x):
        st = state_map.get(gpu_id)
        if st is None:
            continue
        for p in gpu_processes[gpu_id]:
            all_procs.append((p, st))

    if not all_procs:
        return Panel(
            Text("No compute processes running on visible GPUs.", style="dim"),
            title="Job View",
            border_style="cyan",
            box=box.ROUNDED,
        )

    for proc, st in all_procs:
        gpu_util = to_percent("DCGM_FI_DEV_GPU_UTIL", st.values.get("DCGM_FI_DEV_GPU_UTIL"))
        tensor = to_percent("DCGM_FI_PROF_PIPE_TENSOR_ACTIVE", st.values.get("DCGM_FI_PROF_PIPE_TENSOR_ACTIVE"))
        if proc.gpu_mem_mib is not None:
            mem_text = f"{proc.gpu_mem_mib / 1024:.1f}G" if proc.gpu_mem_mib >= 1024 else f"{int(proc.gpu_mem_mib)}M"
        else:
            mem_text = "—"
        jtable.add_row(
            str(proc.pid),
            proc.user[:12],
            proc.gpu_id,
            proc.gid[:14],
            Text(st.status_line, style=st.health_style),
            Text(mem_text, style="magenta"),
            Text(fmt_pct(gpu_util), style=usage_style(gpu_util)),
            Text(fmt_pct(st.real_util), style=usage_style(st.real_util)),
            Text(fmt_pct(tensor), style=usage_style(tensor)),
            Text(proc.command, overflow="ellipsis", no_wrap=True, style="dim"),
        )

    footnote = Text("  * Per-GPU metric (shared across all processes on the same GPU)", style="dim italic")
    return Panel(Group(jtable, footnote), title="Job View", border_style="cyan", box=box.ROUNDED)


# ── Fleet panel, footer & dashboard assembly ─────────────────────────────────

def fleet_panel(states: List[DerivedGPUState], history: HistoryStore, columns: int = 2, power_limits: Optional[Dict[str, float]] = None, nvlink_bw_limits: Optional[Dict[str, float]] = None) -> Panel:
    rows: List[List[Panel]] = []
    for idx in range(0, len(states), columns):
        rows.append([gpu_card(s, history, (power_limits or {}).get(s.gpu_id), (nvlink_bw_limits or {}).get(s.gpu_id)) for s in states[idx: idx + columns]])

    grid = Table.grid(expand=True)
    for _ in range(columns):
        grid.add_column(ratio=1)
    for row in rows:
        padded = row + [Text("")] * (columns - len(row))
        grid.add_row(*padded)
    return Panel(grid, title="Fleet overview", border_style="blue", box=box.ROUNDED)


WEIGHT_PRESETS = {
    (0.35, 0.35, 0.20, 0.10): "AI/ML Workflow",
    (0.45, 0.15, 0.25, 0.15): "HPC Workflow",
    (0.35, 0.10, 0.40, 0.15): "Memory-bound Workflow",
}


def workflow_label(weights: Tuple[float, float, float, float]) -> str:
    rounded = tuple(round(w, 2) for w in weights)
    return WEIGHT_PRESETS.get(rounded, "Custom Workflow")


def footer_panel(selection_desc: str, controller: CommandController, source: str = "", poll: float = 1.0, weights: Tuple[float, float, float, float] = (0.35, 0.35, 0.20, 0.10)) -> Panel:
    selection_text = selection_desc
    msg = controller.last_message or controller.hint()
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")  # fixed 19 chars
    hostname = socket.gethostname().split('.')[0]
    display_source = re.sub(r'^https?://', '', source)
    wf_label = workflow_label(weights)
    left = Text.assemble(
        ("Visible ", "bold cyan"), (selection_text, "dim"),
        ("   ", ""), (wf_label, "bold magenta"),
        ("   Commands ", "bold"), (msg, "green" if controller.command_mode else "dim"),
    )
    left.no_wrap = True
    left.overflow = "ellipsis"
    right_plain = f"host={hostname}  src={display_source}  poll={fmt_duration(poll)}  {now_str}"
    right = Text(right_plain, style="dim", no_wrap=True)
    right_w = len(right_plain)
    line = Table.grid(expand=True)
    line.add_column(ratio=1, no_wrap=True)
    line.add_column(width=2)
    line.add_column(width=right_w, justify="right", no_wrap=True)
    line.add_row(left, Text(""), right)
    return Panel(line, border_style="dim", box=box.ROUNDED)


def render_dashboard(
    states: List[DerivedGPUState],
    history: HistoryStore,
    source: str,
    poll: float,
    controller: CommandController,
    selection_desc: str,
    power_limits: Optional[Dict[str, float]] = None,
    pcie_bw_limits: Optional[Dict[str, float]] = None,
    pcie_info: str = "",
    nvlink_bw_limits: Optional[Dict[str, float]] = None,
    console_height: int = 50,
    weights: Tuple[float, float, float, float] = (0.35, 0.35, 0.20, 0.10),
    gpu_processes: Optional[Dict[str, List[GpuProcess]]] = None,
    cpu_info: Tuple[Optional[int], Optional[int], Optional[float], Optional[int]] = (None, None, None, None),
    ram_info: Tuple[Optional[float], Optional[float]] = (None, None),
) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="summary", size=5),
        Layout(name="middle", ratio=1),
        Layout(name="footer", size=3),
    )
    layout["summary"].update(summary_panel(states, source, poll, selection_desc, cpu_info=cpu_info, ram_info=ram_info))
    layout["footer"].update(footer_panel(selection_desc, controller, source, poll, weights))

    if not states:
        layout["middle"].update(Panel(
            f"No GPU data matched the current selection ({selection_desc}).\nTry --show-all or override with --gpus 0,1",
            border_style="red",
            box=box.ROUNDED,
        ))
        return layout

    if controller.line_mode:
        layout["middle"].update(line_plot_view_panel(states, history, pcie_bw_limits, pcie_info, poll=poll, power_limits=power_limits, console_height=console_height))
    elif controller.jobs_mode:
        layout["middle"].update(jobs_view_panel(states, gpu_processes or {}))
    elif controller.focus_gpu is not None:
        selected = next((s for s in states if s.gpu_id == controller.focus_gpu), states[0])
        layout["middle"].split_row(
            Layout(name="left", ratio=3),
            Layout(name="right", ratio=4),
        )
        layout["middle"]["left"].update(fleet_panel(states, history, columns=1 if len(states) <= 2 else 2, power_limits=power_limits, nvlink_bw_limits=nvlink_bw_limits))
        gpu_power_limit = (power_limits or {}).get(selected.gpu_id)
        gpu_nvlink_limit = (nvlink_bw_limits or {}).get(selected.gpu_id)
        layout["middle"]["right"].update(selected_gpu_panel(selected, history, gpu_power_limit, gpu_nvlink_limit))
    else:
        layout["middle"].update(fleet_panel(states, history, columns=1 if len(states) <= 1 else 2, power_limits=power_limits, nvlink_bw_limits=nvlink_bw_limits))
    return layout


# ── Argument parsing & help text ─────────────────────────────────────────────

def parse_weights(raw: str) -> Tuple[float, float, float, float]:
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("--weights requires four comma-separated values: SM,TENSOR,DRAM,GR")
    try:
        vals = tuple(float(p) for p in parts)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--weights values must be numeric") from exc
    total = sum(vals)
    if total <= 0:
        raise argparse.ArgumentTypeError("--weights must sum to a positive value")
    if abs(total - 1.0) > 1e-6:
        vals = tuple(v / total for v in vals)
    return vals  # type: ignore[return-value]


HELP_EPILOG = f"""
Application:
  {APP_NAME} is a terminal dashboard for dcgm-exporter Prometheus metrics.
  It is designed to help distinguish idle GPUs, real compute, memory pressure,
  transfer/copy pressure, and hardware-health issues at a glance.

Real util equation:
  RealUtil = clamp(0, 100,
              Wsm * SM_ACTIVE
            + Wtensor * TENSOR_ACTIVE
            + Wdram * DRAM_ACTIVE
            + Wgr * GR_ENGINE_ACTIVE)

  GR_ENGINE_ACTIVE is a profiling-level hardware counter (DCGM field 1001).
  If it is unavailable the dashboard falls back to GPU_UTIL (field 203).

Default weights (AI / LLM training preset):
  --weights 0.35,0.35,0.20,0.10      (same as --ai-weights)

Meaning of the default weights:
  0.35 SM_ACTIVE           Kernel execution on SMs — main compute signal.
  0.35 TENSOR_ACTIVE       Tensor core activity — critical for AI/LLM work.
  0.20 DRAM_ACTIVE         Memory bandwidth pressure and data movement.
  0.10 GR_ENGINE_ACTIVE    Small stabilizing term for overall engine activity.

Weight presets (convenience flags):
  --ai-weights             AI / LLM training and inference  (0.35,0.35,0.20,0.10) [default]
  --hpc-weights            General mixed CUDA / HPC         (0.45,0.15,0.25,0.15)
  --mem-weights            Memory-bound / bandwidth-heavy   (0.35,0.10,0.40,0.15)

  Or supply custom weights with --weights W_SM,W_TENSOR,W_DRAM,W_GR (normalized to sum to 1).

Metrics and interpretation:
  Real util      Weighted estimate of meaningful GPU work.
  GPU util       Broad busy/idle signal from DCGM_FI_DEV_GPU_UTIL (device-level).
                 Reports % of time a kernel was running. Can stay high during
                 overhead, sync, or stalls. Same metric as nvidia-smi.
  GR active      Fraction of time the graphics/compute engine is active, from
                 DCGM_FI_PROF_GR_ENGINE_ACTIVE (profiling-level). More accurate
                 than GPU util: based on hardware performance counters rather
                 than kernel-launch sampling.
  SM active      Fraction of cycles with work assigned to the SMs.
  SM occupancy   Fraction of warps resident vs. theoretical max on SMs.
  Tensor         Fraction of cycles with tensor pipes active.
  FP16 pipe      Fraction of cycles FP16 pipes are active (Focus view only).
  FP32 pipe      Fraction of cycles FP32 pipes are active (Focus view only).
  FP64 pipe      Fraction of cycles FP64 pipes are active (Focus view only).
  TC FP16/BF16   Tensor core FP16/BF16 HMMA activity (Focus view, if available).
  TC INT8        Tensor core INT8 IMMA activity (Focus view, if available).
  TC FP64        Tensor core FP64 DFMA activity (Focus view, if available).
  TC TF32/FP32   Tensor core TF32/FP32 DMMA activity (Focus view, if available).
  TC FP8         Tensor core FP8 QMMA activity (Focus view, if available).
  DRAM           Fraction of cycles device memory is actively moving data.
  Memcpy         Device memory-copy engine utilization.
  PCIe RX/TX     Host-device throughput from dcgm-exporter in bytes/sec.
  NVLink Δ       Per-second change from DCGM_FI_DEV_NVLINK_BANDWIDTH_TOTAL,
                 shown as decimal GB/s.

Status classification (thresholds from NVIDIA DCGM profiling metric guidance):

  NVIDIA reference points used:
    SM_ACTIVE  >= 80%   "necessary, but not sufficient, for effective GPU use"
    SM_ACTIVE  <  50%   "likely indicates ineffective GPU usage"
    DRAM_ACTIVE         practical peak ~80%; >= 50% is heavy memory traffic
    TENSOR_ACTIVE       ~93% at full saturation (dcgmproftester)

  Status               Thresholds                         Rationale
  -------------------  ---------------------------------  ----------------------------
  idle                 util<5, GR<5, DRAM<5, no I/O       Nothing running
  tensor-heavy compute Tensor>=50% + SM>=60%               DL training / large inference
  tensor compute       Tensor>=15% + SM>=40%               Moderate tensor use (mixed prec)
  FP64 / HPC compute   FP64>=20% + SM>=50%                 Scientific double-precision
  I/O or data-loading  memcpy>=40% or PCIe>=1GB/s, SM<30%  Transfer heavy; SMs idle
  memory-bound         DRAM>=50% + SM<50%                  Bandwidth limited (NVIDIA: <50% ineffective)
  compute-heavy        SM>=80%                             Effective use (NVIDIA: >=80% needed)
  compute-active       SM>=50%                             Moderate compute, no tensor dominance
  memory-active        DRAM>=40%                           Significant mem traffic, some compute
  busy, low SM use     GR>=40% + SM<25%                    Overhead / sync / small kernels
  low utilization      GR<15% + SM<15% + DRAM<15%          Barely active
  mixed / moderate     (fallthrough)                       No single dominant pattern

Health meanings:
  OK     No current warning condition.
  WARN   PCIe replay counter is growing.
  HOT    GPU or memory temperature is high enough to deserve attention.
  CRIT   Row remap failure or uncorrectable remapped rows detected.

GPU visibility selection:
  The dashboard uses the first matching source in this order:
    1. --gpus
    2. CUDA_VISIBLE_DEVICES
    3. NVIDIA_VISIBLE_DEVICES
    4. SLURM_STEP_GPUS
    5. SLURM_JOB_GPUS
  If none are usable, all GPUs on the node are shown. Use --show-all to ignore
  the environment and show every GPU, or --gpus to force an explicit list.
  All selections are filtered against GPUs accessible to the current process
  (as reported by nvidia-smi), respecting cgroup and container restrictions.

Interactive commands:
  :focus 0     Enter focused view for GPU 0
  :focus 1     Enter focused view for GPU 1
  :plot        Enter plot view (line charts)
  :job         Enter job view (running GPU processes)
  :q           In any sub-view: return to fleet view
               In fleet view: exit the dashboard
  :exit        Exit the dashboard
  Ctrl+C       Exit the dashboard
  Esc          Cancel an unfinished command after ':'

Backend selection:
  --backend prometheus     (default) Read metrics from the dcgm-exporter
                           Prometheus HTTP endpoint.  Profiling fields (SM, tensor,
                           DRAM) update at the exporter's configured interval,
                           typically ~30 seconds.  Best for fleet-level monitoring.

  --backend dcgm           Query dcgmi dmon directly.  Each poll cycle invokes
                           dcgmi dmon -c 2, giving true per-sample resolution
                           (down to 100ms with --poll 0.1).  Best for single-node
                           workload profiling.  Requires the DCGM daemon to be
                           running on the node.  Automatically resolves physical
                           GPU IDs inside SLURM cgroups.

Examples:
  python3 kempner_pulse.py
  python3 kempner_pulse.py --poll 1.0
  python3 kempner_pulse.py --focus-gpu 0
  python3 kempner_pulse.py --hpc-weights
  python3 kempner_pulse.py --mem-weights
  python3 kempner_pulse.py --weights 0.40,0.30,0.20,0.10
  python3 kempner_pulse.py --gpus 2,3
  python3 kempner_pulse.py --show-all
  python3 kempner_pulse.py --source http://otherhost:9400/metrics
  python3 kempner_pulse.py --source /path/to/dcgm_metrics.txt
  python3 kempner_pulse.py --backend dcgm --poll 0.5
  python3 kempner_pulse.py --backend dcgm --export all --poll 0.1
"""


# ── Entry point ──────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description=f"{APP_NAME}: CLI dashboard for DCGM Prometheus metrics with SLURM/CUDA GPU visibility awareness",
        epilog=HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--source", default="http://localhost:9400/metrics", help="Path to a dcgm-exporter text file or an http(s) /metrics endpoint. Default: http://localhost:9400/metrics")
    parser.add_argument("--backend", choices=["prometheus", "dcgm"], default="prometheus",
                        help="Metric collection backend. 'prometheus' reads from dcgm-exporter HTTP endpoint "
                             "(~30s resolution for profiling fields). 'dcgm' queries dcgmi dmon directly for "
                             "true high-resolution sampling (down to 100ms). Default: prometheus")
    parser.add_argument("--poll", type=float, default=1.0, help="UI refresh interval in seconds. This redraws the dashboard; it does not force DCGM itself to sample faster. Default: 1.0")
    parser.add_argument("--history", type=int, default=120, help="Number of samples kept for sparkline history. Default: 120")
    parser.add_argument("--focus-gpu", default=None, help="Start in focused view for one GPU id, for example 0")
    parser.add_argument("--once", action="store_true", help="Render one snapshot and exit instead of running live")
    parser.add_argument("--gpus", default=None, help="Explicit GPU ids or ranges to show, for example 0,1 or 0-3. Overrides SLURM/CUDA visibility env vars.")
    parser.add_argument("--show-all", action="store_true", help="Ignore SLURM/CUDA visibility env vars and show every GPU seen in the DCGM source")
    parser.add_argument("--weights", type=parse_weights, default=(0.35, 0.35, 0.20, 0.10), help="Comma-separated real-util weights in SM,TENSOR,DRAM,GR order. Values are normalized to sum to 1. Example: --weights 0.40,0.30,0.20,0.10")
    parser.add_argument("--ai-weights", dest="weights", action="store_const", const=(0.35, 0.35, 0.20, 0.10), help="Use AI/LLM training weight preset (0.35,0.35,0.20,0.10) — this is the default")
    parser.add_argument("--hpc-weights", dest="weights", action="store_const", const=(0.45, 0.15, 0.25, 0.15), help="Use general HPC weight preset (0.45,0.15,0.25,0.15)")
    parser.add_argument("--mem-weights", dest="weights", action="store_const", const=(0.35, 0.10, 0.40, 0.15), help="Use memory-bound weight preset (0.35,0.10,0.40,0.15)")
    parser.add_argument("--export", nargs="?", const="default", default=None, metavar="COLS",
                        help="Output CSV to stdout. Use --export for default columns, "
                             "--export all for every column, or --export col1,col2,... for a "
                             "custom set. Available columns: "
                             "timestamp,gpu_id,model,real_util_pct,status,health,"
                             "sm_active_pct,tensor_active_pct,dram_active_pct,"
                             "gr_engine_active_pct,gpu_util_pct,"
                             "mem_used_mib,mem_total_mib,mem_used_pct,power_w,"
                             "gpu_temp_c,mem_temp_c,"
                             "sm_occupancy_pct,fp16_pipe_pct,fp32_pipe_pct,fp64_pipe_pct,"
                             "memcpy_util_pct,pcie_rx_bytes_s,pcie_tx_bytes_s,nvlink_gbps,"
                             "sm_clock_mhz,mem_clock_mhz,pcie_replay_rate_s,energy_j,"
                             "tc_hmma_pct,tc_imma_pct,tc_dfma_pct,tc_dmma_pct,tc_qmma_pct")
    args = parser.parse_args()

    console = Console()
    history = HistoryStore(maxlen=max(10, args.history))
    prev: Optional[Sample] = None
    controller = CommandController(args.focus_gpu)

    use_dcgm_backend = args.backend == "dcgm"
    dcgm_physical_gpu_ids: Optional[List[str]] = None
    dcgm_phys_to_local: Dict[str, str] = {}

    if use_dcgm_backend:
        # Verify dcgmi is available and discover GPUs in one call
        try:
            probe = subprocess.run(["dcgmi", "discovery", "-l"],
                                   capture_output=True, text=True, timeout=10)
            if probe.returncode != 0:
                console.print("[bold red]dcgmi not available or DCGM daemon not running.[/]")
                console.print(f"[dim]stderr: {probe.stderr.strip()}[/]")
                return 1
        except FileNotFoundError:
            console.print("[bold red]dcgmi command not found. Install DCGM or use --backend prometheus.[/]")
            return 1
        except subprocess.TimeoutExpired:
            console.print("[bold red]dcgmi timed out. Is the DCGM daemon running?[/]")
            return 1
        dcgm_physical_gpu_ids, dcgm_phys_to_local = _resolve_dcgm_gpu_ids(probe.stdout)
        if not dcgm_physical_gpu_ids:
            console.print("[bold yellow]Warning: could not resolve physical GPU IDs; dcgmi will monitor all GPUs.[/]")
            dcgm_physical_gpu_ids = None
            dcgm_phys_to_local = {}

    if use_dcgm_backend:
        # For dcgm backend, accessible GPUs come from nvidia-smi directly
        dcgm_accessible, dcgm_bus_map = None, {}
        accessible_gpus = query_accessible_gpus()
        # Map physical IDs as accessible when using dcgm backend
        if dcgm_physical_gpu_ids:
            accessible_gpus = set(dcgm_physical_gpu_ids)
    else:
        dcgm_accessible, dcgm_bus_map = resolve_dcgm_mapping(args.source)
        accessible_gpus = dcgm_accessible if dcgm_accessible is not None else query_accessible_gpus()

    if not accessible_gpus:
        console.print("[bold red]KempnerPulse requires a node with NVIDIA GPUs.[/]")
        return 1
    selector = GPUSelector(explicit=args.gpus, disable_auto=args.show_all, accessible=accessible_gpus)
    allowed_gpu_ids, _, _ = selector.resolve()
    selection_desc = "all" if allowed_gpu_ids is None else ",".join(sorted(allowed_gpu_ids, key=lambda x: int(x) if x.isdigit() else x)) or "none"
    power_limits = query_power_limits()
    gpu_models = query_gpu_models()
    pcie_bw_limits, pcie_info = query_pcie_bandwidth()
    nvlink_bw_limits = query_nvlink_bandwidth()
    bus_id_map = dcgm_bus_map or query_bus_id_mapping()

    # When using dcgm backend inside a SLURM cgroup, nvidia-smi returns local
    # GPU indices (e.g. "0") but dcgmi uses physical indices (e.g. "1").
    # Re-key the nvidia-smi lookup dicts so they match dcgmi GPU IDs.
    if use_dcgm_backend and dcgm_phys_to_local:
        local_to_phys = {v: k for k, v in dcgm_phys_to_local.items()}
        gpu_models = {local_to_phys.get(k, k): v for k, v in gpu_models.items()}
        power_limits = {local_to_phys.get(k, k): v for k, v in power_limits.items()}
        pcie_bw_limits = {local_to_phys.get(k, k): v for k, v in pcie_bw_limits.items()}
        nvlink_bw_limits = {local_to_phys.get(k, k): v for k, v in nvlink_bw_limits.items()}

    # --poll validation / backend-specific handling
    if use_dcgm_backend:
        poll_ms = max(DCGM_STREAM_MIN_INTERVAL_MS, int(round(args.poll * 1000)))
        if int(round(args.poll * 1000)) < DCGM_STREAM_MIN_INTERVAL_MS:
            print(f"kempnerpulse: --poll {args.poll}s is below the dcgm profiling floor; "
                  f"clamping to {DCGM_STREAM_MIN_INTERVAL_MS}ms.", file=sys.stderr)
    else:
        poll_ms = int(round(args.poll * 1000))
        if args.poll < 1.0:
            # Prometheus backend: dcgm-exporter's scrape interval (~30s for profiling
            # fields) sets the true ceiling. Sub-second --poll values produce
            # duplicate samples with no new data.
            console.print(
                f"[bold yellow]Warning:[/] --poll {args.poll}s is below the Prometheus "
                f"backend's effective sampling rate.\n"
                f"[dim]dcgm-exporter scrapes DCGM at ~30s for profiling fields, so "
                f"sub-second --poll values produce duplicate samples with no new data.[/]\n"
                f"Options:\n"
                f"  • Use [bold]--backend dcgm[/] for true high-resolution sampling (down to 100ms)\n"
                f"  • Raise [bold]--poll[/] to >= 1.0 on the prometheus backend"
            )
            return 1

    # Start streaming reader for the dcgm backend (not needed for --once, which
    # uses the synchronous load_dcgm_direct path).
    reader: Optional[DcgmStreamReader] = None
    if use_dcgm_backend and not args.once:
        reader = DcgmStreamReader(
            gpu_ids=dcgm_physical_gpu_ids,
            poll_ms=poll_ms,
            gpu_models=gpu_models,
        )
        try:
            reader.start()
        except DcgmStreamError as exc:
            console.print(f"[bold red]dcgmi stream failed to start: {exc}[/]")
            return 1
        atexit.register(reader.stop)
        # SIGTERM's default action bypasses atexit; redirect to a clean exit so
        # the reader subprocess is reaped and the terminal is restored.
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        try:
            if not reader.wait_first_sample(timeout=5.0):
                console.print("[bold red]Timed out waiting for the first dcgmi sample.[/]")
                reader.stop()
                return 1
        except DcgmStreamError as exc:
            console.print(f"[bold red]dcgmi stream error: {exc}[/]")
            reader.stop()
            return 1

    current_visible_ids: Set[str] = set()
    cached_states: List[DerivedGPUState] = []
    cached_gpu_procs: Dict[str, List[GpuProcess]] = {}
    cached_cpu_info: Tuple[Optional[int], Optional[int], Optional[float], Optional[int]] = (None, None, None, None)
    cached_ram_info: Tuple[Optional[float], Optional[float]] = (None, None)

    def fetch_data() -> None:
        nonlocal prev, current_visible_ids, cached_states, cached_gpu_procs
        nonlocal cached_cpu_info, cached_ram_info
        sample: Optional[Sample] = None
        filtered_prev: Optional[Sample] = None
        try:
            if use_dcgm_backend and reader is not None:
                # Streaming path: the reader thread owns the rolling (latest, prev) pair.
                stream_latest, stream_prev = reader.get_pair()
                if stream_latest is None:
                    return
                sample = stream_latest
                filtered_prev = filter_sample_to_gpu_ids(stream_prev, allowed_gpu_ids) if stream_prev is not None else None
            elif use_dcgm_backend:
                # --once path: keep the synchronous one-shot invocation.
                raw = load_dcgm_direct(gpu_ids=dcgm_physical_gpu_ids)
                sample = parse_dcgm_dmon(raw, gpu_models)
                filtered_prev = filter_sample_to_gpu_ids(prev, allowed_gpu_ids) if prev is not None else None
            else:
                raw = load_source(args.source)
                sample = parse_prometheus_text(raw)
                filtered_prev = filter_sample_to_gpu_ids(prev, allowed_gpu_ids) if prev is not None else None
        except Exception as exc:
            print(f"kempnerpulse: data fetch failed: {exc}", file=sys.stderr)
            return
        filtered_sample = filter_sample_to_gpu_ids(sample, allowed_gpu_ids)
        cached_states = build_gpu_states(filtered_sample, filtered_prev, args.weights, gpu_models)
        update_history(history, cached_states)
        # For dcgm streaming, the reader owns `prev`. For prometheus and --once,
        # we continue to track it locally for delta computation.
        if not (use_dcgm_backend and reader is not None):
            prev = sample
        current_visible_ids = {s.gpu_id for s in cached_states}
        cached_gpu_procs = query_gpu_processes(bus_id_map) if controller.jobs_mode else {}
        cached_cpu_info = query_system_cpu()
        cached_ram_info = query_system_ram()
        if controller.focus_gpu is not None and controller.focus_gpu not in current_visible_ids and current_visible_ids:
            controller.focus_gpu = sorted(current_visible_ids, key=lambda x: int(x) if x.isdigit() else x)[0]
            controller.last_message = f"Focused GPU unavailable; switched to GPU {controller.focus_gpu}"

    source_label = "dcgmi dmon (direct)" if use_dcgm_backend else args.source

    def get_layout() -> Layout:
        return render_dashboard(
            cached_states,
            history,
            source_label,
            args.poll,
            controller,
            selection_desc,
            power_limits,
            pcie_bw_limits,
            pcie_info,
            nvlink_bw_limits,
            console_height=console.height,
            weights=args.weights,
            gpu_processes=cached_gpu_procs,
            cpu_info=cached_cpu_info,
            ram_info=cached_ram_info,
        )

    fetch_data()

    if args.export is not None:
        export_cols = resolve_export_columns(args.export)
        writer = csv.writer(sys.stdout)
        writer.writerow([col for col, _ in export_cols])
        sys.stdout.flush()

        current_user = pwd.getpwuid(os.getuid()).pw_name
        _p2l = dcgm_phys_to_local if use_dcgm_backend else None
        ownership = OwnershipCache(bus_id_map, ttl=2.0)

        warned_empty = False

        def emit_rows() -> None:
            nonlocal warned_empty
            gpu_procs = ownership.get()
            filtered = filter_states_for_current_user(cached_states, gpu_procs, _p2l)
            if not filtered:
                if not warned_empty:
                    print(f"kempnerpulse: no GPU compute processes found for user "
                          f"'{current_user}' on {len(cached_states)} visible GPU(s). "
                          "Will emit rows when processes appear.", file=sys.stderr)
                    warned_empty = True
                return
            warned_empty = False
            timestamp = time.time()
            try:
                for state in filtered:
                    writer.writerow(export_gpu_row(state, timestamp, export_cols))
                sys.stdout.flush()
            except BrokenPipeError:
                raise

        try:
            emit_rows()
            if not args.once:
                if use_dcgm_backend and reader is not None:
                    # Streaming path: block on the reader's sample counter so we
                    # emit exactly once per new dcgmi tick (the reader paces at
                    # poll_ms; no time.sleep needed).
                    last_counter = reader.last_counter()
                    while True:
                        latest, _prev, new_counter = reader.wait_for_new(
                            last_counter, timeout=max(args.poll * 5.0, 2.0)
                        )
                        if latest is None:
                            # Reader stopped or errored — exit cleanly.
                            break
                        last_counter = new_counter
                        fetch_data()
                        emit_rows()
                else:
                    while True:
                        time.sleep(args.poll)
                        fetch_data()
                        emit_rows()
        except KeyboardInterrupt:
            pass
        except BrokenPipeError:
            try:
                sys.stdout.close()
            except BrokenPipeError:
                pass
        return 0

    if args.once:
        console.print(get_layout())
        return 0

    with cbreak_stdin(enabled=True):
        with Live(get_layout(), console=console, screen=True, auto_refresh=False) as live:
            last_fetch = time.time()
            last_render = time.time()
            cached_layout = None
            prev_cmd_state = (controller.command_mode, controller.buffer, controller.last_message,
                              controller.focus_gpu, controller.line_mode, controller.jobs_mode)
            while True:
                try:
                    controller.handle_input(current_visible_ids)
                    if controller.should_exit:
                        break
                    now = time.time()
                    if now - last_fetch >= args.poll:
                        fetch_data()
                        last_fetch = now
                        cached_layout = None  # force rebuild after new data
                    cur_cmd_state = (controller.command_mode, controller.buffer, controller.last_message,
                                     controller.focus_gpu, controller.line_mode, controller.jobs_mode)
                    if cur_cmd_state != prev_cmd_state:
                        cached_layout = None  # force rebuild after command state change
                        prev_cmd_state = cur_cmd_state
                    if cached_layout is None or now - last_render >= 0.25:
                        cached_layout = get_layout()
                        live.update(cached_layout, refresh=True)
                        last_render = now
                    time.sleep(0.02)
                except KeyboardInterrupt:
                    break
                except (OSError, ValueError) as exc:
                    live.update(Panel(str(exc), title="dashboard error", border_style="red"), refresh=True)
                    time.sleep(max(args.poll, 1.0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
