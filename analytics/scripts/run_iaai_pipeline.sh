#!/usr/bin/env bash
#
# IAAI pipeline runner — AM (full) and PM (light) passes over five Audi searches.
#
#   analytics/scripts/run_iaai_pipeline.sh am
#   analytics/scripts/run_iaai_pipeline.sh pm
#   analytics/scripts/run_iaai_pipeline.sh am --dry-run
#
# WHY TWO PASSES
# --------------
# The two halves of a lot move on completely different clocks:
#
#   volatile, changes intra-day   listing state, auction date, Buy Now,
#                                 current bid, lots arriving and departing
#   static, fixed for the listing damage, odometer, engine, branch, ACV, photos
#
# A `--details` pull costs one HTTP request PER LOT (~500 across these five
# searches, ~15 min). A search-only pull costs one request per YEAR — 30 total,
# about a minute — and already carries every volatile field.
#
# So AM builds the dataset and PM just tracks movement. PM rows are thin by
# design; data_pull_01's field-level merge fills their static columns from the
# AM archive in the same cohort, so a PM cut is complete even though its own
# pull was not. That merge is what makes this split safe — without it a PM run
# would blank ACV and damage on every row.
#
# COST PER PASS (five searches)
#   AM   ~30 apibara calls + ~530 HTTP    ~20 min
#   PM   ~15 apibara calls +  ~30 HTTP     ~3 min
#
# At 30k apibara calls/month this is ~1,400/month — under 5% of the plan.
set -uo pipefail
cd "$(dirname "$0")/../.."

MODE="${1:-am}"; shift || true
DRY=""; [ "${1:-}" = "--dry-run" ] && DRY="--dry-run"
case "$MODE" in am|pm) ;; *) echo "usage: $0 {am|pm} [--dry-run]"; exit 2 ;; esac

S=analytics/scripts
D=analytics/data/open
DS=analytics/data/sold
STAMP=$(date +%Y%m%dT%H%M%S)
LOG=analytics/data/run_${MODE}_${STAMP}.log

# Ended window: AM looks back 3 months for relist context, PM only needs the
# last few days to catch what sold during the session. Both stay well inside
# Apibara's ~6-month retention wall — a wider range 502s.
if [ "$MODE" = "am" ]; then
    ENDED_FROM=$(date -d "-3 months" +%F); DETAILS="--details"
else
    ENDED_FROM=$(date -d "-7 days"  +%F); DETAILS=""
fi
ENDED_TO=$(date +%F)

# The two pullers slugify a multi-word model DIFFERENTLY -- pull_apibara_01
# writes "rs-5", pull_iaai_web_01 writes "rs_5". Globbing with one slug finds
# zero apibara archives for RS 5 and the enrich step silently degrades to
# web-only (no VIN, no seller, no live bid). Carry both.
# NOT named GROUPS: bash owns that name -- it is a builtin array of the current
# user's Unix group IDs, and assigning to it is silently IGNORED. Doing so made
# this loop iterate over 1000/4/24/27/30/46/100/1001 and search iaai.com for
# "Audi 24", "Audi 27", ... The scope is fixed here and asserted below.
#             model  webslug  apislug  tier
SEARCHES=("A4:a4:a4:2" "S4:s4:s4:1" "A5:a5:a5:2" "S5:s5:s5:1" "RS 5:rs_5:rs-5:1")

# Fail loudly rather than quietly querying nonsense: the scope is exactly these
# five Audi models, and every model must look like a model, not a number.
if [ "${#SEARCHES[@]}" -ne 5 ]; then
    echo "FATAL: expected 5 searches, got ${#SEARCHES[@]} -- variable clobbered?" >&2
    exit 1
fi
for _G in "${SEARCHES[@]}"; do
    case "${_G%%:*}" in
        A4|S4|A5|S5|"RS 5") ;;
        *) echo "FATAL: unexpected model '${_G%%:*}' -- refusing to run" >&2; exit 1 ;;
    esac
done

log () { echo "$@" | tee -a "$LOG"; }

run_group () {
  local MODEL="$1" SLUG="$2" ASLUG="$3" TIER="$4"
  log ""; log "################ Audi $MODEL ($MODE) ################"

  log "--- apibara ENDED  $ENDED_FROM .. $ENDED_TO ---"
  python $S/pull_apibara_01.py iaai ended --make Audi --model $MODEL \
      --year-range 2018-2023 --auction-date-range $ENDED_FROM $ENDED_TO \
      --max-pages 25 2>&1 | grep -E "records:|date span|API call|TRUNCATED" | tee -a "$LOG"

  log "--- iaai_web OPEN ${DETAILS:-(search only)} ---"
  python $S/pull_iaai_web_01.py --make Audi --model $MODEL \
      --year-range 2018-2023 $DETAILS 2>&1 | grep -vE "^        [0-9]+/" \
      | grep -E "lot\(s\)|excluded|records:|states:|Done" | tee -a "$LOG"

  # Open and Live are DISJOINT sets, not subsets — a scheduled lot can sit in
  # either. Pulling only `open` silently loses enrichment for anything mid-sale.
  for SUB in open live; do
    log "--- apibara ${SUB^^} ---"
    python $S/pull_apibara_01.py iaai $SUB --make Audi --model $MODEL \
        --year-range 2018-2023 --max-pages 10 2>&1 \
        | grep -E "records:|API call" | tee -a "$LOG"
  done

  local WEB APIS ADAPTED CUT
  WEB=$(ls -t $D/json-raw/iaai/iaaiweb_iaai_open_audi_${SLUG}_2018_2023_*.json 2>/dev/null | head -1)
  [ -z "$WEB" ] && { log "  !! no web archive for $MODEL, skipping"; return; }
  APIS=$(ls -t $D/json-raw/iaai/apibara_iaai_{open,live}_audi_${ASLUG}_2018-2023_*.json \
                $DS/json-raw/iaai/apibara_iaai_ended_audi_${ASLUG}_2018-2023_*.json 2>/dev/null | head -9)

  [ -z "$APIS" ] && log "  !! no apibara archives matched for $MODEL -- enrichment will be web-only"

  log "--- adapt + enrich ---"
  python $S/iaai_web_adapt_01.py "$WEB" ${APIS:+--enrich-from $APIS} 2>&1 \
      | grep -vE "^  loaded" | grep -E "adapted|enrichment|filled|web-blind" | tee -a "$LOG"

  ADAPTED=$(ls -t $D/json-adapted/iaai/adapted_iaaiweb_iaai_open_audi_${SLUG}_2018_2023_*.json | head -1)

  log "--- csv-raw ---"
  python $S/apibara_json2csv_iaai_01.py "$ADAPTED" 2>&1 | grep -E "row\(s\) x" | tee -a "$LOG"

  log "--- csv-cut (filters + history) ---"
  python $S/data_pull_01.py iaai "$ADAPTED" --tier $TIER \
      --seller-class insurance --exclude-body-style coupe,convertible \
      --max-odometer 100000 --max-distance 3000 --history --history-cache \
      --out audi_${SLUG}_2018-2023_open_ins_nocoupe_noconv_100k_3000mi.csv 2>&1 \
      | grep -vE "^  loaded" | grep -E "history:|unique lots|kept |CSV ->" | tee -a "$LOG"

  CUT=$(ls -t $D/csv-cut/iaai/audi_${SLUG}_2018-2023_open_ins_nocoupe_noconv_100k_3000mi_*.csv | head -1)

  log "--- images (+ archive sold) ---"
  python $S/pull_images_01.py "$(basename "$CUT")" --archive-sold $DRY 2>&1 \
      | grep -vE "^  \[[0-9]+/" | grep -E "archive:|row\(s\) in|image\(s\)|Done|^      " | tee -a "$LOG"
}

log "=============================================================="
log " IAAI pipeline — ${MODE^^} pass — $(date '+%Y-%m-%d %H:%M %Z')"
log " five searches, 2018-2023: A4 S4 A5 S5 RS 5"
log "=============================================================="

for G in "${SEARCHES[@]}"; do
  IFS=: read -r MODEL SLUG ASLUG TIER <<< "$G"
  run_group "$MODEL" "$SLUG" "$ASLUG" "$TIER"
done

log ""; log "=============================================================="
log " summary"
log "=============================================================="
log " apibara calls : $(grep -oE '[0-9]+ API call' "$LOG" | awk '{s+=$1} END {print s+0}')"
log " images new    : $(grep -oE '[0-9]+ downloaded' "$LOG" | awk '{s+=$1} END {print s+0}')"
log " lots archived : $(grep -oE 'moved [0-9]+ departed' "$LOG" | awk '{s+=$2} END {print s+0}')"
log " open tree     : $(find images/open -mindepth 4 -maxdepth 4 -type d 2>/dev/null | wc -l) lots"
log " sold tree     : $(find images/sold -mindepth 4 -maxdepth 4 -type d 2>/dev/null | wc -l) lots"
log " log           : $LOG"

python $S/build_chat_transcript.py >/dev/null 2>&1 || true
