#!/usr/bin/env bash
# dendra_cofailure_measure.sh — MESURE DE CO-ÉCHEC MULTI-MODÈLES du juge (ADR-026 ; décision internal audit 2026-06-20).
#
# OBJECTIF : sur une carte LOUÉE (≥ RTX 3090, 24 Go), obtenir le P_escape COMITÉ EMPIRIQUE de la famille de
# juges décorrélée. Si ≤ 5 % → le gate comité devient ACTIVABLE (bascule du défaut per-juge → comité ≤5 %).
# C'est la mesure que la RTX 2060 ne pouvait PAS faire (seuls des modèles ~3B y tenaient, et ils ÉCHOUENT
# l'admission vrai-100/faux-0). Une 3090 fait tourner mistral-nemo 12B + llama3.1 8B + qwen2.5 7B (séquentiel).
#
# CE QUE FAIT CE SCRIPT (idempotent, UNE commande) :
#   1) vérifie python3 + judge_calibration.py + modea/ (tourne DEPUIS services/) ;
#   2) vérifie le GPU (nvidia-smi) — sans GPU la mesure est trop lente (avertit, ne bloque pas) ;
#   3) installe Ollama si absent, démarre le serveur si besoin, attend qu'il réponde ;
#   4) selftest HORS-LIGNE du harnais co-échec (fail-fast si le banc lui-même est cassé) ;
#   5) pull CHAQUE modèle de la famille (défaut : mistral-nemo, llama3.1, qwen2.5 = 3 familles distinctes) ;
#   6) lance le banc co-échec sur TOUS les cas (gate 5 %), tee vers un log horodaté + sauve le JSON résumé ;
#   7) imprime le VERDICT et PRÉSERVE le code de sortie (0 = gate PASS, 1 = gate FAIL, 2 = Ollama injoignable).
#
# Surcharges :
#   DENDRA_JUDGE_MODELS="mistral-nemo,llama3.1,qwen2.5,gemma2"   # ajoute une 4e famille (marge de décorrélation)
#   DENDRA_GATE_COMMITTEE_PESCAPE=0.05                           # bar du gate (défaut 5 %)
#   OLLAMA_ENDPOINT=http://localhost:11434                       # endpoint Ollama
#
# Usage (terminal SSH de la 3090 LOUÉE, depuis le dossier services/ copié dessus) :
#   cd services && bash dendra_cofailure_measure.sh
set -uo pipefail
say(){ echo; echo "=== $* ==="; }
die(){ echo "  ECHEC: $*" >&2; exit 1; }

SELF="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
cd "$SELF" || die "impossible de cd vers $SELF"
[ -f judge_calibration.py ] || die "judge_calibration.py introuvable ici ($SELF) — lance DEPUIS services/"
[ -f modea/judge.py ]       || die "modea/judge.py introuvable — copie TOUT le dossier services/ (judge_calibration.py + modea/)"
command -v python3 >/dev/null 2>&1 || die "python3 absent (apt-get install -y python3)"
command -v curl    >/dev/null 2>&1 || die "curl absent (apt-get install -y curl) — requis pour Ollama"

MODELS="${DENDRA_JUDGE_MODELS:-mistral-nemo,llama3.1,qwen2.5}"
ENDPOINT="${OLLAMA_ENDPOINT:-http://localhost:11434}"
BAR="${DENDRA_GATE_COMMITTEE_PESCAPE:-0.05}"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUTDIR="$SELF/cofailure_results"; mkdir -p "$OUTDIR"
LOG="$OUTDIR/cofailure-$STAMP.log"
JSON="$OUTDIR/cofailure-$STAMP.json"

say "0) Contexte"
echo "  dossier  : $SELF"
echo "  modèles  : $MODELS"
echo "  endpoint : $ENDPOINT"
echo "  bar gate : P_escape comité ≤ $BAR"
echo "  log      : $LOG"

say "1) GPU"
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=name,memory.total --format=csv,noheader || true
else
  echo "  ⚠ nvidia-smi absent : aucun GPU détecté. La mesure tournera mais sera LENTE (CPU)."
  echo "    Sur une 3090 louée le pilote NVIDIA doit être présent. Reprise dans 5 s…"; sleep 5
fi

say "2) Ollama (install si absent + serveur up)"
if ! command -v ollama >/dev/null 2>&1; then
  echo "  Ollama absent → installation (curl ollama.com/install.sh)…"
  curl -fsSL https://ollama.com/install.sh | sh || die "install Ollama KO"
fi
if ! curl -fsS "$ENDPOINT/api/tags" >/dev/null 2>&1; then
  echo "  démarrage du serveur Ollama (arrière-plan)…"
  nohup ollama serve >"$OUTDIR/ollama-serve-$STAMP.log" 2>&1 &
  for _ in $(seq 1 30); do curl -fsS "$ENDPOINT/api/tags" >/dev/null 2>&1 && break; sleep 1; done
fi
curl -fsS "$ENDPOINT/api/tags" >/dev/null 2>&1 || die "serveur Ollama injoignable sur $ENDPOINT (voir $OUTDIR/ollama-serve-$STAMP.log)"
echo "  serveur Ollama OK ($ENDPOINT)"

say "3) Selftest HORS-LIGNE du harnais co-échec (fail-fast, aucun réseau)"
python3 judge_calibration.py --selftest-cofailure || die "selftest co-échec KO — le banc est cassé, NE PAS se fier à la mesure"

say "4) Pull des modèles (idempotent)"
IFS=',' read -ra MS <<< "$MODELS"
for m in "${MS[@]}"; do
  m="$(echo "$m" | xargs)"; [ -z "$m" ] && continue
  echo "  → ollama pull $m"
  ollama pull "$m" || die "pull '$m' KO (vérifie le nom du modèle / la connexion)"
done

say "5) Banc CO-ÉCHEC sur TOUS les cas (peut prendre ~30–60 min selon GPU + téléchargements)"
set +e
DENDRA_JUDGE=cofailure DENDRA_JUDGE_MODELS="$MODELS" OLLAMA_ENDPOINT="$ENDPOINT" \
  DENDRA_GATE_COMMITTEE_PESCAPE="$BAR" DENDRA_MAX_CASES=0 \
  python3 judge_calibration.py 2>&1 | tee "$LOG"
CODE=${PIPESTATUS[0]}
set -e

# extrait la ligne JSON stable (schema dendra.judge_cofailure.v1) vers un fichier dédié
python3 - "$LOG" "$JSON" <<'PY' || true
import sys, json
log, out = sys.argv[1], sys.argv[2]
lines = [l.strip() for l in open(log, encoding="utf-8") if "dendra.judge_cofailure" in l]
if lines:
    s = lines[-1]; s = s[s.index("{"):]
    try:
        json.dump(json.loads(s), open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"[json] résumé sauvé -> {out}")
    except Exception as e:
        print(f"[json] parse KO ({e}) — le log reste la source.")
PY

say "VERDICT"
case "$CODE" in
  0) echo "  ✅ GATE PASS — co-échec comité ≤ $BAR. La famille est DÉCORRÉLÉE → le gate comité est ACTIVABLE." ;;
  1) echo "  ❌ GATE FAIL — co-échec > $BAR OU aucun modèle admissible. NE PAS activer le gate comité ;"
     echo "     élargir/diversifier la famille (architectures différentes) puis re-mesurer. Filet per-juge strict conservé." ;;
  2) echo "  ⚠ OLLAMA INJOIGNABLE — un modèle n'a pas répondu. Vérifie 'ollama serve' + les pulls, puis relance." ;;
  *) echo "  ⚠ code inattendu ($CODE) — voir $LOG." ;;
esac
echo "  log complet : $LOG"
[ -f "$JSON" ] && echo "  résumé JSON : $JSON"
echo
echo "  → Renvoie-moi le bloc 'VERDICT FAMILLE DÉCORRÉLÉE' + le JSON : si PASS, j'active la bascule du gate défaut."
exit "$CODE"
