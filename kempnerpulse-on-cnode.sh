#!/usr/bin/env bash
set -euo pipefail

print_usage() {
  cat <<EOF
Usage: $0 <compute-node> [kempnerpulse args...]

Options:
  -h, --help              Show this help message and exit.

Environment variables:
  KEMPNERPULSE_VENVPATH   Override the venv used to launch kempnerpulse on the
                          target node. Must point to a venv root directory whose
                          bin/activate exists on that node. On a successful
                          run, the script asks whether to save the override as
                          the new default inside this file.
EOF
}

main() {
  case "${1:-}" in
    -h|--help)
      print_usage
      exit 0
      ;;
  esac

  if [[ $# -lt 1 ]]; then
    print_usage >&2
    exit 1
  fi

  local node="$1"
  shift

  local script_dir script_path venv_activate
  local override_used=0 override_verified=0
  local pre_check_exit=0 ssh_exit=0 reply=""

  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  script_path="$(readlink -f "${BASH_SOURCE[0]}")"

  venv_activate="/n/holylfs06/LABS/kempner_shared/Everyone/common_envs/kempnerpulse/.venv/bin/activate"

  if [[ -n "${KEMPNERPULSE_VENVPATH:-}" ]]; then
    venv_activate="${KEMPNERPULSE_VENVPATH%/}/bin/activate"
    override_used=1
    ssh -o StrictHostKeyChecking=no "$node" "test -f $(printf '%q' "$venv_activate")" || pre_check_exit=$?
    case "$pre_check_exit" in
      0)
        override_verified=1
        ;;
      1)
        echo "kempnerpulse virtual environment not found at ${venv_activate} on ${node} (KEMPNERPULSE_VENVPATH=${KEMPNERPULSE_VENVPATH}); refusing to launch." >&2
        exit 1
        ;;
      *)
        echo "Unable to verify kempnerpulse venv on ${node} (ssh exited ${pre_check_exit}); refusing to launch." >&2
        exit "$pre_check_exit"
        ;;
    esac
  fi

  local remote_cmd
  remote_cmd=$'if ! command -v nvidia-smi >/dev/null 2>&1; then\n'
  remote_cmd+=$'  echo "Target node does not have nvidia-smi available; refusing to launch kempnerpulse." >&2\n'
  remote_cmd+=$'  exit 1\n'
  remote_cmd+=$'fi\n'
  remote_cmd+=$'if ! nvidia-smi -L >/dev/null 2>&1; then\n'
  remote_cmd+=$'  echo "Target node does not appear to have GPUs; refusing to launch kempnerpulse." >&2\n'
  remote_cmd+=$'  exit 1\n'
  remote_cmd+=$'fi\n'
  remote_cmd+="venv_activate=$(printf '%q' "$venv_activate")"$'\n'
  remote_cmd+=$'if [[ ! -f "$venv_activate" ]]; then\n'
  remote_cmd+=$'  echo "kempnerpulse virtual environment not found at $venv_activate; refusing to launch." >&2\n'
  remote_cmd+=$'  exit 1\n'
  remote_cmd+=$'fi\n'
  remote_cmd+="cd $(printf '%q' "$script_dir") && source \"\$venv_activate\" && exec kempnerpulse"
  local arg
  for arg in "$@"; do
    remote_cmd+=" $(printf '%q' "$arg")"
  done

  ssh -tt -o StrictHostKeyChecking=no "$node" bash -lc "$(printf '%q' "$remote_cmd")" || ssh_exit=$?

  if (( override_used )) && (( override_verified )); then
    echo
    read -r -p "Save ${KEMPNERPULSE_VENVPATH} as the new default venv path in ${script_path}? [y/N] " reply || true
    case "$reply" in
      [Yy]|[Yy][Ee][Ss])
        cp "$script_path" "${script_path}.bak"
        if python3 - "$script_path" "$venv_activate" <<'PY'
import sys, pathlib
script, new_activate = sys.argv[1], sys.argv[2]
p = pathlib.Path(script)
lines = p.read_text().splitlines(keepends=True)
replaced = False
for i, line in enumerate(lines):
    stripped = line.lstrip()
    if not replaced and stripped.startswith("venv_activate="):
        indent = line[:len(line) - len(stripped)]
        nl = "\n" if line.endswith("\n") else ""
        lines[i] = f'{indent}venv_activate="{new_activate}"{nl}'
        replaced = True
        break
if not replaced:
    sys.stderr.write("Could not locate venv_activate= line; aborting rewrite.\n")
    sys.exit(2)
p.write_text("".join(lines))
PY
        then
          echo "Default venv path updated. Backup saved at ${script_path}.bak."
        else
          echo "Failed to rewrite ${script_path}; original restored from ${script_path}.bak." >&2
          mv "${script_path}.bak" "$script_path"
        fi
        ;;
      *)
        echo "Default unchanged."
        ;;
    esac
  fi

  exit "$ssh_exit"
}

main "$@"
