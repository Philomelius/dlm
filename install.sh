#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
install_dir=${DLM_INSTALL_DIR:-"$HOME/.local/bin"}

install -d "$install_dir"
install -m 755 "$script_dir/dlm.py" "$install_dir/dlm"

printf 'Installed dlm at %s/dlm\n' "$install_dir"
