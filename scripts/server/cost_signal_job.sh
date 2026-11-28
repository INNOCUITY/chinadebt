#!/usr/bin/env bash
# Finite CPU-only diagnostic. Arguments: python, paired-label archive, new run directory.
set -euo pipefail
py=$1
archive=$2
run=$3
project=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$project"
mkdir "$run"
trap 'code=$?; if ((code != 0)); then printf "failed: %s\n" "$code" > "$run/STATUS"; fi' EXIT
export PYTHONPATH="$project/src" PYTHONUNBUFFERED=1 CUDA_VISIBLE_DEVICES=""
export OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
printf 'checking\n' > "$run/STATUS"
"$py" - <<'PY'
import json,sys
from pathlib import Path
sys.path.insert(0,'scripts/server')
from ops import inventory,tree_digest
assert tree_digest(inventory(Path('data'))) == json.loads(Path('config/baselines/server-v2.json').read_text())['data_tree_sha256_before']
PY
"$py" -m pytest -o addopts="" -p no:capture -p no:debugging -q --basetemp="$run/test-tmp" \
 tests/test_cost_signal_diagnostic.py tests/test_market_signal_probe.py \
 tests/test_ppo.py::test_gae_and_bounded_density \
 tests/test_ppo.py::test_training_reward_uses_actor_ownership_and_terminal_fees > "$run/tests.log" 2>&1
printf 'running\n' > "$run/STATUS"
pids=()
for fold in 0 10 21; do
 shards=$("$py" -c "import json; print(json.load(open('config/experiments/cost_signal_diagnostic.json'))['shards_per_fold']['$fold'])")
 for band in 2 1; do
  for ((shard=0;shard<shards;shard++)); do
   "$py" scripts/run_cost_signal_diagnostic.py collect --root "$run" --archive "$archive" --fold "$fold" --band "$band" --shard "$shard" --shards "$shards" > "$run/worker-$fold-$band-$shard.log" 2>&1 &
   pids+=("$!")
   printf '%s %s %s %s\n' "$!" "$fold" "$band" "$shard" >> "$run/workers.txt"
  done
 done
done
failed=0
for pid in "${pids[@]}"; do wait "$pid" || failed=1; done
if ((failed)); then exit 1; fi
"$py" scripts/run_cost_signal_diagnostic.py summary --root "$run" --archive "$archive" > "$run/summary.log" 2>&1
printf 'completed\n' > "$run/STATUS"
