#!/usr/bin/env bash
# entrypoint-chain.sh - persistent Dendra TESTNET node.
#   Runs `dendrad start` natively: no recompilation and no random keys between boots.
#   First boot : init + stable keys (validator/bob/community/team/reserve/gw) + genesis accounts
#                + OPTIMISTIC params (ADR-025) + model registry (llama3.1:8b default + mistral-nemo
#                + nomic) + gentx + collect.
#   Later boots: a genesis is already in the volume -> state is reused (PERSISTENT).
# Fixed 10 M udndr supply is preserved. gw holds 0 at genesis (funded by the faucet, like miners).
set -e
export PATH="$PATH:/go/bin:/usr/local/go/bin"
BIN=dendrad
HOME_DIR="${DENDRA_HOME:-/root/.dendra}"
CHAIN_ID="${DENDRA_CHAIN_ID:-dendra}"
DENOM=udndr
KB="--keyring-backend test --home $HOME_DIR"
GEN="$HOME_DIR/config/genesis.json"

command -v "$BIN" >/dev/null 2>&1 || { echo "[chain] FATAL: dendrad not found"; exit 1; }
command -v jq    >/dev/null 2>&1 || { echo "[chain] FATAL: jq not found (add it to the Dockerfile)"; exit 1; }
mkdir -p "$HOME_DIR"

if [ ! -f "$GEN" ]; then
  echo "[chain] ===== FIRST BOOT: building the testnet genesis ($CHAIN_ID) ====="
  rm -rf "$HOME_DIR/config" "$HOME_DIR/data" 2>/dev/null || true
  $BIN init "${DENDRA_MONIKER:-dendra-testnet}" --chain-id "$CHAIN_ID" --default-denom "$DENOM" --home "$HOME_DIR" >/dev/null 2>&1

  # COLD ADDRESSES (optional, but the option exists ONLY AT GENESIS).
  # By default the 6 keys are created INSIDE the container, in the `test` keyring — that is, as
  # plaintext mnemonics on a rented host. That is acceptable for service keys (validator/faucet/gw),
  # which must sign continuously. It is not acceptable for `reserve` (3.3 M) and `team` (0.5 M):
  # 38 % of the supply that almost never signs, and that a host compromise would carry away entirely
  # and permanently.
  # When DENDRA_RESERVE_ADDR / DENDRA_TEAM_ADDR are supplied, the corresponding key is NOT generated
  # here: genesis credits an address whose private key has never touched this machine.
  # No spare key is generated alongside: it would land in keys-backup.jsonl and suggest, on the next
  # read, that this host holds the reserve — a false safety net is worse than none.
  #
  # DENDRA_EQUIPE_ADDR IS READ AS A FALLBACK, AND THAT IS NOT A COURTESY. An operator whose stack already
  # names the old variable must not discover at the FIRST BOOT — the one moment cold addresses can be
  # posted at all — that its cold pocket was ignored and 0.5 M landed on a key generated in this
  # container. The old name is honoured, loudly, and the new one wins when both are set.
  TEAM_COLD="${DENDRA_TEAM_ADDR:-}"
  if [ -z "$TEAM_COLD" ] && [ -n "${DENDRA_EQUIPE_ADDR:-}" ]; then
    TEAM_COLD="$DENDRA_EQUIPE_ADDR"
    echo "[chain] WARNING: DENDRA_EQUIPE_ADDR is DEPRECATED -> rename it DENDRA_TEAM_ADDR. This boot uses"
    echo "        the value it carries; the compose file must name the new variable for it to be read."
  fi
  for k in validator bob community team reserve gw; do
    [ "$k" = "reserve" ] && [ -n "${DENDRA_RESERVE_ADDR:-}" ] && continue
    [ "$k" = "team" ]    && [ -n "$TEAM_COLD" ]               && continue
    if ! $BIN keys show "$k" $KB >/dev/null 2>&1; then
      $BIN keys add "$k" $KB --output json >> "$HOME_DIR/keys-backup.jsonl" 2>/dev/null
    fi
  done
  VAL=$($BIN keys show validator -a $KB)
  BOB=$($BIN keys show bob -a $KB)
  COM=$($BIN keys show community -a $KB)
  TEAM="${TEAM_COLD:-$($BIN keys show team -a $KB)}"
  RES="${DENDRA_RESERVE_ADDR:-$($BIN keys show reserve -a $KB)}"
  GW=$($BIN keys show gw -a $KB)
  # A mistyped address (stray space, truncation, wrong prefix) is INVISIBLE: `add-genesis-account`
  # would fail, the boot would carry on, and the supply would be incomplete at block 1 — unfixable.
  # The FORM is therefore checked before anything is created, and the boot is refused rather than
  # allowed to drift.
  for _a in "$TEAM" "$RES"; do
    case "$_a" in
      dendra1*) [ ${#_a} -ge 39 ] || { echo "[chain] FATAL: genesis address too short: $_a"; exit 1; } ;;
      *) echo "[chain] FATAL: invalid genesis address (expected 'dendra1' prefix): $_a"; exit 1 ;;
    esac
  done
  [ -n "${DENDRA_RESERVE_ADDR:-}" ] && echo "[chain] reserve = COLD ADDRESS $RES (key not present on this machine)"
  [ -n "$TEAM_COLD" ]               && echo "[chain] team    = COLD ADDRESS $TEAM (key not present on this machine)"
  echo "[chain] validator=$VAL"
  echo "[chain] bob(faucet)=$BOB"
  echo "[chain] gw(gateway)=$GW (funded by the faucet)"
  chmod 600 "$HOME_DIR/keys-backup.jsonl" 2>/dev/null || true

  $BIN genesis add-genesis-account "$VAL" 2700000000000$DENOM $KB
  $BIN genesis add-genesis-account "$BOB" 100000000000$DENOM $KB
  $BIN genesis add-genesis-account "$COM" 3400000000000$DENOM $KB
  $BIN genesis add-genesis-account "$TEAM" 500000000000$DENOM $KB
  $BIN genesis add-genesis-account "$RES" 3300000000000$DENOM $KB

  # UNIFIED GENESIS: this jobs.params block MUST stay aligned with chain/config.optimistic.yml,
  # which is the published reference for the N1/N2/veto gates. A launch stack that enables optimistic
  # settlement WITHOUT hold_bps (N1), WITHOUT audit_min_quorum (the N=5 veto) and with timeout=30
  # (below N2) bypasses the gates of optimistic mode entirely.
  # Only N1/N2/veto are SECURITY params to keep aligned. audit_sample_bps=5000 (testnet, 50 % audited)
  # differs DELIBERATELY from the 10000 in config.optimistic.yml (demo = 100 % audited, slash visible)
  # — it is not a gate.
  tmp=$(mktemp)
  # audit_sample_bps is env-tunable (default 5000 = testnet 50 %). For a deterministic slash
  # demonstration (the primary is drawn by a uniform HASH, not by stake -> at 50 % the cheater often
  # slips through), set DENDRA_AUDIT_SAMPLE_BPS=10000 (100 % audited): every job of the cheater is
  # judged. 100 % audited is also a STRICTER test of false slashes (every honest miner faces a judge).
  # It is NOT a gate.
  ASB="${DENDRA_AUDIT_SAMPLE_BPS:-5000}"
  # audit_resolve_timeout is env-tunable: 120 blocks (~2 min) suffice for a warm HOMOGENEOUS committee,
  # but two co-resident judge models (shared CPU, alternating loads) post their "0" verdicts AFTER the
  # timeout -> the silent case resolves as no-quorum (negative-EV silence slash, but no HARD slash).
  # Heterogeneous bench: DENDRA_AUDIT_RESOLVE_TIMEOUT=360. Default 120. (Validate requires it to be
  # greater than dispute_window=10.)
  ARTO="${DENDRA_AUDIT_RESOLVE_TIMEOUT:-120}"
  # DISPUTE BOND. Left unset anywhere, the proto default of 0 makes disputing FREE, and the anti-grief
  # confiscation (unfounded dispute -> bond to the Treasury) confiscates nothing: the instrument exists
  # but has no bite. The anchor chosen is `min_stake` (50000 udndr = 0.05 DNDR), about 11 job prices
  # (4500) and 1/200 of a faucet drip (10 DNDR): expensive enough that grieving costs something,
  # accessible enough that an honest user can still contest. It is REFUNDED when a committee was
  # summoned and did not answer, and refunded plus rewarded when the dispute is upheld.
  # Economic parameter; override with DENDRA_DISPUTE_BOND.
  DBOND="${DENDRA_DISPUTE_BOND:-50000}"
  # COMMITTEE SEED SOURCE — CONSENSUS-BREAKING (genesis state). It decides WHO gets to pick the jury.
  #   0 = legacy seed, AppHash(H-1):h. The BLOCK PROPOSER knows both terms BEFORE proposing, so it can
  #       compute the draw offline and choose whether to include the job. The party being verified
  #       then selects its own verifiers, and nothing on screen says so.
  #   1 = decentralised seed only. Without one, no draw happens: the audit is DEFERRED — jobs wait,
  #       visibly, and the backlog drains as soon as a seed exists. With a single validator no
  #       decentralised seed is possible (contributors=1 < the floor below), so audits wait. That
  #       deferral is honest; 0 would produce a verification whose randomness the sole proposer owns.
  # DEFAULT 1, deliberately: the safe value must be the one obtained by doing nothing. This value used
  # to be a literal inside the jq filter, which made the most load-bearing decision of optimistic mode
  # the only one an operator could not read from the deployment kit.
  # Lowering it is NOT a silent downgrade: with verification_mode=1 and committee_reveal_delay unset,
  # `Params.Validate()` rejects 0 (chain/x/jobs/types/params.go:200), and so does any non-zero
  # committee_min_vrf_contributors (:209). The refusal lands on `gentx` below, which re-validates the
  # genesis before signing and so fires BEFORE the explicit validate-genesis; either way the boot
  # fails rather than starting a weaker chain.
  SEEDSRC="${DENDRA_COMMITTEE_SEED_SOURCE:-1}"
  case "$SEEDSRC" in
    0|1) ;;
    *) echo "[chain] FATAL: DENDRA_COMMITTEE_SEED_SOURCE must be 0 (legacy) or 1 (decentralised VRF). Got: $SEEDSRC"; exit 1 ;;
  esac
  if [ "$SEEDSRC" = "0" ]; then
    echo "[chain] WARNING: committee_seed_source=0 -> the jury is drawn on a seed the block proposer"
    echo "        knows before proposing. Verification becomes selectable by the party verified."
    echo "        This entrypoint does not decide whether 0 is allowed; the chain does. In optimistic"
    echo "        mode (verification_mode=1, set below) Params.Validate refuses 0 unless"
    echo "        committee_reveal_delay>0 is posted too, and refuses any non-zero"
    echo "        committee_min_vrf_contributors alongside it. The boot then fails with that message."
  fi
  # COMMITTEE REVEAL DELAY — the SECOND anti-grinding guard, and the one that still holds when the
  # first one is unavailable.
  #
  # This parameter was never written by this entrypoint. `Params.Validate` does not require it once
  # committee_seed_source=1, so the genesis passed and the chain ran with it at 0 — a value chosen by
  # proto3 omission, not by anyone. The `committee_reveal_delay: "1"` in
  # chain/config.optimistic.yml is a DIFFERENT deployment path and never reached this genesis.
  #
  # Why 0 is not harmless even with seed_source=1. The decentralised seed is not always THERE. When
  # contributors sit below committee_min_vrf_contributors — the state of a network that has just
  # started, with fewer validators than the floor — `committeeBaseSeedSourced` alerts and returns the
  # LEGACY fallback. At delay 0 that fallback is `height:time` as observed BY THE JOB CREATOR at open
  # time, so a creator can reopen until the draw suits it. The audit draw is separately deferred in
  # that regime (audit_sampling.go), but the beacon has already been frozen on a predictable value,
  # and it is the beacon the committee is drawn from once contributors arrive.
  # At delay > 0 the beacon is left EMPTY at open and fixed later on the AppHash of a FUTURE block —
  # unknown to the creator whatever the seed source is doing. The two guards are independent, and this
  # is the one that covers the bootstrap.
  #
  # GOVERNABLE, and measured as such: chain/x/jobs/keeper/reveal_delay_governance_test.go moves it
  # from 0 to 1 with MsgUpdateParams on a live parameter set and shows OpenJob changing behaviour
  # immediately. It therefore does NOT require a chain reset. It is written here anyway, because a
  # value that must be corrected by a governance proposal on day one is a hole that stays open until
  # somebody remembers — and the safe value must be the one obtained by doing nothing.
  #
  # 1 block is the minimum that closes the hole; higher values delay commits by that many blocks
  # (CreateCommit refuses until the seed is revealed) without adding unpredictability.
  RVDELAY="${DENDRA_COMMITTEE_REVEAL_DELAY:-1}"
  case "$RVDELAY" in
    ''|*[!0-9]*) echo "[chain] FATAL: DENDRA_COMMITTEE_REVEAL_DELAY must be a non-negative integer. Got: $RVDELAY"; exit 1 ;;
  esac
  if [ "$RVDELAY" = "0" ]; then
    echo "[chain] WARNING: committee_reveal_delay=0 -> the job beacon is frozen at open time on"
    echo "        height:time, both known to the job creator. That is only safe while a decentralised"
    echo "        VRF seed is actually available; below committee_min_vrf_contributors it is not, and"
    echo "        the legacy fallback becomes grindable by whoever opens the job."
  fi
  # x/mint is DE-REGISTERED from the app (structurally zero mint) -> no mint section is written (an
  # unregistered module carrying a genesis section fails the boot); any leftover is purged instead
  # (del), in case an older genesis or volume still carries one.
  jq --arg asb "$ASB" --arg arto "$ARTO" --arg dbond "$DBOND" --arg seed "$SEEDSRC" --arg rvd "$RVDELAY" '
    del(.app_state.mint)
    | .app_state.jobs.params.verification_mode = "1"
    | .app_state.jobs.params.audit_sample_bps = $asb
    | .app_state.jobs.params.min_stake = "50000"
    | .app_state.jobs.params.slash_leak_bps = "8000"
    | .app_state.jobs.params.fee_burn_bps = "500"
    | .app_state.jobs.params.dispute_window = "10"
    | .app_state.jobs.params.dispute_bond = $dbond
    | .app_state.jobs.params.audit_resolve_timeout = $arto
    | .app_state.jobs.params.hold_bps = "10000"
    | .app_state.jobs.params.audit_min_quorum = "4"
    | .app_state.jobs.params.silence_slash_bps = "2000"
    | .app_state.jobs.params.committee_seed_source = $seed
    | .app_state.jobs.params.committee_reveal_delay = $rvd
    | .app_state.jobs.params.avail_require_demand = true
    | .app_state.emission.params.reserve_release_bps = "2"
    | .app_state.emission.params.work_split_bps = "5000"
    | .app_state.emission.params.avail_split_bps = "2000"
    | .app_state.emission.params.work_gate_bps = "15000"
    # 86400 BLOCKS, AND THE EPOCH IS WHAT IS CALIBRATED — NOT THE RATE.
    # The Reserve releases `reserve_release_bps` of what REMAINS each epoch, so the curve is geometric
    # and the quantity that matters is the HALF-LIFE, counted in EPOCHS: at 2 bps it is ~6932 epochs on
    # an idle chain and ~3466 with a saturated gate, and those two counts depend on nothing but the bps.
    # At 300 blocks per epoch that was ~2.1 M / ~1.0 M blocks — the Reserve was materially gone before
    # the testnet ended. Lowering the rate cannot fix it: `reserve_release_bps` is a uint64, the next
    # value below 2 is 1 and then 0, and 0 means STOP EMITTING. So the epoch is what moves.
    # TIME, WITH ITS HYPOTHESIS ATTACHED: at the x/chaintime target of 1 s per block an epoch is one day
    # and the half-lives read 19.0 / 9.5 years; at the ~5 s per block measured on the public endpoint the
    # same epochs are five days and the same half-lives are ~95 / ~47.5 years. The block interval is set
    # by validator `timeout_commit` and network propagation, NOT by consensus, so no year figure here is
    # a guarantee — the epoch counts are.
    # DISTINCT from jobs.avail_epoch_blocks (~300 blocks): availability challenges keep their fast
    # cadence. The two live in different modules and x/jobs reads no emission parameter.
    # Locked by chain/app/emission_lifespan_test.go, which pins the half-life RANGE rather than the
    # integers — a test that pinned 86400 would move with any change instead of refusing it. That range
    # is expressed in years under the declared block interval, and the test says so in its own name.
    | .app_state.emission.params.epoch_blocks = "86400"
    | .app_state.modelregistry.models = [
        {"id":"llama3.1:8b-instruct-q4_K_M","weights_sha256":"","quant":"q4_K_M","engine":"ollama","hw_class":"consumer-gpu","active":true,"hf_repo":"","hf_revision":""},
        {"id":"mistral-nemo","weights_sha256":"","quant":"","engine":"ollama","hw_class":"consumer-gpu","active":true,"hf_repo":"","hf_revision":""},
        {"id":"nomic-embed-text","weights_sha256":"","quant":"","engine":"ollama","hw_class":"consumer-gpu","active":true,"hf_repo":"","hf_revision":""}
      ]
  ' "$GEN" > "$tmp" && mv "$tmp" "$GEN" || { echo "[chain] FATAL: genesis jq patch failed"; exit 1; }

  # DECENTRALIZED VRF BOOTSTRAP — paired with committee_seed_source above. Note the asymmetry, it is
  # deliberate: seed_source is written by the jq block above, whose failure is FATAL, while this block
  # only WARNS. A seed_source patched here on a best-effort basis could silently fall back to 0 — the
  # legacy seed — which is exactly the outcome the pairing exists to prevent.
  #  - committee_min_vrf_contributors = the FLOOR (⌈2N/3⌉ of the intended validator set; default 2, to
  #    be raised as N grows). A seed built from fewer contributors makes committeeBaseSeed RAISE AN
  #    ALERT and fall back to the legacy seed (never a halt; the regression stays VISIBLE).
  #  - vote_extensions_enable_height = a CONSENSUS param (outside app_state) that enables ABCI++ vote
  #    extensions, through which the decentralized VRF produces the seed. With no VRF keys anchored,
  #    the extensions are empty -> alert plus legacy fallback (non-blocking).
  #  Each validator anchors its key with deploy/testnet/anchor_vrf_key.sh, then (re)starts with
  #  DENDRA_VRF_KEY_FILE set.
  MINVRF="${DENDRA_MIN_VRF_CONTRIB:-2}"; VEH="${DENDRA_VE_ENABLE_HEIGHT:-20}"
  # OPERATOR guard on the contributor floor, in the shape of the ADR-022 one further down: a clear FATAL
  # BEFORE the jq, reproducing the precondition the chain applies, rather than an obscure trace later.
  # AT 0 THE FLOOR IS NOT "DORMANT", IT IS THE ONLY BAR THAT REFUSES A ONE-VALIDATOR SEED. The second
  # bar, MinVrfContributorPowerBps=6667 (committeeBaseSeedSourced), asks the contributors to carry 2/3 of
  # the commit's POWER — and a lone validator on a small set carries all of it, so that bar passes. Only
  # the COUNT distinguishes "the set agreed on a seed" from "one machine chose the randomness the audit
  # committee is drawn from". `Params.Validate` does not cover this: it rejects a floor above 0 without
  # seed_source=1 (chain/x/jobs/types/params.go:206) and says nothing about 0 alongside seed_source=1.
  # The admissible value depends on the seed source, so the guard does too:
  #   seed_source=1 -> the floor MUST be >= 1;
  #   seed_source=0 -> the floor MUST be 0, since params.go:206 refuses anything else and the boot would
  #                    otherwise die at gentx with the reason buried in a validation trace.
  # NO UPPER BOUND, and that is a decision rather than an omission: the floor is sized on the INTENDED
  # validator set (⌈2N/3⌉), which is by construction larger than the set present at genesis — a first
  # boot carries exactly one validator, so any ceiling computable here would reject the default of 2. A
  # floor set too high is not silent either: every draw logs the UNDER-DECENTRALIZED alert and the legacy
  # fallback stays visible.
  if [ "$SEEDSRC" = "1" ]; then
    if ! { [ "$MINVRF" -ge 1 ]; } 2>/dev/null; then
      echo "[chain] FATAL: DENDRA_MIN_VRF_CONTRIB must be an integer >= 1 when DENDRA_COMMITTEE_SEED_SOURCE=1. At 0 the floor stops refusing anything: a decentralised seed produced by a SINGLE validator becomes acceptable for the committee draw, and that validator owns the randomness the audit is drawn from. Got: $MINVRF"; exit 1
    fi
  else
    if ! { [ "$MINVRF" -eq 0 ]; } 2>/dev/null; then
      echo "[chain] FATAL: DENDRA_MIN_VRF_CONTRIB must be 0 when DENDRA_COMMITTEE_SEED_SOURCE=0 (the floor is only read on the decentralised seed path, so posting one here advertises a guard that is never evaluated; Params.Validate refuses the combination). Got: $MINVRF"; exit 1
    fi
  fi
  tmp=$(mktemp)
  jq --arg m "$MINVRF" --arg h "$VEH" '
      .app_state.jobs.params.committee_min_vrf_contributors = $m
    | (if has("consensus") then .consensus.params.abci.vote_extensions_enable_height = $h
       else .consensus_params.abci.vote_extensions_enable_height = $h end)
  ' "$GEN" > "$tmp" && mv "$tmp" "$GEN" \
    || { echo "[chain] WARNING: VRF bootstrap patch (min_vrf/vote_extensions) failed -> committee_seed_source=$SEEDSRC will ALERT (legacy fallback, visible and non-blocking); check the consensus section of the genesis."; rm -f "$tmp"; }
  echo "[chain] VRF bootstrap: committee_seed_source=$SEEDSRC, committee_min_vrf_contributors=$MINVRF, vote_extensions_enable_height=$VEH (each validator must anchor its VRF key: deploy/testnet/anchor_vrf_key.sh)"

  # ADR-022 — SLASHABLE AVAILABILITY (liveness), sliding-window form.
  # DORMANT by default (DENDRA_AVAIL_SLASH != 1 -> nothing is written, availability slashing is OFF and
  # the mandatory vrf_pubkey holds back farming). Arming is DENDRA_AVAIL_SLASH=1, and only after a live
  # proof of no false positives at soft launch (an honest miner survives a brief outage, and the real
  # p_miss corresponds to ~99 % uptime). It writes ATOMICALLY epoch=300 plus the 5 slash params
  # calibrated for the SLIDING window (k=5/W=25 for consumer hardware: honest bleed at 99 % uptime
  # ~0.5 %/year, exact chronic-failure rate 16 %, a 20-minute outage survived; see
  # bench-results/avail-slash-calibration-sliding.json). The tumbling-window calibration k=4/W=20 no
  # longer applies: the keeper algorithm changed. Writing the params atomically makes it impossible to
  # arm with a mismatched epoch. Every value is env-overridable. Params.Validate requires this
  # machinery whenever slash_bps > 0 (including W <= 64, the capacity of the bitmask).
  if [ "${DENDRA_AVAIL_SLASH:-0}" = "1" ]; then
    AEB="${DENDRA_AVAIL_EPOCH_BLOCKS:-300}"; ADL="${DENDRA_AVAIL_DEADLINE_BLOCKS:-150}"
    AFK="${DENDRA_AVAIL_FAIL_K:-5}"; AFW="${DENDRA_AVAIL_FAIL_WINDOW:-25}"
    ASB="${DENDRA_AVAIL_SLASH_BPS:-500}"; ASM="${DENDRA_AVAIL_SLASH_MAX:-0}"
    # OPERATOR guard: a clear message BEFORE the jq. Without it validate-genesis still rejects the
    # result (fail closed), but with an obscure trace. It reproduces the preconditions of
    # params.Validate() for a fat-fingered env override. It also rejects epoch < 2*deadline, so an
    # override such as DENDRA_AVAIL_EPOCH_BLOCKS=16 (which satisfies "16 > 0") can no longer break the
    # calibration SILENTLY: the deadline must stay within half an epoch. Window > 64 is rejected too
    # (bitmask capacity; Params.Validate would reject it anyway).
    if ! { [ "$AFK" -gt 0 ] && [ "$AFW" -ge "$AFK" ] && [ "$AFW" -le 64 ] && [ "$ADL" -gt 0 ] && [ "$AEB" -ge $((2 * ADL)) ] && [ "$ASB" -le 10000 ]; } 2>/dev/null; then
      echo "[chain] FATAL: inconsistent ADR-022 arming (requires avail_fail_k>0, avail_fail_k<=avail_fail_window<=64 [sliding bitmask], avail_deadline_blocks>0, avail_epoch_blocks>=2*avail_deadline_blocks [deadline within half an epoch, per the calibration], avail_slash_bps<=10000). Got epoch=$AEB deadline=$ADL k=$AFK window=$AFW slash=$ASB max=$ASM"; exit 1
    fi
    tmp=$(mktemp)
    jq --arg eb "$AEB" --arg dl "$ADL" --arg k "$AFK" --arg w "$AFW" --arg s "$ASB" --arg m "$ASM" '
        .app_state.jobs.params.avail_epoch_blocks = $eb
      | .app_state.jobs.params.avail_deadline_blocks = $dl
      | .app_state.jobs.params.avail_fail_k = $k
      | .app_state.jobs.params.avail_fail_window = $w
      | .app_state.jobs.params.avail_slash_bps = $s
      | .app_state.jobs.params.avail_slash_max = $m
    ' "$GEN" > "$tmp" && mv "$tmp" "$GEN" || { echo "[chain] FATAL: ADR-022 arming (avail slash) jq failed"; exit 1; }
    echo "[chain] ADR-022 ARMED: availability slashing ON (epoch=$AEB deadline=$ADL k=$AFK/$AFW slash=${ASB}bps max=$ASM) -- has the live no-false-positive proof been done?"
  else
    echo "[chain] ADR-022 availability slashing DORMANT (DENDRA_AVAIL_SLASH!=1) -- arm ONLY after the live no-false-positive proof at soft launch"
  fi

  # CONSENSUS DOWNTIME (x/slashing) -- THE SDK DEFAULTS ARE NOT SURVIVABLE ON THIS NETWORK.
  # Nothing in this repository ever wrote a slashing section, so every chain booted from this file has
  # run the pinned SDK's defaults: signed_blocks_window=100, min_signed_per_window=0.5,
  # downtime_jail_duration=600s, slash_fraction_downtime=0.01 (x/slashing/types/params.go:12-19).
  # Those are the values a validator inherits by nobody deciding anything.
  #
  # WHAT THEY DECIDE, READ FROM THE FUNCTION THAT APPLIES THEM RATHER THAN FROM THEIR NAMES.
  # `HandleValidatorSignature` computes, in x/slashing/keeper/infractions.go:
  #     maxMissed := signedBlocksWindow - round(signedBlocksWindow * minSignedPerWindow)      (:110)
  # and jails plus slashes as soon as `MissedBlocksCounter > maxMissed`                       (:113).
  # At the defaults maxMissed is 100 - 50 = 50, so the 51st missed block of the window jails the
  # validator AND burns 1 % of its stake.
  # THE TOLERANCE IS A COUNT OF BLOCKS; what turns it into a duration is the block interval, which
  # consensus does not fix -- it emerges from validator `timeout_commit` and network propagation.
  # Measured on the public endpoint at 5.03 s per block (two independent spans, 200 and 1000 blocks):
  # 51 blocks is 4 MINUTES 17 SECONDS. A router reboot, an image pull or a home-ISP reconnection is
  # longer than that. For operators on domestic connections the default is not a deterrent, it is a
  # scheduled confiscation -- and being jailed also removes the node from the VRF contributor count,
  # which is what the audit committee draw depends on.
  #
  # THE VALUES BELOW, EACH DERIVED. Block counts first; every duration names the 5.03 s/block it
  # assumes -- MEASURED on the public endpoint over three independent spans (heights 88000->88200,
  # 89000->89500 and 89182->90182), 5.029 s per block on each of the three. A duration written without
  # that clause is a claim about a parameter consensus does not fix, so there are none below.
  #  - signed_blocks_window = 10000 blocks -> uptime is judged over the last 10000 blocks, which is
  #    ~13 h 58 at 5.03 s/block.
  #  - min_signed_per_window = 0.85 -> maxMissed = 10000 - 8500 = 1500, so the jail fires on the 1501st
  #    missed block: 1501 blocks of continuous absence, ~2 h 06 at 5.03 s/block, against the 51 blocks
  #    the SDK defaults allow today (~4 min 17 at the same 5.03 s/block) -- 29x. A validator holding
  #    99 % uptime misses ~100 blocks per window and is nowhere near the bar; one at 84 % is past it.
  #    Side effect of the same window, and a welcome one: `height > minHeight` (:109/:113) gives a
  #    freshly bonded or freshly unjailed validator a full window -- 10000 blocks, ~14 h at
  #    5.03 s/block -- before it can be jailed at all.
  #  - THE COMPARISON POINT, AND WHY ONLY ITS SHAPE TRANSFERS. The reference usually quoted for a
  #    production Cosmos chain is a window of 10000 blocks at min_signed 0.05 (the Cosmos Hub). THAT
  #    FIGURE IS NOT MEASURABLE FROM THIS REPOSITORY -- it is a second-hand number, so only its SHAPE is
  #    borrowed here: a window of THOUSANDS of blocks rather than a hundred. The RATIO deliberately is
  #    not borrowed. A ratio of 0.05 only catches a validator that signed almost nothing for a whole
  #    window -- reasonable on a set of a hundred, too far on a set of three where losing one node is a
  #    third of the power and where the jail is the thing that publishes "this operator is gone". And
  #    the same 10000 blocks are not the same duration on two chains whose intervals differ, so the
  #    count cannot be copied for the duration it is imagined to represent either. 0.85 is where a
  #    hiccup costs nothing and a real absence still costs the seat.
  #  - slash_fraction_downtime = 0.0005 (0.05 %). What removes an unreliable validator is the JAIL:
  #    immediate exit from the set, no rewards, no VRF contribution, and a MANUAL MsgUnjail before it
  #    can return. The token slash exists so that being jailed is not costless -- not so that an outage
  #    is expensive. It fires ONCE per jail event: the counter is zeroed at :160 and a jailed validator
  #    stops accumulating misses, so a large per-event fraction cannot be defended by "it adds up", it
  #    does not. 0.05 % is legible in a balance query and scales with stake. It is not decorative: it is
  #    still 5x what the reference chain applies for downtime, on a network whose stakes are far smaller.
  #  - downtime_jail_duration = 600s, DELIBERATELY UNCHANGED. Lengthening it would reach almost nobody
  #    new -- under the window above, being jailed at all already means 1501 missed blocks, ~2 h 06 at
  #    5.03 s/block -- while holding the validator out of the VRF contributor count for longer, and on
  #    a set this small that count is the scarcer resource. The friction that matters is that MsgUnjail
  #    is a human act, not its delay.
  #  - slash_fraction_double_sign = 0.05, UNCHANGED. Downtime is an accident, equivocation is not. It is
  #    written here only so that an operator can read the WHOLE slashing policy of this deployment from
  #    one place instead of inferring the unwritten half from the SDK.
  #
  # RANGE GUARD, FATAL, BECAUSE THE MODULE'S OWN VALIDATION DOES NOT COVER THE SHAPES THAT MATTER.
  # `Params.Validate` refuses a window <= 0, a ratio outside [0,1] and a jail duration <= 0
  # (x/slashing/types/params.go:73, :92, :105). It ACCEPTS min_signed_per_window = exactly 1, where
  # maxMissed is 0 and the FIRST missed block jails; and it ACCEPTS a slash fraction of 0, where the
  # downtime penalty is silently disarmed (:140 refuses only a negative). Both are one env override
  # away and neither is visible afterwards. So the ratios are taken as BASIS POINTS -- integer
  # arithmetic, no floating point, no locale-dependent decimal parsing -- and the tolerance is checked
  # in the unit where it is decided, BLOCKS, after the same rounding the keeper applies.
  #
  # A THRESHOLD BOUNDED ON ONE SIDE ONLY IS NOT A THRESHOLD, AND ITS MESSAGE IS THE WORST PART.
  # `min_signed_bps=1` satisfies "1 <= min_signed_bps <= 10000" and yields min_signed_per_window =
  # 0.0001, i.e. maxMissed = 9999 in a 10000-block window: ONE signed block per window keeps the seat,
  # for ever. Measured against the keeper's own arithmetic (cosmossdk.io/math v1.5.3, the version
  # chain/go.mod pins: LegacyDec("0.0001").MulInt64(10000).RoundInt64() = 1). A guard that accepts
  # that while printing "0 would disarm the downtime jail entirely" does not protect the jail, it
  # certifies it. Every threshold below is therefore bounded on BOTH sides, and each bound is derived:
  #  - TOLERANCE FLOOR, 60 BLOCKS. The shortest tolerance that stays meaningful at BOTH intervals this
  #    project has on record: ~1 min at the x/chaintime target of 1 s per block, ~5 min at the
  #    5.03 s/block measured above. Below it the jail fires on ordinary restart jitter.
  #  - TOLERANCE CEILING, HALF THE WINDOW. `min_signed_per_window` names a REQUIRED PRESENCE RATE. Past
  #    half the window it declares a validator absent for the MAJORITY of the window to be in good
  #    standing -- the jail then stops publishing "this operator is gone", and a seat that still counts
  #    as a VRF contributor stops being evidence of anything. Written in BLOCKS (`2 * tolerance <=
  #    window`) rather than as a bps floor, so that it is read in the unit the keeper decides in; on an
  #    ODD window a ratio of exactly 0.5 lands one block above half and is refused, which is the bound
  #    doing its job rather than an off-by-one.
  #  - WINDOW FLOOR, 120 BLOCKS, DERIVED FROM THE TWO ABOVE (2 x 60) and not from the SDK. It is the
  #    smallest window on which a tolerance of at least 60 blocks and a presence requirement of at
  #    least half the window can both hold. A floor of 100 -- the SDK's own window, copied -- would be
  #    a floor no value of min_signed_bps could satisfy, i.e. a FATAL that contradicts its own message.
  #  - WINDOW CEILING, 100000 BLOCKS. The window is ALSO the immunity of a freshly bonded or freshly
  #    unjailed validator: no downtime jail applies before `height > StartHeight + signedBlocksWindow`
  #    (:113). 100000 blocks is already more than the entire height of the public chain (90182, read
  #    from /status on 2026-08-07), so a window above it means a validator bonded at genesis has not
  #    been jailable at any point in this network's life -- a policy written "armed" and behaving
  #    "disarmed". It also keeps every product here far below 2^63, which the previous shape did not:
  #    `signed_blocks_window = 9223372036854775807` PASSED it, because `SBW * MSBPS` wrapped to -8500
  #    and the tolerance came out as 9223372036854775807 (measured, bash 5.3.9) -- a number that
  #    satisfies ">= 60" and disarms the jail for good. Hence also the order below: the magnitudes are
  #    checked BEFORE any product is formed, never after.
  #  - JAIL CEILING, THE GENESIS'S OWN unbonding_time, READ FROM THE FILE BEING WRITTEN rather than
  #    copied: a jail at least as long as the unbonding period is not a pause, it is an eviction --
  #    the stake finishes unbonding before the seat can be reclaimed, and `downtime_jail_duration`
  #    would then name something it does not do. Params.Validate only refuses <= 0.
  SBW="${DENDRA_SLASH_SIGNED_BLOCKS_WINDOW:-10000}"
  MSBPS="${DENDRA_SLASH_MIN_SIGNED_BPS:-8500}"
  DJS="${DENDRA_SLASH_DOWNTIME_JAIL_SECONDS:-600}"
  DTBPS="${DENDRA_SLASH_DOWNTIME_BPS:-5}"
  DSBPS="${DENDRA_SLASH_DOUBLE_SIGN_BPS:-500}"
  # (1) SHAPE, before any comparison. Two traps that a "digits only" test does not catch, both measured
  # in bash 5.3.9: a 19-digit value makes `[ x -ge 100 ]` ITSELF fail with rc=2 (the guard would then
  # be judging the exit status of a broken comparison, not the value); and a LEADING ZERO is read as
  # OCTAL by `[` and by `$(( ))` -- `[ 0600 -ge 60 ]` is true on the value 384, while the string
  # written into the genesis stays "0600s" and the chain reads 600s. The guard would judge a different
  # number from the one it writes. 18 digits is the widest that `[` still compares as itself.
  for _v in "$SBW" "$MSBPS" "$DJS" "$DTBPS" "$DSBPS"; do
    case "$_v" in
      ''|*[!0-9]*) echo "[chain] FATAL: every DENDRA_SLASH_* override must be a non-negative INTEGER, and the two ratios are BASIS POINTS, not decimals (8500 = 0.85, 5 = 0.0005). An UNSET or EMPTY override falls back to the armed default of this file and never to the SDK's -- \${VAR:-default} treats the two alike, so nothing here can be silently emptied back into the SDK policy. Got window=$SBW min_signed_bps=$MSBPS jail_s=$DJS downtime_bps=$DTBPS double_sign_bps=$DSBPS"; exit 1 ;;
      0|[1-9]*) : ;;
      *) echo "[chain] FATAL: a DENDRA_SLASH_* override carries a LEADING ZERO ('$_v'). Bash reads it as OCTAL in every comparison below (0600 -> 384) while the value written into the genesis stays the decimal string, so the guard would judge one number and the chain another. Drop the zero. Got window=$SBW min_signed_bps=$MSBPS jail_s=$DJS downtime_bps=$DTBPS double_sign_bps=$DSBPS"; exit 1 ;;
    esac
    if [ "${#_v}" -gt 18 ]; then
      echo "[chain] FATAL: a DENDRA_SLASH_* override has ${#_v} digits ('$_v'). Past 18 digits bash cannot compare the value at all (rc=2), so no range check below would mean anything. Got window=$SBW min_signed_bps=$MSBPS jail_s=$DJS downtime_bps=$DTBPS double_sign_bps=$DSBPS"; exit 1
    fi
  done
  # (2) MAGNITUDES, still before any product.
  if ! { [ "$SBW" -ge 120 ] && [ "$SBW" -le 100000 ] && [ "$MSBPS" -ge 1 ] && [ "$MSBPS" -le 10000 ] \
      && [ "$DJS" -ge 60 ] && [ "$DTBPS" -ge 1 ] && [ "$DTBPS" -le 10000 ] \
      && [ "$DSBPS" -ge 100 ] && [ "$DSBPS" -le 10000 ]; } 2>/dev/null; then
    echo "[chain] FATAL: inconsistent slashing policy. Requires 120<=signed_blocks_window<=100000 (below 120 no ratio can satisfy the tolerance bounds at once; above 100000 the window is longer than the whole height this chain has reached, so it measures an immunity rather than recent uptime, and the products in this guard stop being exact); 1<=min_signed_bps<=10000 (0 would mean a validator never has to sign, which disarms the downtime jail entirely -- and see the tolerance check, which is what actually keeps the jail meaningful); downtime_jail_seconds>=60; 1<=downtime_bps<=10000 (0 disarms the downtime slash); 100<=double_sign_bps<=10000 (equivocation stays punished at >=1 %, it is not an accident). Got window=$SBW min_signed_bps=$MSBPS jail_s=$DJS downtime_bps=$DTBPS double_sign_bps=$DSBPS"; exit 1
  fi
  # (3) JAIL vs UNBONDING, read from the genesis this script is writing. An unreadable bound is a
  # REFUSAL, never a skipped check: "unknown" must not be spelled "fine".
  _UBT="$(jq -r '.app_state.staking.params.unbonding_time // ""' "$GEN" 2>/dev/null)"
  case "$_UBT" in
    *[0-9]s) _UBS="${_UBT%s}" ;;
    *) echo "[chain] FATAL: .app_state.staking.params.unbonding_time could not be read as a number of seconds (got '$_UBT'). The jail ceiling is derived from it, and a bound that cannot be read is refused rather than dropped."; exit 1 ;;
  esac
  case "$_UBS" in
    *[!0-9]*) echo "[chain] FATAL: .app_state.staking.params.unbonding_time is not an integer number of seconds (got '$_UBT')."; exit 1 ;;
  esac
  if [ "${#_UBS}" -gt 18 ] || ! [ "$DJS" -lt "$_UBS" ] 2>/dev/null; then
    echo "[chain] FATAL: downtime_jail_seconds=$DJS is not shorter than the staking unbonding_time (${_UBS}s) of this genesis. A jail that outlasts unbonding is not a jail: the stake leaves before the seat can be reclaimed, so the parameter would name a pause and perform an eviction."; exit 1
  fi
  # (4) TOLERANCE, in BLOCKS, with the keeper's own tie-break. `RoundInt64` is BANKERS rounding -- round
  # half to EVEN, `chopPrecisionAndRound` in cosmossdk.io/math legacy_dec.go -- and NOT round-half-up.
  # The gap is one block and only ever one block, but this guard claims to compute what the chain
  # computes: at window=121 with a ratio of 0.5 the keeper's maxMissed is 61 (measured with
  # cosmossdk.io/math v1.5.3) where a half-up formula answers 60. The keeper builds the count in
  # x/slashing/keeper/params.go, `MinSignedPerWindow` -> `MulInt64(signedBlocksWindow).RoundInt64()`,
  # and subtracts it at x/slashing/keeper/infractions.go:110.
  _a=$(( SBW * MSBPS )); _q=$(( _a / 10000 )); _r=$(( _a % 10000 ))
  if [ "$_r" -gt 5000 ]; then
    _q=$(( _q + 1 ))
  elif [ "$_r" -eq 5000 ] && [ $(( _q % 2 )) -eq 1 ]; then
    _q=$(( _q + 1 ))
  fi
  _TOL=$(( SBW - _q ))
  if ! { [ "$_TOL" -ge 60 ] && [ $(( 2 * _TOL )) -le "$SBW" ]; } 2>/dev/null; then
    echo "[chain] FATAL: signed_blocks_window=$SBW with min_signed_bps=$MSBPS leaves a tolerance of ${_TOL} blocks, outside [60, $(( SBW / 2 ))]. BELOW 60 the jail fires on ordinary restart jitter (and at min_signed_bps=10000 the tolerance is 0, so the FIRST missed block jails -- Params.Validate accepts that, this does not). ABOVE half the window the parameter declares a validator absent for most of the window to be in good standing, which is not a jail: min_signed_bps=1 leaves 9999 missable blocks out of 10000."; exit 1
  fi
  # bps -> the 18-decimal string the LegacyDec fields carry. Built by integer arithmetic on purpose:
  # `awk '{printf "%.18f", 8500/10000}'` prints 0.849999999999999978, a DIFFERENT parameter.
  _dec() { printf '%d.%04d%s' "$(( $1 / 10000 ))" "$(( $1 % 10000 ))" '00000000000000'; }
  tmp=$(mktemp)
  # FATAL, like the jobs block and unlike the VRF one: a slashing patch that fails quietly leaves the
  # SDK defaults in place, and those defaults are the state this block exists to leave.
  jq --arg sbw "$SBW" --arg msw "$(_dec "$MSBPS")" --arg djd "${DJS}s" \
     --arg sfds "$(_dec "$DSBPS")" --arg sfdt "$(_dec "$DTBPS")" '
      .app_state.slashing.params.signed_blocks_window       = $sbw
    | .app_state.slashing.params.min_signed_per_window      = $msw
    | .app_state.slashing.params.downtime_jail_duration     = $djd
    | .app_state.slashing.params.slash_fraction_double_sign = $sfds
    | .app_state.slashing.params.slash_fraction_downtime    = $sfdt
  ' "$GEN" > "$tmp" && mv "$tmp" "$GEN" || { echo "[chain] FATAL: slashing params jq patch failed"; exit 1; }
  echo "[chain] slashing: window=$SBW blocks, min_signed=$(_dec "$MSBPS") -> a validator is jailed once it has"
  echo "        missed MORE THAN ${_TOL} blocks inside the window; downtime slash=$(_dec "$DTBPS"), jail=${DJS}s,"
  echo "        double-sign slash=$(_dec "$DSBPS"). BLOCKS are the parameter; the duration ${_TOL} blocks represent"
  echo "        depends on the block interval, which consensus does not fix - measure it, do not assume it."

  # STDERR IS KEPT ON FAILURE. `gentx` re-validates the whole genesis before signing, so it is the
  # FIRST command that rejects a bad parameter set — earlier than the explicit validate-genesis below.
  # Discarding its stderr turned every such rejection into a bare exit 1: the container stopped after
  # printing a normal-looking boot log, and the operator had nothing to read. Measured on
  # DENDRA_COMMITTEE_SEED_SOURCE=0, where Params.Validate refuses the combination and the only signal
  # was the exit code. The message now survives, whatever the parameter at fault.
  GTX_ERR=$(mktemp)
  if ! $BIN genesis gentx validator 1000000000000$DENOM --chain-id "$CHAIN_ID" $KB >/dev/null 2>"$GTX_ERR"; then
    echo "[chain] FATAL: gentx refused this genesis. It re-validates every module parameter before" >&2
    echo "        signing, so the reason below is the reason the chain would not have accepted:" >&2
    cat "$GTX_ERR" >&2; rm -f "$GTX_ERR"; exit 1
  fi
  if ! $BIN genesis collect-gentxs --home "$HOME_DIR" >/dev/null 2>"$GTX_ERR"; then
    echo "[chain] FATAL: collect-gentxs failed:" >&2; cat "$GTX_ERR" >&2; rm -f "$GTX_ERR"; exit 1
  fi
  rm -f "$GTX_ERR"
  if $BIN genesis validate-genesis --home "$HOME_DIR" >/dev/null 2>&1; then
    echo "[chain] genesis VALID."
  else
    # Fail at TOOLING level (clean failure) rather than crash at boot. This catches, among other
    # things, a genesis whose params violate the positive-EV invariant, via GenesisState.Validate().
    echo "[chain] FATAL: validate-genesis failed (invalid genesis; e.g. the positive-EV params invariant):" >&2
    $BIN genesis validate-genesis --home "$HOME_DIR" >&2 || true
    exit 1
  fi
  echo "[chain] ===== testnet genesis ready (frozen in the volume) ====="
else
  echo "[chain] existing genesis -> persistent state reused."
fi

# VRF BOOTSTRAP — the validator's VRF key is managed automatically:
#  - keygen when absent -> written to the home directory (dendrad loads it through DENDRA_VRF_KEY_FILE
#    and SIGNS its vote extensions from startup, with no restart);
#  - the pubkey is anchored on-chain IN THE BACKGROUND (idempotent, best effort) so the validator
#    COUNTS in the VRF aggregation (it contributes to `committee_min_vrf_contributors`). A failure
#    leaves anti-grinding red — visible, never blocking.
VRFKEY="$HOME_DIR/config/vrf_key"
if command -v dendra-vrf >/dev/null 2>&1; then
  [ -s "$VRFKEY" ] || { dendra-vrf keygen | cut -f1 > "$VRFKEY" && chmod 600 "$VRFKEY" && echo "[chain] validator VRF key generated ($VRFKEY)"; }
  (
    for _ in $(seq 1 90); do curl -s http://127.0.0.1:26657/status 2>/dev/null | grep -q latest_block_height && break; sleep 2; done
    VSK=$(cat "$VRFKEY" 2>/dev/null); VPK=$(dendra-vrf pubkey "$VSK" 2>/dev/null); A=$($BIN keys show validator -a $KB 2>/dev/null)
    POP=$(dendra-vrf prove "$VSK" "dendra/vrf-pop/$A" 2>/dev/null)
    # A one-shot best-effort anchor fails SILENTLY on the first-boot race, and the node then does not
    # contribute to the seed (anti-grinding red) until someone anchors by hand. Hence a bounded retry.
    # Success means "code":0 in the JSON response, not exit 0 from the binary: a broadcast tx can carry
    # code != 0.
    # `"code":0` in a broadcast response means the tx entered the MEMPOOL, NOT that it EXECUTED. An
    # accepted tx can still fail at execution (account number unreadable at signing time, funds, state
    # changed meanwhile) and the anchoring then NEVER happened -- while this script prints "anchored".
    # The concrete consequence: the validator does not contribute to the seed, so anti-grinding is
    # INACTIVE, silently. Only `query tx <hash>` returns the EXECUTION result.
    # (deploy/testnet/anchor_vrf_key.sh applies the same hardening.)
    ok=0
    for _ in $(seq 1 10); do
      OUT=$($BIN tx jobs register-validator-vrf-key "$VPK" "$POP" --from validator $KB --chain-id "$CHAIN_ID" --node tcp://127.0.0.1:26657 --yes -o json 2>/dev/null)
      HASH=$(printf '%s' "$OUT" | tr -d ' \t' | grep -o '"txhash":"[A-Fa-f0-9]*"' | head -1 | cut -d'"' -f4)
      if [ -n "$HASH" ]; then
        # let the block include the tx, then READ the execution result
        for _ in $(seq 1 12); do
          sleep 5
          QOUT=$($BIN query tx "$HASH" --node tcp://127.0.0.1:26657 -o json 2>/dev/null)
          QCODE=$(printf '%s' "$QOUT" | tr -d ' \t' | grep -o '"code":[0-9]*' | head -1 | cut -d: -f2)
          [ -n "$QCODE" ] && break
        done
        [ "$QCODE" = "0" ] && { ok=1; break; }
        [ -n "$QCODE" ] && echo "[chain] ERR: VRF anchoring REJECTED at execution (code=$QCODE) -> this validator does NOT contribute to the seed"
      fi
      sleep 30
    done
    if [ "$ok" = 1 ]; then echo "[chain] validator VRF pubkey anchored on-chain (automatic, code:0 verified)"
    else echo "[chain] ERR: automatic VRF anchoring NOT confirmed after 10 attempts -> anchor manually with deploy/testnet/anchor_vrf_key.sh (anti-grinding stays RED otherwise)"; fi
  ) &
else
  echo "[chain] (info) dendra-vrf missing from the image -> no automatic VRF anchoring (rebuild the chain image to enable it)"
fi

# CORS: let browser wallets / dApps reach the public RPC + REST (public testnet). Idempotent per boot.
CFGTOML="$HOME_DIR/config/config.toml"
[ -f "$CFGTOML" ] && sed -i 's|^cors_allowed_origins = .*|cors_allowed_origins = ["*"]|' "$CFGTOML"

# P2P: ACCEPT SEVERAL NODES FROM ONE ADDRESS. CometBFT defaults `allow_duplicate_ip` to false, which
# refuses a second inbound connection from an address that already holds one. The topology this
# project documents produces exactly that: an operator running a validator and a miner, each with its
# own node, reaches the seed from a single public IP — and so does anyone bringing up a second machine
# behind one home or office connection.
# WHAT MAKES IT EXPENSIVE IS THE ERROR IT PRODUCES, NOT THE REFUSAL ITSELF. The rejection lands during
# the encrypted handshake, so the joiner reads `auth failure: secret conn failed: connection reset by
# peer` — wording that points at the node ID, the genesis or the network path, none of which is at
# fault. It is deterministic, it survives every retry, and nothing on the joining side can diagnose or
# work around it, because the decision belongs entirely to the seed.
# The seed is public and permissionless, so one address holding several nodes is expected traffic, not
# an anomaly to guard against. The duplicate-IP rule protects a validator set against a single host
# claiming many peer slots; on a bootstrap seed it only turns away real operators.
[ -f "$CFGTOML" ] && sed -i 's|^allow_duplicate_ip = .*|allow_duplicate_ip = true|' "$CFGTOML"

# SNAPSHOTS — mandatory once a CONSENSUS-BREAKING change has occurred.
# Without a snapshot, a new node REPLAYS the history from genesis. The current binary writes
# `audit_committee/*` (ADR-032) where an older one did not: replaying a block from BEFORE the upgrade
# at which an audit was drawn recomputes an AppHash that differs from the one already signed in the
# header, and the replay panics. No height-guard value fixes this, because the history genuinely
# contains BOTH behaviors.
# The remedy is therefore not to replay correctly but to NOT REPLAY: snapshots are served, and the
# joiner state-syncs at a height AFTER the upgrade. Cost: a few MB every 500 blocks.
APPTOML="$HOME_DIR/config/app.toml"
if [ -f "$APPTOML" ]; then
  sed -i 's|^snapshot-interval = .*|snapshot-interval = 500|' "$APPTOML"
  sed -i 's|^snapshot-keep-recent = .*|snapshot-keep-recent = 5|' "$APPTOML"
  grep -q '^snapshot-interval' "$APPTOML" || printf '\n[state-sync]\nsnapshot-interval = 500\nsnapshot-keep-recent = 5\n' >> "$APPTOML"
  echo "[chain] snapshots ENABLED (every 500 blocks, 5 kept) -> joiners can state-sync"
  echo "        instead of replaying a history that contains a consensus-breaking change."
fi

echo "[chain] starting dendrad (RPC 0.0.0.0:26657, REST 1317 with CORS)"
exec $BIN start --home "$HOME_DIR" \
  --rpc.laddr tcp://0.0.0.0:26657 \
  --api.enable --api.address tcp://0.0.0.0:1317 --api.enabled-unsafe-cors \
  --grpc.address 0.0.0.0:9090 \
  --minimum-gas-prices 0$DENOM
