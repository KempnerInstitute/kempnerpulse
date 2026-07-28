# Changelog

All notable changes to KempnerPulse are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **NVLink fallback for systems where the aggregate gauge is unavailable**:
  `DCGM_FI_DEV_NVLINK_BANDWIDTH_TOTAL` (field 449) reports `N/A` on some systems
  while the NVLink profiling fields `DCGM_FI_PROF_NVLINK_TX_BYTES` and
  `DCGM_FI_PROF_NVLINK_RX_BYTES` (1011/1012) still carry usable per-direction
  rates. Those two fields are now collected, and the canonical NVLink aggregate
  falls back to their sum when 449 is missing. A real `0.0` from 449 means idle
  and does *not* trigger the fallback, and a one-sided reading is reported as
  unknown rather than as a silently halved total (#21)

## [0.5.0] - 2026-07-27

This release replaces the single-file implementation with a layered package.
The command-line interface is unchanged apart from the new default backend and
poll interval noted below; the **Python import surface is not** — see *Removed*.

### Added
- **Layered architecture** (`src/kempnerpulse/`): a four-stage data-flow pipeline
  — `reader/` (raw acquisition) → `translate/` (vendor-neutral canonical record)
  → `compute/` (pure-functional real-util, classification, health) →
  `present/` (Rich TUI and CSV). Backend and DCGM field vocabulary now stop at
  Layer 2, and the domain logic in Layer 3 is testable with no GPU present. A
  cross-cutting tier holds `config`, `identification`, `selection`,
  `system_queries`, and `lifecycle`. See `docs/architecture.md`.
- **`replay` backend** (`--backend replay`) — drives the whole pipeline from a
  saved capture, so the UI and compute layers can be exercised without DCGM or
  a GPU.
- **`kp`** as a short console-script alias for `kempnerpulse`.
- **Test suite** — 156 tests under `tests/` (unit per layer, plus an
  integration test over the full pipeline) with recorded dcgmi and Prometheus
  fixtures.
- **Continuous integration** — `.github/workflows/tests.yml` runs the suite;
  `.github/workflows/docs.yml` builds the documentation.
- **Documentation site** — Sphinx + MyST sources under `docs/`, covering
  install, quickstart, views, backends, CLI, running on SLURM, the canonical
  schema, and the classification taxonomy. Most of the former README body moved
  here.
- **`uv.lock`** — a pinned, reproducible development environment; `uv run pytest`
  and `uv run kp` work with no manual setup.

### Changed
- **Package layout is now src-based.** The console entry point moved from
  `kempner_pulse:main` to `kempnerpulse.__main__:main`, and the version is
  single-sourced from `pyproject.toml` via `importlib.metadata`.
- **Default backend is now `dcgm`** (previously `prometheus`), and **`--poll` is
  backend-aware**: `0.1` s (100 ms) for `--backend dcgm`, `1.0` s for
  `--backend prometheus`. A bare `kempnerpulse` is now equivalent to
  `kempnerpulse --backend dcgm --poll 0.1`. The prometheus backend still rejects
  sub-`1.0` s polling.
- **Fleet card header** drops the redundant `gpu<id>` device token when it only
  duplicates the `GPU <id>` index (kept when it carries extra info, e.g. MIG).
- **Top summary bar** drops low-priority fields as the terminal narrows
  (`CPU` → `RAM` → `FB used` → `Health` → `Power`), leaving GPUs / Active / Avg real
  util, so the remaining fields stay readable instead of jamming.
- **Footer status line** drops fields on both sides as it narrows instead of
  jamming: right side `host` → `src` → `poll` → date; left side (priority order
  `Commands`, `Visible`, workflow) drops the workflow then `Visible`, always keeping
  the `Commands` field so the command input stays visible while you type.
- **Fleet grid is now 2-D proportional** (`choose_grid` + shared `build_fleet_panel`):
  cards-per-row follows the window's aspect ratio (wide → more columns, tall → more
  rows, ragged grids like 2+1), no longer capped at two per row. The focused-mode
  mini-fleet uses the **same** layout code, so it also packs 2+ cards per row when
  its pane is wide enough (instead of always one per row).
- **Minimum-size gate** shows an ASCII-box placeholder ("Terminal Too Small" with a
  per-dimension "Width/Height Too Narrow/Short" or "Is OK" line) instead of squeezing
  the values; the title/border are yellow and each dimension is green when OK / red
  when short. Degrades to an unframed message when the terminal is narrower than the box.
- **Vertical scrolling** of the fleet: when more card-rows exist than fit the
  height, scroll with `↑`/`↓`, `PgUp`/`PgDn`, or `j`/`k`; a `▲`/`▼` indicator with
  the visible row range shows in the panel title. Works in the main fleet and the
  focused-mode mini-fleet (shared code), re-renders immediately on keypress, and
  never disrupts command typing.
- **Layout numbers are now named module constants** (bar widths, status-column
  width, detail-column widths, summary breakpoints) instead of inline literals; the
  status-column width is *derived* from the canonical `WORKLOAD_STATUS_LABELS` list.

### Fixed
- **Fleet card bars** no longer drift out of alignment when the terminal widens —
  the `real`/`mem`/`pwr` bars are a fixed-width, left-aligned block.
- **Narrow fleet cards stack cleanly**: when a card is too narrow for two detail
  columns, the right column moves *under* the left as a single grid (so values stay
  aligned), and the bars stack vertically to match.
- **Focused-GPU view no longer reflows/jitters** as values change frame to frame:
  the `Status` field is padded to a fixed width, the info grid uses fixed-width
  columns, and the metric table's `Now` column is pinned so it (and the `Bar`
  column after it) don't jump when a value's width changes (e.g. NVLink activity).
- **Summary fields are centered** (both title and value lines).
- **Health badge** (`[OK]`/`[WARN]`/`[HOT]`/`[CRIT]`) is padded to a fixed width and
  the card header no-wraps, so changing health no longer pushes the header onto a
  second line and jitters the card.
- **Focused-GPU view**: the info fields (Status, PCIe RX/TX, NVLink, clocks, …)
  reflow into 4 / 2 / 1 columns by available width instead of jamming, and the
  mini-fleet is dropped (the focused panel goes full width) when the window is too
  narrow to show both.

### Removed
- **The flat `kempner_pulse.py` module** (3038 lines) is gone, superseded by the
  `kempnerpulse` package. This is a **breaking change for importers**: code doing
  `import kempner_pulse` must move to `import kempnerpulse` and the corresponding
  layer submodule. The `kempnerpulse` command itself is unaffected.
- Interim compatibility shims and the pre-rewrite test files used during the
  transition.
