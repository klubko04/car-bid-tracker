#!/usr/bin/env bash
# Repeatable Copart pipeline runner for validated 2018-2023 Audi RS5/S5/A5/S4
# cohorts.
#
# A run ID is a checkpoint namespace.  Re-running the same ID resumes and skips
# completed stages.  --pass selects the namespace and how much work is done:
#
#   --pass am    (default)  full chain, one namespace per UTC day at T000000Z
#   --pass pm               refresh chain at T120000Z, reusing the day's AM
#                           sold-side artifacts instead of re-pulling them
#   --pass full             force the full chain in whatever namespace applies
#
# WHY THE PM PASS EXISTS
# ----------------------
# Two things made a same-day second run useless.  The namespace was
# date-only, so every stage found its checkpoint and skipped: the PM run did
# nothing at all.  Forcing a new --run-id went to the other extreme and
# repeated the ended-history pull, which is by far the most expensive stage on
# a metered plan -- measured at 15 calls for S5, 35 for A5, 8 for S4 against a
# 100-call monthly APIBara allowance.
#
# Nothing on the sold side moves between morning and evening: an auction that
# closed yesterday still closed yesterday.  What does move is the open side --
# current bid, buy-now, auction date, and newly listed lots.  The PM pass
# therefore reuses stages 01/05/09/10 from the AM namespace and re-runs only
# the open chain, costing 2 APIBara calls instead of 10-37.
set -Eeuo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$ROOT"

SCRIPTS="$ROOT/analytics/scripts"
DATA="$ROOT/analytics/data"
SOLD_RAW="$DATA/sold/json-raw/copart"
SOLD_ADAPTED="$DATA/sold/json-adapted/copart"
SOLD_CSV_RAW="$DATA/sold/csv-raw/copart"
SOLD_CSV_CUT="$DATA/sold/csv-cut/copart"
OPEN_RAW="$DATA/open/json-raw/copart"
OPEN_ADAPTED="$DATA/open/json-adapted/copart"
OPEN_CSV_RAW="$DATA/open/csv-raw/copart"
OPEN_CSV_CUT="$DATA/open/csv-cut/copart"
COPART_PIPELINE_PYTHON=${COPART_PIPELINE_PYTHON:-python3}

MODEL="S5"
MAKE="Audi"
YEARS="2018-2023"
TIER=1
ENDED_MAX_PAGES=25
APIBARA_EXPECTED_CALLS=17
STATE_MAX_PAGES=10
WEB_MAX_PAGES=20
GALLERY_CAPTURE_SECONDS=45
GALLERY_DELAY_SECONDS=10
GALLERY_WORKERS=1
RUN_ID=""
PASS="am"
MIGRATE_CONFIG=0
ENDED_FROM=""
ENDED_TO=""
DRY_RUN=0

usage() {
    cat <<'EOF'
usage: analytics/scripts/run_copart_pipeline.sh [am|pm|full] [options]

Runs the complete 2018-2023 Audi RS5, S5, A5, or S4 Copart chain:
  APIBara ended -> Copart web open -> APIBara open/live -> vPIC adapters
  -> lot-number merge -> preliminary csv-cut selection -> selected gallery URLs
  -> final csv-raw/csv-cut -> sold/open image lifecycle and download

options:
  --model RS5|S5|A5|S4        exact Audi model (default: S5)
  --pass am|pm|full           am = full chain (default); pm = open-side refresh
                              that reuses the day's AM sold artifacts
  --run-id YYYYMMDDTHHMMSSZ  checkpoint namespace (default: from --pass)
  --ended-from YYYY-MM-DD     ended-window start (default: six months before end)
  --ended-to YYYY-MM-DD       ended-window end (default: today UTC)
  --gallery-workers 1..5      isolated signed-in Chrome tabs (default: 1)
  --migrate-config            adopt a new pipeline config in an EXISTING
                              namespace: keeps the metered APIBara stages that
                              already completed and re-runs only the stages the
                              new config actually changes
  --dry-run                   print the complete plan; no calls or writes
  -h, --help

Re-run with the same run ID to resume idempotently.

Twice-daily cadence, the whole Copart scope in one command:
    run_copart_pipeline.sh am        # morning: full chain, S5 A5 S4 RS5
    run_copart_pipeline.sh pm        # evening: open-side refresh only

One cohort at a time:
    run_copart_pipeline.sh am --model S5
    run_copart_pipeline.sh pm --model RS5

Naming a --model runs only that cohort; omitting it sweeps all four.

The PM pass costs 2 APIBara calls; the AM pass costs 10-37 depending on the
model's ended cohort. Budget accordingly against the 100-call monthly plan.
EOF
}

die() {
    printf 'FATAL: %s\n' "$*" >&2
    exit 1
}

# The IAAI runner takes the pass as a bare first word and sweeps every search
# in one invocation. Match that, because these two are operated together and a
# different calling convention per platform is how a PM pass gets skipped.
# `--pass` still works and still wins if both are given.
# The bare positional form is what triggers a cohort sweep. `--pass am` and a
# plain `--dry-run` keep their existing single-cohort behaviour, so nothing
# that worked before changes shape.
SWEEP=0
if [[ "${1:-}" =~ ^(am|pm|full)$ ]]; then
    PASS=$1; SWEEP=1; shift
fi

# Capture the remaining argv BEFORE parsing consumes it. The cohort sweep
# re-invokes this script and must forward every flag verbatim; reading "$@"
# after the parse loop yields an EMPTY list, which silently dropped --dry-run
# and turned a dry run into a live pull.
SWEEP_ARGS=("$@")

MODEL_EXPLICIT=0
while (($#)); do
    case "$1" in
        --model)
            (($# >= 2)) || die "--model needs a value"
            MODEL=${2^^}; MODEL_EXPLICIT=1; shift 2 ;;
        --pass)
            (($# >= 2)) || die "--pass needs a value"
            PASS=${2,,}; shift 2 ;;
        --run-id)
            (($# >= 2)) || die "--run-id needs a value"
            RUN_ID=$2; shift 2 ;;
        --ended-from)
            (($# >= 2)) || die "--ended-from needs a value"
            ENDED_FROM=$2; shift 2 ;;
        --ended-to)
            (($# >= 2)) || die "--ended-to needs a value"
            ENDED_TO=$2; shift 2 ;;
        --gallery-workers)
            (($# >= 2)) || die "--gallery-workers needs a value"
            GALLERY_WORKERS=$2; shift 2 ;;
        --migrate-config) MIGRATE_CONFIG=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option: $1" ;;
    esac
done

# Cohort sweep. With no --model this script runs each cohort end to end by
# re-invoking itself, so one command covers the whole Copart scope exactly as
# run_iaai_pipeline.sh does. Each cohort keeps its own checkpoint namespace and
# its own lock, so a failure in one does not strand the others -- the sweep
# reports which cohorts failed and exits non-zero.
COPART_COHORTS=(S5 A5 S4 RS5)
if ((SWEEP == 1 && MODEL_EXPLICIT == 0)); then
    sweep_failed=()
    for cohort in "${COPART_COHORTS[@]}"; do
        printf '\n################ Copart %s (%s) ################\n' "$cohort" "$PASS"
        if "$0" --model "$cohort" --pass "$PASS" "${SWEEP_ARGS[@]}"; then
            :
        else
            sweep_failed+=("$cohort")
            printf 'Copart %s FAILED — continuing with the remaining cohorts\n' \
                "$cohort" >&2
        fi
    done
    if ((${#sweep_failed[@]})); then
        printf '\nsweep finished with failures: %s\n' "${sweep_failed[*]}" >&2
        exit 1
    fi
    printf '\nsweep complete: %s\n' "${COPART_COHORTS[*]}"
    exit 0
fi

case "$PASS" in
    am|full) DEFAULT_RUN_ID=$(date -u +%Y%m%dT000000Z) ;;
    pm)      DEFAULT_RUN_ID=$(date -u +%Y%m%dT120000Z) ;;
    *) die "--pass must be am, pm, or full" ;;
esac
RUN_ID=${RUN_ID:-$DEFAULT_RUN_ID}
# The AM namespace of the same UTC day is where a PM pass looks for the
# sold-side artifacts it intends to reuse.
AM_RUN_ID="${RUN_ID%T*}T000000Z"
[[ "$PASS" == "pm" && "$RUN_ID" == "$AM_RUN_ID" ]] &&
    die "--pass pm needs a namespace distinct from the AM run ($AM_RUN_ID)"
[[ "$RUN_ID" =~ ^[0-9]{8}T[0-9]{6}Z$ ]] ||
    die "--run-id must be an exact UTC timestamp: YYYYMMDDTHHMMSSZ"
ENDED_TO=${ENDED_TO:-$(date -u +%F)}
ENDED_FROM=${ENDED_FROM:-$(date -u -d "$ENDED_TO -6 months" +%F)}
for value in "$ENDED_FROM" "$ENDED_TO"; do
    [[ "$value" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] ||
        die "ended dates must be YYYY-MM-DD, got: $value"
    date -u -d "$value" +%F >/dev/null 2>&1 || die "invalid date: $value"
done
[[ "$ENDED_FROM" < "$ENDED_TO" ]] || die "ended start must precede ended end"
[[ "$GALLERY_WORKERS" =~ ^[1-5]$ ]] || die "--gallery-workers must be between 1 and 5"

# Keep expansion explicit. Other models still require a separate audited
# decision before the runner accepts them.
[[ "$MAKE" == "Audi" && "$YEARS" == "2018-2023" ]] ||
    die "scope changed outside Audi 2018-2023"
case "$MODEL" in
    RS5|S5|A5|S4) ;;
    *) die "--model must be RS5, S5, A5, or S4" ;;
esac
MODEL_SLUG=${MODEL,,}
FINAL_BODY_FILTERS=()
CUT_QUALIFIER=""

# stat.vin's model <select> values, read off its own search page. A BARE NAME
# IS NOT A VALID SUBSTITUTE: "A5" happens to match, "S5" silently returns an
# empty results page with no error, which looks exactly like an empty cohort.
#
# RS5 has no value of its own. stat.vin groups it the way Copart does, under
# S5/RS5, so an RS5 search returns nothing and the S5 group returns both --
# 6 of 17 S5-group lots were RS5 in the 2026-08-20 pull. RS5 therefore points
# at the S5 group on purpose; the enricher joins on Copart lot number and
# validates year + VIN prefix, so the mixed group cannot contaminate a cohort.
declare -A STATVIN_MODELS=(
    [S5]="S5_group_id_24870"
    [A5]="A5_group_id_24918"
    [S4]="S4_group_id_24878"
    [RS5]="S5_group_id_24870"
)
STATVIN_MODEL="${STATVIN_MODELS[$MODEL]:-}"
[[ -n "$STATVIN_MODEL" ]] ||
    die "no stat.vin model value registered for $MODEL"

# Dealer consignments are trade-in stock a retailer already declined to
# retail. They are kept in canonical csv-raw and dropped from the final cut,
# which also keeps them out of gallery capture -- the gallery stage takes its
# lot list from the selection CSV.
SELLER_EXCLUSIONS=(--exclude-seller-class dealer)
if [[ "$MODEL" == "A5" ]]; then
    # The first bounded A5 validation exhausted the S5-sized 25-page cap at
    # 500 records and only reached 2026-04-07 in a 2026-02-19..08-19 window.
    ENDED_MAX_PAGES=50
    APIBARA_EXPECTED_CALLS=40
    TIER=2
    # A5 is the Sportback cohort. Preserve every observation in canonical
    # csv-raw, then exclude the Coupe/Convertible families from final history,
    # image lifecycle, and downstream analysis artifacts.
    FINAL_BODY_FILTERS=(--exclude-body-style coupe,convertible)
    CUT_QUALIFIER="_nocoupe_noconv"
fi
if [[ "$MODEL" == "RS5" ]]; then
    # RS5 is the smallest cohort by a wide margin: a live Copart web probe on
    # 2026-08-19 returned 26 open lots for 2018-2023 (5/13/0/3/2/3 by year),
    # against 69 for S5 and 204 for A5. Copart's exact model description is
    # "RS5" with no space -- note this differs from IAAI, where the same car is
    # "RS 5"; the shared model group "S5/RS5" is what makes the exact MODL
    # facet necessary in the first place.
    #
    # A small cohort needs a small cap. The runner still fails closed if
    # APIBara reports truncation, so an undersized cap surfaces as an error
    # rather than a quietly partial archive.
    ENDED_MAX_PAGES=15
    APIBARA_EXPECTED_CALLS=8
    TIER=1
    # RS5 ships as Coupe and Sportback. Keep every observation in canonical
    # csv-raw and exclude the coupe/convertible families from the final cut,
    # matching the A5/S4 treatment.
    FINAL_BODY_FILTERS=(--exclude-body-style coupe,convertible)
    CUT_QUALIFIER="_nocoupe_noconv"
fi
if [[ "$MODEL" == "S4" ]]; then
    # S4 is the sedan cohort. A generous cap prevents a partial six-month
    # archive; the runner still fails closed if APIBara reports truncation.
    ENDED_MAX_PAGES=50
    APIBARA_EXPECTED_CALLS=35
    TIER=1
    FINAL_BODY_FILTERS=(--exclude-body-style coupe,convertible)
    CUT_QUALIFIER="_nocoupe_noconv"
fi
APIBARA_HARD_CAP=$((ENDED_MAX_PAGES + STATE_MAX_PAGES + STATE_MAX_PAGES))

ENDED_RAW="$SOLD_RAW/apibara_copart_ended_audi_${MODEL_SLUG}_2018-2023_${ENDED_FROM}_${ENDED_TO}_${RUN_ID}.json"
WEB_RAW="$OPEN_RAW/copartweb_copart_open_audi_${MODEL_SLUG}_2018_2023_${RUN_ID}.json"
OPEN_API_RAW="$OPEN_RAW/apibara_copart_open_audi_${MODEL_SLUG}_2018-2023_${RUN_ID}.json"
LIVE_API_RAW="$OPEN_RAW/apibara_copart_live_audi_${MODEL_SLUG}_2018-2023_${RUN_ID}.json"
ENDED_VPIC="$SOLD_ADAPTED/vpic_$(basename "$ENDED_RAW")"
OPEN_VPIC="$OPEN_ADAPTED/vpic_$(basename "$OPEN_API_RAW")"
LIVE_VPIC="$OPEN_ADAPTED/vpic_$(basename "$LIVE_API_RAW")"
WEB_ADAPTED="$OPEN_ADAPTED/adapted_$(basename "$WEB_RAW")"
STATVIN_RAW="$OPEN_RAW/statvin_copart_open_audi_${MODEL_SLUG}_2018_2023_${RUN_ID}.json"
WEB_ENRICHED="$OPEN_ADAPTED/statvin_$(basename "$WEB_ADAPTED")"
MEDIA_REUSED="$OPEN_ADAPTED/images_$(basename "$WEB_ENRICHED")"
MEDIA_BROWSER="$OPEN_ADAPTED/browser_$(basename "$WEB_ENRICHED")"
SOLD_RAW_CSV="$SOLD_CSV_RAW/audi_${MODEL_SLUG}_2018-2023_ended_${RUN_ID}_copart.csv"
OPEN_RAW_CSV="$OPEN_CSV_RAW/audi_${MODEL_SLUG}_2018-2023_open_${RUN_ID}_copart.csv"
SOLD_CUT="$SOLD_CSV_CUT/audi_${MODEL_SLUG}_2018-2023_ended_history${CUT_QUALIFIER}_${RUN_ID}.csv"
OPEN_CUT="$OPEN_CSV_CUT/audi_${MODEL_SLUG}_2018-2023_open_history${CUT_QUALIFIER}_${RUN_ID}.csv"
RUN_DIR="$DATA/runs/copart/$MODEL_SLUG/$RUN_ID"
OPEN_SELECTION="$RUN_DIR/audi_${MODEL_SLUG}_2018-2023_open_selection${CUT_QUALIFIER}_${RUN_ID}.csv"
LOG="$RUN_DIR/run.log"
STARTED_AT=$(date --iso-8601=seconds)

# ---------------------------------------------------------------------------
# PM pass: inherit the sold side instead of re-buying it.
#
# Stages 01/05/09/10 describe auctions that already closed. They cannot change
# between an AM and a PM run on the same day, and stage 01 is the single most
# expensive call on the plan (measured: S5 15, A5 35, S4 8 calls against a
# 100-call month). The PM pass repoints those four artifacts at a recent AM
# namespace and lets run_stage's own validators decide whether they are usable.
#
# The lookback is deliberately NOT "same UTC date". The operator runs in
# US/Pacific, where an evening PM run is already the next day in UTC, so a
# same-date rule would find no AM run and silently re-buy the ended pull --
# exactly the cost this pass exists to avoid. Instead: prefer today's AM
# namespace, else the most recent AM namespace whose artifacts are younger
# than SOLD_MAX_AGE_HOURS.
# ---------------------------------------------------------------------------
SOLD_MAX_AGE_HOURS=${SOLD_MAX_AGE_HOURS:-36}
SOLD_INHERITED=0
SOLD_INHERITED_FROM=""

pm_try_inherit() {
    local candidate=$1 ended vpic raw_csv cut age_hours
    ended=$(ls -1 "$SOLD_RAW"/apibara_copart_ended_audi_"${MODEL_SLUG}"_2018-2023_*_"${candidate}".json \
        2>/dev/null | head -1)
    [[ -s "$ended" ]] || return 1
    vpic="$SOLD_ADAPTED/vpic_$(basename "$ended")"
    raw_csv="$SOLD_CSV_RAW/audi_${MODEL_SLUG}_2018-2023_ended_${candidate}_copart.csv"
    cut="$SOLD_CSV_CUT/audi_${MODEL_SLUG}_2018-2023_ended_history${CUT_QUALIFIER}_${candidate}.csv"
    [[ -s "$vpic" && -s "$raw_csv" && -s "$cut" ]] || return 1
    age_hours=$(( ( $(date +%s) - $(stat -c %Y "$ended") ) / 3600 ))
    ((age_hours <= SOLD_MAX_AGE_HOURS)) || return 1
    ENDED_RAW="$ended"; ENDED_VPIC="$vpic"
    SOLD_RAW_CSV="$raw_csv"; SOLD_CUT="$cut"
    SOLD_INHERITED=1
    SOLD_INHERITED_FROM="$candidate (${age_hours}h old)"
    return 0
}

if [[ "$PASS" == "pm" ]]; then
    if ! pm_try_inherit "$AM_RUN_ID"; then
        while read -r candidate; do
            [[ -n "$candidate" ]] || continue
            pm_try_inherit "$candidate" && break
        done < <(ls -1 "$DATA/runs/copart/$MODEL_SLUG" 2>/dev/null |
                 grep -E '^[0-9]{8}T000000Z$' | sort -r)
    fi
fi


print_command() {
    printf '  '
    printf '%q ' "$@"
    printf '\n'
}

print_budget() {
    local ended_line="ended <= $ENDED_MAX_PAGES, open <= $STATE_MAX_PAGES, live <= $STATE_MAX_PAGES"
    local expected="$APIBARA_EXPECTED_CALLS"
    if ((SOLD_INHERITED)); then
        expected="2 (open + live)"
        ended_line="ended: INHERITED from $SOLD_INHERITED_FROM — not re-pulled"
    elif [[ "$PASS" == "pm" ]]; then
        ended_line="ended <= $ENDED_MAX_PAGES (no reusable AM run found — FULL cost), open/live <= $STATE_MAX_PAGES"
    fi
    cat <<EOF
Call-budget estimate ($MODEL, --pass $PASS)
  APIBara: expected ~$expected calls; hard cap $APIBARA_HARD_CAP
    $ended_line
  Copart web search: expected 6 calls (one/year); hard cap 120
  NHTSA vPIC: cache misses / 50, calculated after each raw APIBara pull
  signed-in galleries: one browser page per csv-cut-selected incomplete lot
    (body-style exclusions happen before gallery requests)
    workers: $GALLERY_WORKERS isolated tab(s), shared signed-in Chrome profile
  image CDN: one request per missing local image; existing non-empty files skip
EOF
}

print_plan() {
    printf 'Copart %s pipeline DRY RUN — %s (--pass %s)\n' "$MODEL" "$RUN_ID" "$PASS"
    if [[ "$PASS" == "pm" ]]; then
        if ((SOLD_INHERITED)); then
            printf 'PM pass: stages 01/05/09/10 inherited from %s — SKIPPED\n' \
                "$SOLD_INHERITED_FROM"
        else
            printf 'PM pass: no usable AM sold artifacts within %sh — FULL chain\n' \
                "$SOLD_MAX_AGE_HOURS"
        fi
    fi
    printf 'Window: %s through %s | scope: %s %s %s\n\n' \
        "$ENDED_FROM" "$ENDED_TO" "$YEARS" "$MAKE" "$MODEL"
    print_budget
    printf '\n01 apibara-ended\n'
    print_command "$COPART_PIPELINE_PYTHON" "$SCRIPTS/pull_apibara_01.py" copart ended \
        --make "$MAKE" --model "$MODEL" --year-range "$YEARS" \
        --auction-date-range "$ENDED_FROM" "$ENDED_TO" \
        --max-pages "$ENDED_MAX_PAGES" --out "$ENDED_RAW"
    printf '02 copart-web-open\n'
    print_command "$COPART_PIPELINE_PYTHON" "$SCRIPTS/pull_copart_web_01.py" \
        --make "$MAKE" --model "$MODEL" --year-range "$YEARS" \
        --max-pages "$WEB_MAX_PAGES" --out "$WEB_RAW"
    printf '03 apibara-open\n'
    print_command "$COPART_PIPELINE_PYTHON" "$SCRIPTS/pull_apibara_01.py" copart open \
        --make "$MAKE" --model "$MODEL" --year-range "$YEARS" \
        --max-pages "$STATE_MAX_PAGES" --out "$OPEN_API_RAW"
    printf '04 apibara-live\n'
    print_command "$COPART_PIPELINE_PYTHON" "$SCRIPTS/pull_apibara_01.py" copart live \
        --make "$MAKE" --model "$MODEL" --year-range "$YEARS" \
        --max-pages "$STATE_MAX_PAGES" --out "$LIVE_API_RAW"
    printf '05-07 vPIC adapters (ended/open/live)\n'
    print_command "$COPART_PIPELINE_PYTHON" "$SCRIPTS/copart_vpic_adapt_01.py" \
        "$ENDED_RAW" --out "$ENDED_VPIC"
    print_command "$COPART_PIPELINE_PYTHON" "$SCRIPTS/copart_vpic_adapt_01.py" \
        "$OPEN_API_RAW" --out "$OPEN_VPIC"
    print_command "$COPART_PIPELINE_PYTHON" "$SCRIPTS/copart_vpic_adapt_01.py" \
        "$LIVE_API_RAW" --out "$LIVE_VPIC"
    printf '08 lot-number merge\n'
    print_command "$COPART_PIPELINE_PYTHON" "$SCRIPTS/copart_web_adapt_01.py" \
        "$WEB_RAW" --enrich-from "$OPEN_VPIC" "$LIVE_VPIC" "$ENDED_VPIC" \
        --audit --out "$WEB_ADAPTED"
    printf '08a stat.vin seller/VIN pull + 08b enrich (before the cut)\n'
    print_command "$COPART_PIPELINE_PYTHON" "$SCRIPTS/pull_statvin_web_01.py" \
        --make "$MAKE" --model "$STATVIN_MODEL" --year-range "$YEARS" \
        --out "$STATVIN_RAW"
    print_command "$COPART_PIPELINE_PYTHON" "$SCRIPTS/copart_statvin_enrich_01.py" \
        "$WEB_ADAPTED" --statvin "$STATVIN_RAW" --out "$WEB_ENRICHED"
    printf '09 sold csv-raw + 10 sold history csv-cut\n'
    print_command "$COPART_PIPELINE_PYTHON" "$SCRIPTS/apibara_json2csv_copart_01.py" \
        "$ENDED_VPIC" --out "$SOLD_RAW_CSV"
    print_command "$COPART_PIPELINE_PYTHON" "$SCRIPTS/data_pull_01.py" copart \
        "$ENDED_VPIC" --tier "$TIER" --sold-only "${FINAL_BODY_FILTERS[@]}" \
        "${SELLER_EXCLUSIONS[@]}" \
        --history --history-cache --out "$SOLD_CUT"
    printf '11 preliminary open csv-cut selection (cheap; before gallery calls)\n'
    print_command "$COPART_PIPELINE_PYTHON" "$SCRIPTS/data_pull_01.py" copart \
        "$WEB_ENRICHED" --tier "$TIER" "${FINAL_BODY_FILTERS[@]}" \
        "${SELLER_EXCLUSIONS[@]}" \
        --out "$OPEN_SELECTION"
    printf '12-13 selected gallery reuse/browser completion\n'
    printf '  media reuse sources are discovered at run time; remaining selected lots use\n'
    print_command "$COPART_PIPELINE_PYTHON" "$SCRIPTS/copart_browser_enrich_01.py" \
        "$MEDIA_REUSED" --lots-from-csv "$OPEN_SELECTION" \
        --all-incomplete --max-lots 0 \
        --capture-seconds "$GALLERY_CAPTURE_SECONDS" \
        --delay "$GALLERY_DELAY_SECONDS" --workers "$GALLERY_WORKERS" \
        --out "$MEDIA_BROWSER"
    printf '14 final open csv-raw + 15 final open history csv-cut\n'
    print_command "$COPART_PIPELINE_PYTHON" "$SCRIPTS/apibara_json2csv_copart_01.py" \
        '<selected-completed-media.json>' --out "$OPEN_RAW_CSV"
    print_command "$COPART_PIPELINE_PYTHON" "$SCRIPTS/data_pull_01.py" copart \
        '<selected-completed-media.json>' --tier "$TIER" "${FINAL_BODY_FILTERS[@]}" \
        "${SELLER_EXCLUSIONS[@]}" \
        --history --history-cache --out "$OPEN_CUT"
    printf '16 sold/open image lifecycle + selected image download\n'
    print_command "$COPART_PIPELINE_PYTHON" "$SCRIPTS/pull_images_01.py" "$OPEN_CUT" \
        --platform copart --archive-sold --model-folder "$MAKE $MODEL"
    printf '\nNo files, browser sessions, API calls, or image downloads were made.\n'
}

if ((DRY_RUN)); then
    print_plan
    exit 0
fi

"$COPART_PIPELINE_PYTHON" -c 'import httpx' >/dev/null 2>&1 ||
    die "$COPART_PIPELINE_PYTHON cannot import httpx; activate/install requirements.txt or set COPART_PIPELINE_PYTHON to the project environment"

mkdir -p "$RUN_DIR" "$SOLD_RAW" "$SOLD_ADAPTED" "$SOLD_CSV_RAW" \
    "$SOLD_CSV_CUT" "$OPEN_RAW" "$OPEN_ADAPTED" "$OPEN_CSV_RAW" "$OPEN_CSV_CUT"
RUN_STARTED_AT_FILE="$RUN_DIR/started_at"
if [[ -s "$RUN_STARTED_AT_FILE" ]]; then
    read -r STARTED_AT < "$RUN_STARTED_AT_FILE"
else
    printf '%s\n' "$STARTED_AT" > "$RUN_STARTED_AT_FILE"
fi
exec 9>"$RUN_DIR/run.lock"
flock -n 9 || die "run $RUN_ID is already active"

# The fingerprint exists to stop a namespace being reused for a DIFFERENT
# DATASET. It therefore covers only inputs that change what the run produces:
# scope, tier, ended window, page caps, and the cut filters.
#
# Gallery capture-seconds / delay / worker count are deliberately NOT in it.
# They change how fast the browser stage runs, never what it returns, and
# including them meant `--gallery-workers 5` collided with a namespace created
# at the default 1 and aborted three cohorts out of four with
# "already exists with different dates/config" -- a message that named neither
# the field nor the value.
CONFIG="version=5|scope=$MAKE-$MODEL-2018-2023|tier=$TIER"
CONFIG="$CONFIG|ended=$ENDED_FROM:$ENDED_TO"
CONFIG="$CONFIG|caps=$ENDED_MAX_PAGES:$STATE_MAX_PAGES:$WEB_MAX_PAGES"
CONFIG="$CONFIG|cut=${FINAL_BODY_FILTERS[*]:-none}"
CONFIG="$CONFIG|seller_cut=${SELLER_EXCLUSIONS[*]:-none}"
CONFIG="$CONFIG|statvin=$STATVIN_MODEL|gallery_after_cut=true"
CONFIG_SHA=$(printf '%s' "$CONFIG" | sha256sum | awk '{print $1}')
if [[ -f "$RUN_DIR/config.sha256" ]]; then
    read -r SAVED_CONFIG < "$RUN_DIR/config.sha256"
    if [[ "$SAVED_CONFIG" != "$CONFIG_SHA" && "$MIGRATE_CONFIG" == "1" ]]; then
        # Stages 01-09 read only the scope/window/caps, which a cut or
        # stat.vin change does not touch, so their artifacts stay valid and
        # their metered APIBara calls are not spent twice. Everything from the
        # stat.vin pull onward depends on the new config and is re-run.
        for stage_key in 08a-statvin-pull 08b-statvin-enrich 10-history-sold \
                         11-open-selection 12-gallery-reuse 13-gallery-browser \
                         14-csv-raw-open 15-history-open 16-images; do
            rm -f "$RUN_DIR/$stage_key.done"
        done
        printf '%s\n' "$CONFIG_SHA" > "$RUN_DIR/config.sha256"
        printf '%s\n' "$CONFIG" > "$RUN_DIR/config.txt"
        SAVED_CONFIG="$CONFIG_SHA"
        log_pending_migration=1
    fi
    if [[ "$SAVED_CONFIG" != "$CONFIG_SHA" ]]; then
        # Say what actually differs. "choose a new --run-id" on its own sends
        # the operator hunting through the script for the field that moved.
        printf 'FATAL: run ID %s already holds a different dataset.\n' "$RUN_ID" >&2
        if [[ -f "$RUN_DIR/config.txt" ]]; then
            read -r SAVED_CONFIG_TEXT < "$RUN_DIR/config.txt"
            printf '  stored: %s\n  wanted: %s\n' "$SAVED_CONFIG_TEXT" "$CONFIG" >&2
            "$COPART_PIPELINE_PYTHON" - "$SAVED_CONFIG_TEXT" "$CONFIG" >&2 <<'PY'
import sys
stored = dict(p.split("=", 1) for p in sys.argv[1].split("|") if "=" in p)
wanted = dict(p.split("=", 1) for p in sys.argv[2].split("|") if "=" in p)
for key in sorted(set(stored) | set(wanted)):
    if stored.get(key) != wanted.get(key):
        print(f"  differs: {key}: {stored.get(key)!r} -> {wanted.get(key)!r}")
PY
        fi
        printf '  fix: --migrate-config keeps the completed APIBara stages and\n' >&2
        printf '       re-runs only what the new config changes, or\n' >&2
        printf '       --run-id %s starts a clean namespace (re-spends quota)\n' \
            "$(date -u +%Y%m%dT%H%M%SZ)" >&2
        exit 1
    fi
else
    printf '%s\n' "$CONFIG_SHA" > "$RUN_DIR/config.sha256"
    printf '%s\n' "$CONFIG" > "$RUN_DIR/config.txt"
fi

touch "$LOG"
log() {
    printf '[%s] %s\n' "$(date --iso-8601=seconds)" "$*" | tee -a "$LOG"
}

validate_apibara() {
    local path=$1 expected_mode=$2 require_rows=$3
    "$COPART_PIPELINE_PYTHON" - "$path" "$expected_mode" "$require_rows" <<'PY'
import json, sys
path, mode, require_rows = sys.argv[1], sys.argv[2], int(sys.argv[3])
d = json.load(open(path, encoding="utf-8"))
assert d.get("platform") == "copart" and d.get("mode") == mode
assert d.get("pages"), "no APIBara response pages"
assert all(p.get("status") == 200 for p in d["pages"]), "non-200 APIBara page"
assert not (d.get("counts") or {}).get("truncated"), "APIBara archive truncated"
assert not require_rows or (d.get("counts") or {}).get("records", 0) > 0, "empty required cohort"
PY
}

validate_apibara_ended() { validate_apibara "$1" ended 1; }
validate_apibara_open() { validate_apibara "$1" open 0; }
validate_apibara_live() { validate_apibara "$1" live 0; }

validate_web_raw() {
    "$COPART_PIPELINE_PYTHON" - "$1" "$MODEL" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
model = sys.argv[2]
assert d.get("platform") == "copart" and d.get("source") == "copart-web"
assert d.get("mode") == "open" and len(d.get("queries") or []) == 6
assert str((d.get("search_params") or {}).get("model") or "").upper() == model
c = d.get("counts") or {}
assert c.get("records", 0) > 0, "empty web cohort"
assert not c.get("truncated") and not c.get("failed_queries"), "incomplete web snapshot"
assert all(p.get("status") == 200 for q in d["queries"] for p in q.get("pages", []))
PY
}

validate_vpic() {
    "$COPART_PIPELINE_PYTHON" - "$1" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
assert d.get("platform") == "copart"
a = d.get("adapter") or {}
assert a.get("name") == "copart_vpic_adapt_01"
assert (a.get("market_scope") or {}).get("policy") == "us_only"
PY
}

validate_statvin_raw() {
    local artifact=$1
    [[ -s "$artifact" ]] || return 1
    "$COPART_PIPELINE_PYTHON" - "$artifact" <<'PY'
import json, sys
document = json.loads(open(sys.argv[1], encoding="utf-8").read())
records = document.get("records") or []
counts = document.get("counts") or {}
if not records:
    raise SystemExit("stat.vin archive has no records")
if counts.get("truncated"):
    raise SystemExit("stat.vin archive is truncated; raise --max-pages")
# A record without a lot number cannot join, and one without a VIN cannot
# complete a mask -- either means the card contract moved.
if not all(r.get("lot_number") for r in records):
    raise SystemExit("stat.vin records are missing lot numbers")
PY
}

validate_web_adapted() {
    "$COPART_PIPELINE_PYTHON" - "$1" "$MODEL" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
model = sys.argv[2]
assert d.get("platform") == "copart" and d.get("source") == "copart-web-adapted"
c = d.get("counts") or {}
assert c.get("records", 0) > 0 and not c.get("truncated")
assert (d.get("adapter") or {}).get("market_scope", {}).get("policy") == "us_only"
records = [r for p in d.get("pages") or [] for r in (p.get("raw") or {}).get("data") or []]
assert records and all(str(r.get("model") or "").upper() == model for r in records)
PY
}

validate_media_complete() {
    "$COPART_PIPELINE_PYTHON" - "$1" "$SCRIPTS" <<'PY'
import json, sys
sys.path.insert(0, sys.argv[2])
import copart_image_enrich_01 as media
d = json.load(open(sys.argv[1], encoding="utf-8"))
pending = [str(r.get("lot_number")) for r in media.records(d)
           if media.needs_gallery_capture(r)]
assert not pending, f"{len(pending)} incomplete galleries remain: {pending[:8]}"
PY
}

validate_csv() {
    "$COPART_PIPELINE_PYTHON" - "$1" "$MODEL" <<'PY'
import csv, sys
with open(sys.argv[1], encoding="utf-8", newline="") as stream:
    rows = list(csv.DictReader(stream))
assert rows, "CSV has no data rows"
assert len({r.get("lot_number") for r in rows}) == len(rows), "duplicate lot rows"
assert all((r.get("platform") or "copart").lower() == "copart" for r in rows)
assert not any((r.get("market") or "").lower() == "canada" for r in rows), "Canada leaked into CSV"
assert all((r.get("model") or "").upper() == sys.argv[2] for r in rows), "wrong model leaked into CSV"
PY
}

validate_final_csv() {
    validate_csv "$@" || return
    ((${#FINAL_BODY_FILTERS[@]})) || return 0
    "$COPART_PIPELINE_PYTHON" - "$1" <<'PY'
import csv, re, sys
with open(sys.argv[1], encoding="utf-8", newline="") as stream:
    rows = list(csv.DictReader(stream))
leaks = []
for row in rows:
    style = str(row.get("body_style") or "").casefold()
    if re.search(r"\b(coupe|convertible|cabriolet)\b", style):
        leaks.append((row.get("lot_number"), row.get("body_style")))
assert not leaks, f"final CSV contains Coupe/Convertible lots: {leaks[:8]}"
PY
}

validate_image_stage() {
    local artifact=$1 stage_log=$2
    [[ -s "$artifact" ]] || return 1
    grep -Eq 'Done\..*, 0 failed' "$stage_log"
}

run_stage() {
    local key=$1 artifact=$2 validator=$3
    shift 3
    local marker="$RUN_DIR/$key.done" stage_log="$RUN_DIR/$key.log" rc stage_started elapsed
    if [[ -f "$marker" ]]; then
        "$validator" "$artifact" "$stage_log" ||
            die "$key checkpoint exists but its artifact no longer validates"
        log "SKIP $key — completed checkpoint is valid"
        return 0
    fi
    log "START $key"
    stage_started=$(date +%s)
    : > "$stage_log"
    if "$@" 2>&1 | tee -a "$LOG" "$stage_log"; then
        :
    else
        rc=${PIPESTATUS[0]}
        die "$key failed with exit $rc; resume with --run-id $RUN_ID"
    fi
    "$validator" "$artifact" "$stage_log" ||
        die "$key returned success but failed artifact validation"
    elapsed=$(( $(date +%s) - stage_started ))
    {
        printf 'completed_at=%s\n' "$(date --iso-8601=seconds)"
        printf 'artifact=%s\n' "$artifact"
        printf 'sha256=%s\n' "$(sha256sum "$artifact" | awk '{print $1}')"
        printf 'elapsed_seconds=%s\n' "$elapsed"
    } > "$marker.tmp"
    mv "$marker.tmp" "$marker"
    log "DONE  $key (${elapsed}s)"
}

pending_gallery_count() {
    "$COPART_PIPELINE_PYTHON" - "$1" "$2" "$SCRIPTS" <<'PY'
import json, sys
document, selection, scripts = sys.argv[1:]
sys.path.insert(0, scripts)
import copart_image_enrich_01 as media
d = json.load(open(document, encoding="utf-8"))
allowed = set(media.lot_numbers_from_csv(selection))
print(sum(media.normalize_lot(r.get("lot_number")) in allowed and
          media.needs_gallery_capture(r) for r in media.records(d)))
PY
}

validate_selected_media() {
    "$COPART_PIPELINE_PYTHON" - "$1" "$OPEN_SELECTION" "$SCRIPTS" <<'PY'
import json, sys
document, selection, scripts = sys.argv[1:]
sys.path.insert(0, scripts)
import copart_image_enrich_01 as media
d = json.load(open(document, encoding="utf-8"))
indexed = {media.normalize_lot(r.get("lot_number")): r for r in media.records(d)}
selected = media.lot_numbers_from_csv(selection)
missing = [lot for lot in selected if lot not in indexed]
pending = [lot for lot in selected if lot in indexed and
           media.needs_gallery_capture(indexed[lot])]
assert not missing, f"{len(missing)} selected lots absent from media JSON: {missing[:8]}"
assert not pending, f"{len(pending)} selected galleries incomplete: {pending[:8]}"
PY
}

log "Copart pipeline run $RUN_ID started at $STARTED_AT"
if [[ "${log_pending_migration:-0}" == "1" ]]; then
    log "config migrated to v5 in place — stages 01-09 kept, 08a onward re-run"
fi
if [[ "$PASS" == "pm" ]]; then
    if ((SOLD_INHERITED)); then
        for stage_key in 01-apibara-ended 05-vpic-ended 09-csv-raw-sold 10-history-sold; do
            [[ -f "$RUN_DIR/$stage_key.done" ]] && continue
            printf 'completed_at=%s\ninherited_from=%s\nnote=pm pass reuses an AM sold-side artifact\nelapsed_seconds=0\n' \
                "$(date --iso-8601=seconds)" "$SOLD_INHERITED_FROM" > "$RUN_DIR/$stage_key.done"
        done
        log "PM pass — sold side inherited from $SOLD_INHERITED_FROM"
        log "         saves ~$APIBARA_EXPECTED_CALLS APIBara call(s); this pass costs ~2"
    else
        log "PM pass — no usable AM sold artifacts within ${SOLD_MAX_AGE_HOURS}h; running the FULL chain"
        log "         this will spend ~$APIBARA_EXPECTED_CALLS APIBara call(s) on ended history"
    fi
fi
log "scope=$YEARS $MAKE $MODEL ended=$ENDED_FROM..$ENDED_TO"
print_budget | tee -a "$LOG"

run_stage 01-apibara-ended "$ENDED_RAW" \
    "validate_apibara_ended" \
    "$COPART_PIPELINE_PYTHON" "$SCRIPTS/pull_apibara_01.py" copart ended \
    --make "$MAKE" --model "$MODEL" --year-range "$YEARS" \
    --auction-date-range "$ENDED_FROM" "$ENDED_TO" \
    --max-pages "$ENDED_MAX_PAGES" --out "$ENDED_RAW"

run_stage 02-copart-web-open "$WEB_RAW" validate_web_raw \
    "$COPART_PIPELINE_PYTHON" "$SCRIPTS/pull_copart_web_01.py" \
    --make "$MAKE" --model "$MODEL" --year-range "$YEARS" \
    --max-pages "$WEB_MAX_PAGES" --out "$WEB_RAW"

run_stage 03-apibara-open "$OPEN_API_RAW" validate_apibara_open \
    "$COPART_PIPELINE_PYTHON" "$SCRIPTS/pull_apibara_01.py" copart open \
    --make "$MAKE" --model "$MODEL" --year-range "$YEARS" \
    --max-pages "$STATE_MAX_PAGES" --out "$OPEN_API_RAW"

run_stage 04-apibara-live "$LIVE_API_RAW" validate_apibara_live \
    "$COPART_PIPELINE_PYTHON" "$SCRIPTS/pull_apibara_01.py" copart live \
    --make "$MAKE" --model "$MODEL" --year-range "$YEARS" \
    --max-pages "$STATE_MAX_PAGES" --out "$LIVE_API_RAW"

run_stage 05-vpic-ended "$ENDED_VPIC" validate_vpic \
    "$COPART_PIPELINE_PYTHON" "$SCRIPTS/copart_vpic_adapt_01.py" \
    "$ENDED_RAW" --out "$ENDED_VPIC"
run_stage 06-vpic-open "$OPEN_VPIC" validate_vpic \
    "$COPART_PIPELINE_PYTHON" "$SCRIPTS/copart_vpic_adapt_01.py" \
    "$OPEN_API_RAW" --out "$OPEN_VPIC"
run_stage 07-vpic-live "$LIVE_VPIC" validate_vpic \
    "$COPART_PIPELINE_PYTHON" "$SCRIPTS/copart_vpic_adapt_01.py" \
    "$LIVE_API_RAW" --out "$LIVE_VPIC"

run_stage 08-web-adapt-merge "$WEB_ADAPTED" validate_web_adapted \
    "$COPART_PIPELINE_PYTHON" "$SCRIPTS/copart_web_adapt_01.py" "$WEB_RAW" \
    --enrich-from "$OPEN_VPIC" "$LIVE_VPIC" "$ENDED_VPIC" \
    --audit --out "$WEB_ADAPTED"

# stat.vin fills the seller gap Copart leaves and completes the masked VIN.
# It MUST run before the selection: the cut excludes dealer lots and the
# gallery stage takes its lot list from that cut, so a seller class arriving
# later would be too late to keep a dealer lot out of either.
run_stage 08a-statvin-pull "$STATVIN_RAW" validate_statvin_raw \
    "$COPART_PIPELINE_PYTHON" "$SCRIPTS/pull_statvin_web_01.py" \
    --make "$MAKE" --model "$STATVIN_MODEL" --year-range "$YEARS" \
    --out "$STATVIN_RAW"

run_stage 08b-statvin-enrich "$WEB_ENRICHED" validate_web_adapted \
    "$COPART_PIPELINE_PYTHON" "$SCRIPTS/copart_statvin_enrich_01.py" \
    "$WEB_ADAPTED" --statvin "$STATVIN_RAW" --out "$WEB_ENRICHED"

run_stage 09-csv-raw-sold "$SOLD_RAW_CSV" validate_csv \
    "$COPART_PIPELINE_PYTHON" "$SCRIPTS/apibara_json2csv_copart_01.py" \
    "$ENDED_VPIC" --out "$SOLD_RAW_CSV"
run_stage 10-history-sold "$SOLD_CUT" validate_final_csv \
    "$COPART_PIPELINE_PYTHON" "$SCRIPTS/data_pull_01.py" copart "$ENDED_VPIC" \
    --tier "$TIER" --sold-only "${FINAL_BODY_FILTERS[@]}" "${SELLER_EXCLUSIONS[@]}" \
    --history --history-cache --out "$SOLD_CUT"

# This cheap preliminary cut is the authoritative gallery allowlist. Full
# galleries are never requested for lots already removed by downstream rules.
run_stage 11-open-selection "$OPEN_SELECTION" validate_final_csv \
    "$COPART_PIPELINE_PYTHON" "$SCRIPTS/data_pull_01.py" copart "$WEB_ENRICHED" \
    --tier "$TIER" "${FINAL_BODY_FILTERS[@]}" "${SELLER_EXCLUSIONS[@]}" \
    --out "$OPEN_SELECTION"

REUSE_COMMAND=("$COPART_PIPELINE_PYTHON" "$SCRIPTS/copart_image_enrich_01.py"
               "$WEB_ENRICHED" --lots-from-csv "$OPEN_SELECTION")
while IFS= read -r prior; do
    [[ "$prior" == "$WEB_ADAPTED" || "$prior" == "$WEB_ENRICHED" \
       || "$prior" == "$MEDIA_REUSED" ]] && continue
    REUSE_COMMAND+=(--reuse-from "$prior")
done < <(find "$OPEN_ADAPTED" -maxdepth 1 -type f \
    -name "*copartweb_copart_open_audi_${MODEL_SLUG}*.json" | sort)
REUSE_COMMAND+=(--reuse-only --out "$MEDIA_REUSED")
run_stage 12-gallery-reuse "$MEDIA_REUSED" validate_web_adapted "${REUSE_COMMAND[@]}"

PENDING=$(pending_gallery_count "$MEDIA_REUSED" "$OPEN_SELECTION")
GALLERY_MINUTES=$(( (PENDING * (GALLERY_CAPTURE_SECONDS + GALLERY_DELAY_SECONDS) + GALLERY_WORKERS * 60 - 1) / (GALLERY_WORKERS * 60) ))
SELECTION_COUNT=$(($(wc -l < "$OPEN_SELECTION") - 1))
log "gallery budget after cut/reuse: $PENDING of $SELECTION_COUNT selected lot page(s), about $GALLERY_MINUTES min with $GALLERY_WORKERS worker(s)"
if ((PENDING)); then
    run_stage 13-gallery-browser "$MEDIA_BROWSER" validate_selected_media \
        "$COPART_PIPELINE_PYTHON" "$SCRIPTS/copart_browser_enrich_01.py" \
        "$MEDIA_REUSED" --lots-from-csv "$OPEN_SELECTION" \
        --all-incomplete --max-lots 0 \
        --capture-seconds "$GALLERY_CAPTURE_SECONDS" \
        --delay "$GALLERY_DELAY_SECONDS" --workers "$GALLERY_WORKERS" \
        --out "$MEDIA_BROWSER"
    FINAL_MEDIA="$MEDIA_BROWSER"
else
    FINAL_MEDIA="$MEDIA_REUSED"
    if [[ ! -f "$RUN_DIR/13-gallery-browser.done" ]]; then
        printf 'completed_at=%s\nartifact=%s\nnote=no incomplete selected galleries after reuse\nelapsed_seconds=0\n' \
            "$(date --iso-8601=seconds)" "$FINAL_MEDIA" \
            > "$RUN_DIR/13-gallery-browser.done"
    fi
    log "DONE  13-gallery-browser — no browser calls needed"
fi
validate_selected_media "$FINAL_MEDIA" "" || die "selected media archive is incomplete"

run_stage 14-csv-raw-open "$OPEN_RAW_CSV" validate_csv \
    "$COPART_PIPELINE_PYTHON" "$SCRIPTS/apibara_json2csv_copart_01.py" \
    "$FINAL_MEDIA" --out "$OPEN_RAW_CSV"

run_stage 15-history-open "$OPEN_CUT" validate_final_csv \
    "$COPART_PIPELINE_PYTHON" "$SCRIPTS/data_pull_01.py" copart "$FINAL_MEDIA" \
    --tier "$TIER" "${FINAL_BODY_FILTERS[@]}" "${SELLER_EXCLUSIONS[@]}" \
    --history --history-cache --out "$OPEN_CUT"

IMAGE_MANIFEST="$ROOT/images/open/manifest_open.csv"
run_stage 16-images "$IMAGE_MANIFEST" validate_image_stage \
    "$COPART_PIPELINE_PYTHON" "$SCRIPTS/pull_images_01.py" "$OPEN_CUT" \
    --platform copart --archive-sold --model-folder "$MAKE $MODEL"

COMPLETED_AT=$(date --iso-8601=seconds)
"$COPART_PIPELINE_PYTHON" - "$RUN_DIR/manifest.json" "$RUN_ID" \
    "$STARTED_AT" "$COMPLETED_AT" "$ENDED_FROM" "$ENDED_TO" \
    "$ENDED_RAW" "$WEB_RAW" "$OPEN_API_RAW" "$LIVE_API_RAW" \
    "$ENDED_VPIC" "$OPEN_VPIC" "$LIVE_VPIC" "$FINAL_MEDIA" \
    "$SOLD_RAW_CSV" "$OPEN_RAW_CSV" "$SOLD_CUT" "$OPEN_CUT" \
    "$OPEN_SELECTION" "$RUN_DIR" "$MAKE" "$MODEL" <<'PY'
import json, pathlib, re, sys
(destination, run_id, started, completed, date_from, date_to, *tail) = sys.argv[1:]
artifacts, run_dir, make, model = tail[:-3], tail[-3], tail[-2], tail[-1]
names = ["ended_raw", "web_raw", "open_raw", "live_raw", "ended_vpic",
         "open_vpic", "live_vpic", "final_media", "sold_csv_raw",
         "open_csv_raw", "sold_csv_cut", "open_csv_cut", "open_selection"]
timings = {}
run_path = pathlib.Path(run_dir)
for marker in run_path.glob("*.done"):
    values = {}
    for line in marker.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    if "elapsed_seconds" in values:
        timings[marker.stem] = int(values["elapsed_seconds"])
attempt_timings = {}
log_path = run_path / "run.log"
if log_path.is_file():
    pattern = re.compile(r"DONE\s+([0-9]{2}-[a-z0-9-]+) \(([0-9]+)s\)")
    for stage, seconds in pattern.findall(log_path.read_text(encoding="utf-8")):
        attempt_timings.setdefault(stage, []).append(int(seconds))
manifest = {
    "pipeline": f"copart-{model.lower()}", "version": 4, "run_id": run_id,
    "scope": {"make": make, "model": model, "year_from": 2018, "year_to": 2023,
              "market": "UnitedStates", "ended_from": date_from, "ended_to": date_to},
    "started_at": started, "completed_at": completed,
    "artifacts": dict(zip(names, artifacts)),
    "optimization": {
        "gallery_after_preliminary_cut": True,
        "gallery_allowlist": artifacts[-1],
    },
    "stage_timings_seconds": dict(sorted(timings.items())),
    "stage_attempt_timings_seconds": dict(sorted(attempt_timings.items())),
}
path = pathlib.Path(destination)
path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
PY

log "COMPLETE $RUN_ID at $COMPLETED_AT"
log "manifest=$RUN_DIR/manifest.json"
log "open cut=$OPEN_CUT"
log "sold cut=$SOLD_CUT"
