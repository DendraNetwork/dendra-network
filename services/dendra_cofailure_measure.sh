#!/usr/bin/env bash
# dendra_cofailure_measure.sh — MULTI-MODEL CO-FAILURE MEASUREMENT for the judge (ADR-026).
#
# GOAL: on a rented GPU (RTX 3090 class or better, 24 GB), obtain the EMPIRICAL committee P_escape of
# the decorrelated judge family. At 5 % or below, the committee gate becomes activatable (switching
# the default from per-judge to committee <= 5 %). A 6 GB card cannot make this measurement: only
# ~3B models fit there, and those FAIL the true-100/false-0 admission. A 24 GB card runs
# mistral-nemo 12B, llama3.1 8B and qwen2.5 7B sequentially.
#
# WHAT THIS SCRIPT DOES (idempotent, ONE command):
#   1) checks python3, judge_calibration.py and modea/ (it must run FROM services/);
#   2) checks the GPU (nvidia-smi) — without one the measurement is very slow (warns, does not block);
#   3) installs Ollama when missing, starts the server if needed, waits for it to answer;
#   4) runs the OFFLINE selftest of the co-failure harness (fail fast if the bench itself is broken);
#   5) pulls EVERY model of the family (default: mistral-nemo, llama3.1, qwen2.5 = 3 distinct families);
#   6) runs the co-failure bench over ALL cases (5 % gate), tees to a timestamped log and saves the
#      JSON summary;
#   7) prints the VERDICT and PRESERVES the exit code (0 = gate PASS, 1 = gate FAIL, 2 = Ollama
#      unreachable).
#
# Overrides:
#   DENDRA_JUDGE_MODELS="mistral-nemo,llama3.1,qwen2.5,gemma2"   # adds a 4th family (decorrelation margin)
#   DENDRA_GATE_COMMITTEE_PESCAPE=0.05                           # the gate bar (default 5 %)
#   OLLAMA_ENDPOINT=http://localhost:11434                       # Ollama endpoint
#
# Usage (SSH session on the rented GPU host, from the copied services/ directory):
#   cd services && bash dendra_cofailure_measure.sh
set -uo pipefail
say(){ echo; echo "=== $* ==="; }
die(){ echo "  FAILED: $*" >&2; exit 1; }

SELF="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
cd "$SELF" || die "cannot cd to $SELF"
[ -f judge_calibration.py ] || die "judge_calibration.py not found here ($SELF) — run FROM services/"
[ -f modea/judge.py ]       || die "modea/judge.py not found — copy the WHOLE services/ directory (judge_calibration.py + modea/)"
command -v python3 >/dev/null 2>&1 || die "python3 missing (apt-get install -y python3)"
command -v curl    >/dev/null 2>&1 || die "curl missing (apt-get install -y curl) — required for Ollama"

MODELS="${DENDRA_JUDGE_MODELS:-mistral-nemo,llama3.1,qwen2.5}"
ENDPOINT="${OLLAMA_ENDPOINT:-http://localhost:11434}"
BAR="${DENDRA_GATE_COMMITTEE_PESCAPE:-0.05}"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUTDIR="$SELF/cofailure_results"; mkdir -p "$OUTDIR"
LOG="$OUTDIR/cofailure-$STAMP.log"
JSON="$OUTDIR/cofailure-$STAMP.json"

say "0) Context"
echo "  directory : $SELF"
echo "  models    : $MODELS"
echo "  endpoint  : $ENDPOINT"
echo "  gate bar  : committee P_escape <= $BAR"
echo "  log       : $LOG"

say "1) GPU"
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=name,memory.total --format=csv,noheader || true
else
  echo "  WARNING: nvidia-smi missing, no GPU detected. The measurement will run but will be SLOW (CPU)."
  echo "    On a rented GPU host the NVIDIA driver must be present. Continuing in 5 s..."; sleep 5
fi

say "2) Ollama (install when missing, server up)"
if ! command -v ollama >/dev/null 2>&1; then
  echo "  Ollama missing -> installing (curl ollama.com/install.sh)..."
  curl -fsSL https://ollama.com/install.sh | sh || die "Ollama install failed"
fi
if ! curl -fsS "$ENDPOINT/api/tags" >/dev/null 2>&1; then
  echo "  starting the Ollama server (background)..."
  nohup ollama serve >"$OUTDIR/ollama-serve-$STAMP.log" 2>&1 &
  for _ in $(seq 1 30); do curl -fsS "$ENDPOINT/api/tags" >/dev/null 2>&1 && break; sleep 1; done
fi
curl -fsS "$ENDPOINT/api/tags" >/dev/null 2>&1 || die "Ollama server unreachable at $ENDPOINT (see $OUTDIR/ollama-serve-$STAMP.log)"
echo "  Ollama server OK ($ENDPOINT)"

say "3) OFFLINE selftest of the co-failure harness (fail fast, no network)"
python3 judge_calibration.py --selftest-cofailure || die "co-failure selftest failed — the bench is broken, do NOT trust the measurement"

say "4) Model pull (idempotent)"
IFS=',' read -ra MS <<< "$MODELS"
for m in "${MS[@]}"; do
  m="$(echo "$m" | xargs)"; [ -z "$m" ] && continue
  echo "  -> ollama pull $m"
  ollama pull "$m" || die "pull '$m' failed (check the model name and the connection)"
done

say "5) CO-FAILURE bench over ALL cases (roughly 30-60 min depending on GPU and downloads)"
set +e
DENDRA_JUDGE=cofailure DENDRA_JUDGE_MODELS="$MODELS" OLLAMA_ENDPOINT="$ENDPOINT" \
  DENDRA_GATE_COMMITTEE_PESCAPE="$BAR" DENDRA_MAX_CASES=0 \
  python3 judge_calibration.py 2>&1 | tee "$LOG"
CODE=${PIPESTATUS[0]}
set -e

# extract the stable JSON line (schema dendra.judge_cofailure.v1) into a dedicated file
python3 - "$LOG" "$JSON" <<'PY' || true
import sys, json
log, out = sys.argv[1], sys.argv[2]
lines = [l.strip() for l in open(log, encoding="utf-8") if "dendra.judge_cofailure" in l]
if lines:
    s = lines[-1]; s = s[s.index("{"):]
    try:
        json.dump(json.loads(s), open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"[json] summary saved -> {out}")
    except Exception as e:
        print(f"[json] parse failed ({e}) — the log remains the source.")
PY

say "VERDICT"
case "$CODE" in
  0) echo "  GATE PASS — committee co-failure <= $BAR. The family is DECORRELATED, the committee gate is activatable." ;;
  1) echo "  GATE FAIL — co-failure > $BAR OR no admissible model. Do NOT activate the committee gate;"
     echo "     widen and diversify the family (different architectures) and re-measure. The strict per-judge net stays." ;;
  2) echo "  OLLAMA UNREACHABLE — a model did not answer. Check 'ollama serve' and the pulls, then re-run." ;;
  *) echo "  unexpected exit code ($CODE) — see $LOG." ;;
esac
echo "  full log     : $LOG"
[ -f "$JSON" ] && echo "  JSON summary : $JSON"
echo
echo "  -> Keep the verdict block and the JSON summary: they are the evidence required before switching the default gate."
exit "$CODE"
