#!/bin/sh
set -eu

cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "Virtual environment not found. Create it and install requirements first." >&2
  exit 1
fi

export DATA_DIR="${DATA_DIR:-$PWD/data}"
unset API_TOKEN
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
exec .venv/bin/python -m autocut_api.local
