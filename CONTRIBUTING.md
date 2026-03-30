# Contributing to KempnerPulse

Thank you for your interest in contributing! Here's how to get started.

## Getting Started

1. Fork the repository and clone your fork.
2. Create a virtual environment and install dependencies:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install rich
   ```

3. Run the dashboard against a live dcgm-exporter endpoint or a saved metrics
   file:

   ```bash
   python3 kempner_pulse.py --source http://localhost:9400/metrics
   python3 kempner_pulse.py --source /path/to/dcgm_metrics.txt
   ```

## Project Structure

KempnerPulse is intentionally a **single-file** application (`kempner_pulse.py`)
with no dependencies beyond Python 3.9+ and `rich`. Please keep it that way
unless there is a compelling reason to split.

```
kempner_pulse.py          # All source code
pyproject.toml            # Package metadata and entry point
README.md                 # User-facing documentation
docs/
  metrics.md              # DCGM metric reference
  classification.md       # Workload classification & health states
  images/                 # Screenshots
```

## Submitting Changes

1. Create a feature branch from `main`.
2. Keep commits focused: one logical change per commit.
3. Make sure the script compiles cleanly:

   ```bash
   python3 -m py_compile kempner_pulse.py
   ```

4. Test interactively: run the dashboard and verify your change in Fleet View,
   Focus View, Plot View, and Job View as applicable.
5. Open a pull request with a clear description of what changed and why.

## Code Style

- Follow existing conventions in the file (PEP 8 with 120-char lines).
- Keep functions focused and well-commented where the logic is non-obvious.
- Use type hints consistent with `from __future__ import annotations`.

## Reporting Issues

Open a GitHub issue with:

- Your Python version (`python3 --version`)
- Your `rich` version (`pip show rich`)
- The GPU model and dcgm-exporter version if relevant
- Steps to reproduce the problem
- Terminal output or a screenshot

## License

By contributing you agree that your contributions will be licensed under the
[MIT License](LICENSE).
