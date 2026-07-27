#!/usr/bin/env bash
# Run the Apibara sold-lot probe scripts.
#
#   ./test/run_sold.sh iaai      # 1 call  — sold IAAI lots + IAAI-only filters
#   ./test/run_sold.sh copart    # 1 call  — sold Copart lots
#   ./test/run_sold.sh generic   # 2 calls — ended lots (both platforms) + one /history
#   ./test/run_sold.sh all       # 4 calls — all three, in that order
#
# EVERY RUN SPENDS LIVE API QUOTA (free plan = 100 requests/month).
# Raw JSON lands in test_run/. Requires APIBARA_API_KEY in the repo-root .env.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="${PYTHON:-python}"

declare -A SCRIPTS=(
  [iaai]="test/test_apibara_sold_iaai_01.py"
  [copart]="test/test_apibara_sold_copart_01.py"
  [generic]="test/test_apibara_sold01.py"
)
declare -A COST=( [iaai]=1 [copart]=1 [generic]=2 )

target="${1:-}"
case "$target" in
  iaai|copart|generic) order=("$target") ;;
  all)                 order=(iaai copart generic) ;;
  *)
    sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac

total=0
for k in "${order[@]}"; do total=$(( total + COST[$k] )); done

if [[ ! -f .env ]]; then
  echo "ERROR: no .env at $ROOT/.env — copy .env.example and set APIBARA_API_KEY" >&2
  exit 1
fi

echo "About to spend ~${total} Apibara API call(s) of your monthly quota."
read -r -p "Continue? [y/N] " reply
[[ "$reply" =~ ^[Yy]$ ]] || { echo "Aborted — no calls made."; exit 0; }

for k in "${order[@]}"; do
  echo
  echo "### ${SCRIPTS[$k]}  (~${COST[$k]} call(s))"
  "$PY" "${SCRIPTS[$k]}"
done

echo
echo "Done. Output written to $ROOT/test_run/"
