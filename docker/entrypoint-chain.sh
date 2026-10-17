#!/usr/bin/env bash
# entrypoint-chain.sh - persistent Dendra testnet node.
#   Runs `dendrad start` natively: no recompilation and no random keys between boots.
#   First boot : init + stable keys (validator/bob/community/team/reserve/gw/delegation/payout/generator,
#                the cold ones supplied as addresses) + genesis accounts
#                + optimistic params (ADR-025) + model registry (llama3.1:8b default + mistral-nemo
#                + nomic) + gentx + collect.
#   Later boots: a genesis is already in the volume -> state is reused (persistent).
# The fixed 10 M DNDR supply is preserved. gw holds 0 at genesis (funded by the faucet, like miners).
set -e
export PATH="$PATH:/go/bin:/usr/local/go/bin"
BIN=dendrad
HOME_DIR="${DENDRA_HOME:-/root/.dendra}"
# The chain id is read, never defaulted (ADR-048 item 8). The only plausible default is the name of a
# previous chain: a relaunch under a new name that forgot to pass it would build a genesis for the old
# network, and every service would then sign for a chain the network no longer runs. The network file
# carries it.
CHAIN_ID="${DENDRA_CHAIN_ID:-}"
[ -n "$CHAIN_ID" ] || { echo "[chain] FATAL: DENDRA_CHAIN_ID is not set. It comes from the network file (deploy/launch/.env.public.example, copied to .env by the launcher) and is never defaulted."; exit 1; }
DENOM=udndr
KB="--keyring-backend test --home $HOME_DIR"
# Service keys live apart from the validator's (ADR-046). The faucet and the gateway are the two services
# exposed to the internet, and mounting this whole home (the validator's key, its consensus key and every
# hot pocket) to reach the one key each signs with would expose all of it. Their two keys (faucet `bob`, gateway `gw`)
# are created in a separate keyring, on its own volume, which is all those services mount, so a
# compromised faucet holds the faucet's float, not the network. The Final Testnet Season `payout` and
# request `generator` keys go to the programme keyring below, for the same reason.
SVC_HOME="${DENDRA_SERVICE_HOME:-/root/.dendra-svc}"
SKB="--keyring-backend test --home $SVC_HOME"
mkdir -p "$SVC_HOME"
# Programme keys (ADR-047): the Final Testnet Season `payout` and request `generator` accounts sign
# from the programme's generator container, which faces no inbound traffic. Their keyring is a third
# one, on its own volume: neither the validator's home nor the keyring the faucet and the gateway mount.
PROG_HOME="${DENDRA_PROGRAMME_HOME:-/root/.dendra-prog}"
PKB="--keyring-backend test --home $PROG_HOME"
mkdir -p "$PROG_HOME"
GEN="$HOME_DIR/config/genesis.json"
GEN_DONE="$HOME_DIR/config/.dendra-genesis-complete"   # written as the last step of the build (see the end of the first boot)
command -v "$BIN" >/dev/null 2>&1 || { echo "[chain] FATAL: dendrad not found"; exit 1; }
command -v jq    >/dev/null 2>&1 || { echo "[chain] FATAL: jq not found (add it to the Dockerfile)"; exit 1; }
mkdir -p "$HOME_DIR"

# A genesis already in the volume must belong to the network this node is told to run. A mismatch is a
# kit pointed at the wrong volume (or the reverse), and booting would serve one chain under another's
# name: refused, with both names.
if [ -f "$GEN" ]; then
  _gen_id="$(jq -r '.chain_id // ""' "$GEN" 2>/dev/null || true)"
  if [ "$_gen_id" != "$CHAIN_ID" ]; then
    echo "[chain] FATAL: the genesis in this volume is for chain '$_gen_id', but DENDRA_CHAIN_ID is '$CHAIN_ID'. Point the kit at the right volume, or relaunch with a fresh volume; nothing is overwritten."; exit 1
  fi
fi
if [ ! -f "$GEN" ]; then
  echo "[chain] ===== first boot: building the testnet genesis ($CHAIN_ID) ====="
  rm -rf "$HOME_DIR/config" "$HOME_DIR/data" 2>/dev/null || true
  $BIN init "${DENDRA_MONIKER:-dendra-testnet}" --chain-id "$CHAIN_ID" --default-denom "$DENOM" --home "$HOME_DIR" >/dev/null 2>&1

  # Cold addresses (optional, but only possible at genesis).
  # By default the keys are created inside the container, in the `test` keyring, i.e. as plaintext
  # mnemonics on a rented host. That is acceptable for service keys (validator/faucet/gw), which must
  # sign continuously. It is not acceptable for `reserve` (3.3 M) and `team` (0.5 M): 38 % of the supply
  # that almost never signs, and that a host compromise would take away for good.
  # When DENDRA_RESERVE_ADDR / DENDRA_TEAM_ADDR are supplied, the corresponding key is not generated
  # here: genesis credits an address whose private key has never touched this machine.
  # No spare key is generated alongside: it would land in keys-backup.jsonl and suggest, on the next
  # read, that this host holds the reserve.
  #
  # One name only: the team pocket has no former variable name and no fallback to one.
  # deploy/launch/launch_env_check.sh refuses a public launch unless DENDRA_TEAM_ADDR holds a dendra1
  # address, so a stack that still names the old variable stops before the first boot instead of minting
  # 0.5 M onto a key generated in this container. A fallback would have had to live in this file and in
  # the compose file, for a variable read at a single moment of a chain's life; a pocket minted onto a
  # hot key cannot be taken back without a new genesis.
  TEAM_COLD="${DENDRA_TEAM_ADDR:-}"
  # ADR-046: the large pockets are cold. Community (the programme's and the network's money) and the
  # delegation pocket (stake reserved for future validators) join the team: the key that holds them must
  # not live on the host it would compromise. The payout account (it pays the Final Testnet Season
  # weekly, funded with PAY_AMT below) may be cold too; left empty it is a service key here, like the
  # faucet's. The generator is always a service key: it opens the programme's requests and pays their
  # fees, so it must sign continuously.
  COM_COLD="${DENDRA_COMMUNITY_ADDR:-}"
  DEL_COLD="${DENDRA_DELEGATION_ADDR:-}"
  PAY_COLD="${DENDRA_PAYOUT_ADDR:-}"
  for k in validator bob community team reserve gw delegation payout generator; do
    [ "$k" = "reserve" ]    && [ -n "${DENDRA_RESERVE_ADDR:-}" ] && continue
    [ "$k" = "team" ]       && [ -n "$TEAM_COLD" ]               && continue
    [ "$k" = "community" ]  && [ -n "$COM_COLD" ]                && continue
    [ "$k" = "delegation" ] && [ -n "$DEL_COLD" ]                && continue
    [ "$k" = "payout" ]     && [ -n "$PAY_COLD" ]                && continue
    case "$k" in
      bob|gw)            _kb="$SKB"; _bk="$SVC_HOME/keys-backup.jsonl" ;;
      payout|generator)  _kb="$PKB"; _bk="$PROG_HOME/keys-backup.jsonl" ;;
      *)                       _kb="$KB";  _bk="$HOME_DIR/keys-backup.jsonl" ;;
    esac
    if ! $BIN keys show "$k" $_kb >/dev/null 2>&1; then
      $BIN keys add "$k" $_kb --output json >> "$_bk" 2>/dev/null
    fi
  done
  chmod 600 "$SVC_HOME/keys-backup.jsonl" "$PROG_HOME/keys-backup.jsonl" 2>/dev/null || true
  VAL=$($BIN keys show validator -a $KB)
  BOB=$($BIN keys show bob -a $SKB)
  COM="${COM_COLD:-$($BIN keys show community -a $KB)}"
  DEL="${DEL_COLD:-$($BIN keys show delegation -a $KB)}"
  PAYOUT="${PAY_COLD:-$($BIN keys show payout -a $PKB)}"
  GENR=$($BIN keys show generator -a $PKB)
  TEAM="${TEAM_COLD:-$($BIN keys show team -a $KB)}"
  # The reserve goes to the module account, whose address is derived, never taken from a key.
  # Taking it from a keyring key called "reserve" funds an ordinary account: the coins land at an address
  # the `emission` module cannot spend, while its `Reserve` counter still announces them. The chain then
  # boots, looks healthy, and defers every epoch from the first one with `security_transfer_failed`.
  # No transaction repairs that afterwards: it is decided here or not at all.
  # The derivation is `authtypes.NewModuleAddress("emission")` = bech32(sha256("emission")[:20]),
  # computed rather than copied. The constant below is a cross-check, not the source: if the prefix or
  # the derivation ever changes, the boot stops instead of quietly funding a different address.
  RES=$(python3 - <<'PYEOF'
import hashlib
CH="qpzry9x8gf2tvdw0s3jn54khce6mua7l"
def pm(v):
    g=[0x3B6A57B2,0x26508E6D,0x1EA119FA,0x3D4233DD,0x2A1462B3]; c=1
    for x in v:
        b=c>>25; c=((c&0x1FFFFFF)<<5)^x
        for i in range(5): c^= g[i] if ((b>>i)&1) else 0
    return c
def hx(h): return [ord(c)>>5 for c in h]+[0]+[ord(c)&31 for c in h]
def cb(d):
    a=b=0; o=[]
    for x in d:
        a=(a<<8)|x; b+=8
        while b>=5: b-=5; o.append((a>>b)&31)
    if b: o.append((a<<(5-b))&31)
    return o
raw=hashlib.sha256(b"emission").digest()[:20]; d=cb(list(raw))
c=pm(hx("dendra")+d+[0]*6)^1
print("dendra1"+"".join(CH[x] for x in d)+"".join(CH[(c>>5*(5-i))&31] for i in range(6)))
PYEOF
)
  RES_EXPECTED="dendra1q6tym5h6cd3cxcqua9eflkf5rct8c6rsn92wrf"
  [ "$RES" = "$RES_EXPECTED" ] || {
    echo "[chain] FATAL: the derived emission module address ($RES) does not match the pinned one ($RES_EXPECTED)."
    echo "        Either the bech32 prefix or the module derivation changed. Funding a different address"
    echo "        would strand the whole Reserve, so the boot stops here."; exit 1; }
  if [ -n "${DENDRA_RESERVE_ADDR:-}" ] && [ "$DENDRA_RESERVE_ADDR" != "$RES" ]; then
    echo "[chain] FATAL: DENDRA_RESERVE_ADDR=$DENDRA_RESERVE_ADDR is not the emission module account ($RES)."
    echo "        The Reserve is not an ordinary holding: x/emission spends it by module name, so any"
    echo "        other address makes the counter unbacked and every epoch defer."; exit 1
  fi
  GW=$($BIN keys show gw -a $SKB)
  # A mistyped address (stray space, truncation, wrong prefix) would go unnoticed: `add-genesis-account`
  # would fail, the boot would carry on, and the supply would be incomplete at block 1, with no fix.
  # So the form is checked before anything is created, and the boot is refused on a bad address.
  for _a in "$TEAM" "$RES" "$COM" "$DEL" "$PAYOUT"; do
    case "$_a" in
      dendra1*) [ ${#_a} -ge 39 ] || { echo "[chain] FATAL: genesis address too short: $_a"; exit 1; } ;;
      *) echo "[chain] FATAL: invalid genesis address (expected 'dendra1' prefix): $_a"; exit 1 ;;
    esac
  done
  echo "[chain] reserve = emission module account $RES (derived, no key holds it)"
  [ -n "$TEAM_COLD" ]               && echo "[chain] team    = cold address $TEAM (key not present on this machine)"
  [ -n "$COM_COLD" ]  && echo "[chain] community  = cold address $COM" || echo "[chain] WARNING: community key generated on this host (set DENDRA_COMMUNITY_ADDR for a public network)"
  [ -n "$DEL_COLD" ]  && echo "[chain] delegation = cold address $DEL" || echo "[chain] WARNING: delegation key generated on this host (set DENDRA_DELEGATION_ADDR for a public network)"
  [ -n "$PAY_COLD" ]  && echo "[chain] payout     = cold address $PAYOUT" || echo "[chain] payout     = service key on this host ($PAYOUT)"
  echo "[chain] generator  = service key on this host ($GENR)"
  echo "[chain] validator=$VAL"
  echo "[chain] bob(faucet)=$BOB"
  echo "[chain] gw(gateway)=$GW (funded by the faucet)"
  chmod 600 "$HOME_DIR/keys-backup.jsonl" 2>/dev/null || true

  # ADR-046: the pockets. The 10 000 000 DNDR supply is unchanged; what moves is who holds the keys.
  # The validator pocket (27 %) is split: the hot validator key keeps its self-bond and a margin, the
  # cold delegation pocket holds the rest for future validators. The community pocket (34 %) funds the
  # Final Testnet Season payout account and the request generator. The payout account keeps 15 000 DNDR,
  # which more than covers the season cap (3 000 DNDR since ADR-047 decision 22; cap_season of the rule set
  # in force is the bound that applies, not this balance).
  # The generator's 3 000 DNDR is an operating assumption, not a decided figure, funded once here. Its fees
  # are a bounded volume, whatever the number of miners: a day sends at most ATTEMPTS_PER_REQUEST x
  # requests_per_day requests, requests_per_day being that of the day's rule set
  # (final_season_rules.py::rules_for_day, RULE_HISTORY), over the days of the season (it ends at a fixed
  # time, ADR-047 decision 17), and each request escrows at most BASE_FEE + PER_TOKEN x (prompt tokens +
  # OUT_ALLOW) udndr (final_season_generator.py, client.py::quick_metered). Size it from those
  # constants, and read the account's balance on chain before a new rule set raises requests_per_day.
  VAL_AMT=700000000000;  DEL_AMT=2000000000000; BOB_AMT=100000000000
  PAY_AMT=15000000000;   GENR_AMT=3000000000;   COM_AMT=3382000000000
  TEAM_AMT=500000000000; RES_AMT=3300000000000
  $BIN genesis add-genesis-account "$VAL"    ${VAL_AMT}$DENOM $KB
  $BIN genesis add-genesis-account "$DEL"    ${DEL_AMT}$DENOM $KB
  $BIN genesis add-genesis-account "$BOB"    ${BOB_AMT}$DENOM $KB
  $BIN genesis add-genesis-account "$COM"    ${COM_AMT}$DENOM $KB
  $BIN genesis add-genesis-account "$PAYOUT" ${PAY_AMT}$DENOM $KB
  $BIN genesis add-genesis-account "$GENR"   ${GENR_AMT}$DENOM $KB
  $BIN genesis add-genesis-account "$TEAM"   ${TEAM_AMT}$DENOM $KB
  $BIN genesis add-genesis-account "$RES"    ${RES_AMT}$DENOM $KB
  # The total is read from the built file, not added up here: eight constants can agree with each other
  # and still not make 10 000 000, and what the chain will hold is what the genesis says.
  _tot=$(jq -r '[.app_state.bank.balances[].coins[] | select(.denom=="udndr") | .amount | tonumber] | add' "$HOME_DIR/config/genesis.json")
  [ "$_tot" = "10000000000000" ] || { echo "[chain] FATAL: the built genesis holds $_tot udndr, not the fixed supply of 10000000000000."; exit 1; }

  # `add-genesis-account` creates a BaseAccount. The balance alone would work today (bank resolves the
  # module by name to this same address), but auth would hold an ordinary account where a module lives,
  # which only works by accident. It is promoted to a real ModuleAccount so the state says what it is.
  # Empty permissions: emission neither mints nor burns (app_config.go).
  _g="$HOME_DIR/config/genesis.json"; _t=$(mktemp)
  jq --arg a "$RES" '
    .app_state.auth.accounts |= map(
      if .address == $a then
        {"@type":"/cosmos.auth.v1beta1.ModuleAccount",
         "base_account":{"address":$a,"pub_key":null,"account_number":.account_number,"sequence":"0"},
         "name":"emission","permissions":[]}
      else . end)
  ' "$_g" > "$_t" && mv "$_t" "$_g"

  # Run on a throwaway home, in the image that runs this file (`dendra/chain:latest`, jq 1.6): the two jq
  # programs below once shipped wrapped in literal quotes, so the check compared the filter text with the
  # expected amount, and `bash -n` cannot catch that. The corrected run boots the chain and reports
  # 3300000000000 udndr at the emission ModuleAccount.
  #
  # The check reads the built file rather than the intention: the source always declared this pocket
  # funded while the deployed genesis still credited an ordinary account.
  _bal=$(jq -r --arg a "$RES" '.app_state.bank.balances[] | select(.address==$a) | .coins[] | select(.denom=="udndr") | .amount' "$_g")
  _typ=$(jq -r --arg a "$RES" '.app_state.auth.accounts[] | select((.base_account.address // .address)==$a) | ."@type"' "$_g")
  _name=$(jq -r --arg a "$RES" '.app_state.auth.accounts[] | select((.base_account.address // .address)==$a) | .name // ""' "$_g")
  [ "$_bal" = "3300000000000" ] || { echo "[chain] FATAL: genesis balance at the emission module is '$_bal', expected 3300000000000."; exit 1; }
  [ "$_typ" = "/cosmos.auth.v1beta1.ModuleAccount" ] || { echo "[chain] FATAL: the emission pocket is a '$_typ', not a ModuleAccount."; exit 1; }
  [ "$_name" = "emission" ] || { echo "[chain] FATAL: the module account at $RES is named '$_name', not emission."; exit 1; }
  echo "[chain] verified in the built genesis: $_bal udndr at the emission ModuleAccount $RES"

  # Unified genesis: this jobs.params block must stay aligned with chain/config.optimistic.yml, the
  # published reference for the N1/N2/veto gates. A launch stack that enables optimistic settlement
  # without hold_bps (N1), without audit_min_quorum (the N=5 veto) and with timeout=30 (below N2)
  # bypasses the gates of optimistic mode entirely.
  # Only N1/N2/veto are security params to keep aligned. audit_sample_bps here differs on purpose from
  # the 10000 in config.optimistic.yml (demo = 100 % audited, slash visible); it is not a gate.
  tmp=$(mktemp)
  # audit_sample_bps is env-tunable. For a deterministic slash demonstration (the primary is drawn by a
  # uniform hash, not by stake, so at 50 % the cheater often slips through), set
  # DENDRA_AUDIT_SAMPLE_BPS=10000 (100 % audited): every job of the cheater is judged. 100 % audited is
  # also a stricter test of false slashes (every honest miner faces a judge). It is not a gate.
  # ADR-046: 20 % at the relaunch (raised to 50 % by governance once four jurors vote). Below a working
  # jury an audited job can only be refunded, so a lower rate pays more miners on the next block.
  ASB="${DENDRA_AUDIT_SAMPLE_BPS:-2000}"
  # audit_resolve_timeout is env-tunable: 120 blocks (~2 min) suffice for a warm homogeneous committee,
  # but two co-resident judge models (shared CPU, alternating loads) post their "0" verdicts after the
  # timeout -> the silent case resolves as no-quorum (negative-EV silence slash, but no hard slash).
  # Heterogeneous bench: DENDRA_AUDIT_RESOLVE_TIMEOUT=360. Default 120. (Validate requires it to be
  # greater than dispute_window=10.)
  ARTO="${DENDRA_AUDIT_RESOLVE_TIMEOUT:-120}"
  # Dispute bond. Left unset, the proto default of 0 makes disputing free, and the anti-grief confiscation
  # (unfounded dispute -> bond to the Treasury) confiscates nothing. The anchor intended by ADR-033 is
  # `min_stake`. That equality was once broken (a chain ran with a bond a twentieth of a miner identity, so
  # the capital for one identity bought twenty unfounded disputes). ADR-046 put the two defaults back level
  # (the line below and MINSTAKE further down), but they stay two separate settings: whether a given chain
  # holds the equality is a reading, `dendrad query jobs params -o json` -> `dispute_bond` and `min_stake`.
  # The value is an economic parameter and the owner's call: expensive enough that griefing costs
  # something, accessible enough that an honest user can still contest. It is not sized against a faucet
  # drip: a drip is the faucet's own setting (DENDRA_FAUCET_AMOUNT, set to `min_stake` in
  # deploy/launch/.env.public.example and read from the chain by
  # services/faucet.py::drip_amount), and the faucet's status reports what it grants.
  # It is refunded when a committee was summoned and did not answer, and refunded plus rewarded when the
  # dispute is upheld. Override with DENDRA_DISPUTE_BOND.
  # ADR-046: 1 DNDR, equal to min_stake, so contesting a verdict and registering an identity cost the same.
  DBOND="${DENDRA_DISPUTE_BOND:-1000000}"
  # Committee seed source: consensus-breaking (genesis state). It decides who gets to pick the jury.
  #   0 = legacy seed, AppHash(H-1):h. The block proposer knows both terms before proposing, so it can
  #       compute the draw offline and choose whether to include the job: the party being verified then
  #       selects its own verifiers, invisibly.
  #   1 = decentralised seed only. Without one, no draw happens: the audit is deferred, jobs wait visibly,
  #       and the backlog drains as soon as a seed exists. With a single validator no decentralised seed
  #       is possible when the floor below exceeds one contributor, so audits wait; 0 would instead give
  #       the sole proposer the randomness of the verification.
  # The default is 1 on purpose: the safe value must be the one obtained by doing nothing. It is a variable
  # rather than a literal inside the jq filter, so the deployment kit can see it.
  # Lowering it is not a silent downgrade: in optimistic mode `Params.Validate()` rejects a zero
  # `committee_reveal_delay` outright (a decentralised seed does not buy an exemption), and it also
  # rejects a non-zero `committee_min_vrf_contributors` posted without the source that reads it.
  # The refusal lands on `gentx` below, which re-validates the genesis before signing and so fires
  # before the explicit validate-genesis; either way the boot fails rather than starting a weaker chain.
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
  # Committee reveal delay: the second anti-grinding guard, and the one that still holds when the first
  # is unavailable.
  #
  # This entrypoint writes it. `Params.Validate` does not require it once committee_seed_source=1, so
  # left unwritten the chain would run with it at 0, a value chosen by proto3 omission rather than by
  # anyone. The `committee_reveal_delay: "1"` in chain/config.optimistic.yml belongs to another
  # deployment path and does not reach this genesis.
  #
  # Why 0 is not harmless even with seed_source=1: the decentralised seed is not always there. When
  # contributors sit below committee_min_vrf_contributors (a network that has just started, with fewer
  # validators than the floor), `committeeBaseSeedSourced` alerts and returns the legacy fallback. At
  # delay 0 that fallback is `height:time` as observed by the job creator at open time, so a creator can
  # reopen until the draw suits it. The audit draw is separately deferred in that regime
  # (audit_sampling.go), but the beacon has already been frozen on a predictable value, and the committee
  # is drawn from that beacon once contributors arrive.
  # At delay > 0 the beacon is left empty at open and fixed later on the AppHash of a future block, unknown
  # to the creator whatever the seed source is doing. The two guards are independent, and this one covers
  # the bootstrap.
  #
  # It is governable: chain/x/jobs/keeper/reveal_delay_governance_test.go moves it from 0 to 1 with
  # MsgUpdateParams on a live parameter set and shows OpenJob changing behaviour immediately, so it does
  # not require a chain reset. It is written here anyway, because the safe value must be the one obtained
  # by doing nothing rather than a governance fix on day one.
  #
  # 1 block is the minimum that closes the hole; higher values delay commits by that many blocks
  # (CreateCommit refuses until the seed is revealed) without adding unpredictability.
  RVDELAY="${DENDRA_COMMITTEE_REVEAL_DELAY:-1}"
  case "$RVDELAY" in
    ''|*[!0-9]*) echo "[chain] FATAL: DENDRA_COMMITTEE_REVEAL_DELAY must be a non-negative integer. Got: $RVDELAY"; exit 1 ;;
  esac
  if [ "$RVDELAY" = "0" ]; then
    echo "[chain] committee_reveal_delay=0 -> the job beacon is frozen inside the opening block."
    echo "        In optimistic mode (verification_mode=1) the chain refuses this at load: the boot"
    echo "        will fail on Params.Validate, it will not start a weaker chain. Setting"
    echo "        committee_seed_source=1 does not lift the refusal, and that is the point -- a"
    echo "        decentralised source stops a single actor from picking the seed, not the proposer"
    echo "        from seeing it. The proposer composes the very block the seed is fixed in: it reads"
    echo "        the pending job, assembles the vote extensions the seed is built from, and knows the"
    echo "        committee before it commits to proposing."
    echo "        In redundant mode (verification_mode=0) a zero delay stays a valid choice -- peer"
    echo "        divergence carries the verification there, so the committee is not the only check."
  fi
  # x/mint is de-registered from the app (structurally zero mint), so no mint section is written (an
  # unregistered module carrying a genesis section fails the boot); any leftover is purged instead
  # (del), in case an older genesis or volume still carries one.
  # juror_freshness_blocks is written here. Written by nobody, a relaunch restarts it at 0, which falls
  # back to the compiled constant `jurorFreshnessBlocks` and silently discards any governance vote that
  # changed it (on the previous chain, the only module parameter a passed proposal ever changed).
  # It belongs in this patch, whose failure is fatal (see below), not in the second one where a failure is
  # only a warning: a jury window is a security bound.
  # The value is in blocks, the unit the chain counts. Converting it to days would require an inter-block
  # interval that the consensus does not fix (it emerged at ~5.24 s on the previous chain).
  # ADR-046: 500000, the value the previous chain voted (proposal 2) after a shorter window had dropped
  # registered miners out of eligibility.
  JFB="${DENDRA_JUROR_FRESHNESS_BLOCKS:-500000}"
  case "$JFB" in ''|*[!0-9]*) echo "[FATAL] DENDRA_JUROR_FRESHNESS_BLOCKS=$JFB is not a block count"; exit 1;; esac
  # ADR-044 gesture 2, armed by ADR-046: a retention no jury can judge goes back to the client after
  # this many blocks (about a day at ~5 s per block, an assumption; the chain counts blocks). It must
  # be at least the resolve timeout, which Params.Validate also enforces; refused here with the reason.
  UNWIND="${DENDRA_AUDIT_UNWIND_BLOCKS:-17280}"
  case "$UNWIND" in ''|*[!0-9]*) echo "[FATAL] DENDRA_AUDIT_UNWIND_BLOCKS=$UNWIND is not a block count"; exit 1;; esac
  [ "$UNWIND" -ge "$ARTO" ] || { echo "[FATAL] DENDRA_AUDIT_UNWIND_BLOCKS=$UNWIND is below audit_resolve_timeout=$ARTO: the retention would be undone before the audit it waits for is retried once"; exit 1; }

  # Minimum stake. At 50000 udndr (0.05 DNDR) an identity costs nothing: a set of thirteen, most of them
  # never anchoring anything, costs about 0.65 DNDR in all, yet free identities fill the seats that decide
  # a slash. `antievasion.go` warns that four identities at `min_stake` could take 80 % of an honest bond,
  # which only holds at a real entry price.
  #
  # 1 000 000 udndr = 1 DNDR makes thirteen identities cost 13 DNDR. What that costs in faucet requests
  # depends on the drip, the faucet's own setting. With DENDRA_FAUCET_AMOUNT=min_stake
  # (deploy/launch/.env.public.example) one grant funds exactly one identity, so a sybil set costs one
  # request per identity, each under the faucet's per-address and per-IP quotas. It stays cheap for a
  # real miner.
  # This is the sybil price, not the draw's ceiling: the work draw weighs each identity's stake up to
  # assignment_stake_cap_multiple x min_stake, set below with the rest of the jobs params.
  MINSTAKE="${DENDRA_MIN_STAKE:-1000000}"
  case "$MINSTAKE" in ''|*[!0-9]*) echo "[FATAL] DENDRA_MIN_STAKE=$MINSTAKE is not a udndr amount"; exit 1;; esac
  [ "$MINSTAKE" -gt 0 ] || { echo "[FATAL] DENDRA_MIN_STAKE=0 disables the assignment ceiling (ADR-042): it is scaled by min_stake"; exit 1; }
  # Seeded models are inactive unless they carry a digest.
  #
  # `weights_sha256` is what binds a `model_id` declared in a commit to a real artifact. Models written
  # active with an empty digest into every genesis would leave one protection: the chain warns at InitChain
  # ("active model(s) at genesis without weights_sha256 ... Fix it before any incentivized launch") and
  # deliberately does not refuse, since a development genesis legitimately carries placeholders, so the
  # warning alone would stand between a placeholder and the incentivized network.
  #
  # The guard belongs here, where the genesis is composed. `active` is consulted at exactly one site
  # (`x/jobs/keeper/msg_server_commit.go`) and only under `enforce_model_registry`, which is false in this
  # genesis, so seeding these models inactive changes nothing today. The day the registry is armed, the
  # only way to activate a model becomes `RegisterModel`, which refuses an empty digest
  # (`msg_server_model.go:33`).
  #
  # The escape hatch is explicit: DENDRA_GENESIS_MODELS_ACTIVE=1 restores `active:true`, and the boot then
  # prints what that costs.
  MODELS_ACTIVE="${DENDRA_GENESIS_MODELS_ACTIVE:-0}"
  case "$MODELS_ACTIVE" in
    0|1) ;;
    *) echo "[chain] FATAL: DENDRA_GENESIS_MODELS_ACTIVE must be 0 or 1. Got: $MODELS_ACTIVE"; exit 1 ;;
  esac
  if [ "$MODELS_ACTIVE" = "1" ]; then
    _MACT=true
    echo "[chain] WARNING: seeded models are active with an empty weights_sha256"
    echo "        (DENDRA_GENESIS_MODELS_ACTIVE=1). There is then no model_id<->artifact binding: a"
    echo "        miner can declare one of these models and serve something else. Acceptable on a"
    echo "        devnet, not before an incentivized launch."
  else
    _MACT=false
    echo "[chain] seeded models are inactive (no weights_sha256 to bind them to an artifact)."
    echo "        Nothing changes while enforce_model_registry is false. To activate one, register it"
    echo "        with its digest (tx modelregistry register-model), which is the path that requires"
    echo "        the binding. DENDRA_GENESIS_MODELS_ACTIVE=1 restores the old behaviour."
  fi
  # assignment_stake_cap_multiple = 100 is the genesis value (ADR-047, decision 10),
  # the highest Params.Validate accepts. The work draw weighs each identity's stake up to multiple x
  # min_stake (committee.go::capAssignmentWeights): up to that ceiling the weight is the stake, so at this
  # value splitting a stake into more identities buys no more work. The multiple is governed, and a lower
  # one clips more stakes: every value governance can set from here is at least as strict. The value in
  # force is read on the chain (`dendrad query jobs params -o json`), never from this line.
  jq --arg asb "$ASB" --arg arto "$ARTO" --arg dbond "$DBOND" --arg seed "$SEEDSRC" --arg rvd "$RVDELAY" \
     --argjson mact "$_MACT" \
     --arg jfb "$JFB" --arg minstake "$MINSTAKE" --arg unwind "$UNWIND" --arg team "$TEAM" '
    del(.app_state.mint)
    | .app_state.jobs.params.juror_freshness_blocks = $jfb
    | .app_state.jobs.params.audit_unwind_blocks = $unwind
    | .app_state.jobs.params.team_address = $team
    | .app_state.jobs.params.verification_mode = "1"
    | .app_state.jobs.params.audit_sample_bps = $asb
    | .app_state.jobs.params.min_stake = $minstake
    | .app_state.jobs.params.assignment_stake_cap_multiple = "100"
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
    # 86400 blocks per emission epoch: the epoch is what is calibrated, not the rate.
    # The Reserve releases `reserve_release_bps` of what remains each epoch, so the curve is geometric and
    # the quantity that matters is the half-life, counted in epochs: at 2 bps it is ~6932 epochs on an idle
    # chain and ~3466 with a saturated gate, and those counts depend only on the bps. At 300 blocks per
    # epoch that was ~2.1 M / ~1.0 M blocks, so the Reserve would have been mostly gone before the testnet
    # ended. Lowering the rate cannot fix it: `reserve_release_bps` is a uint64, the next values below 2
    # are 1 and then 0, and 0 stops emission. So the epoch length is what moves.
    # In time, with the assumption stated: at the x/chaintime target of 1 s per block an epoch is one day
    # and the half-lives read 19.0 / 9.5 years; at the ~5 s per block measured on the public endpoint the
    # same epochs are five days and the half-lives ~95 / ~47.5 years. The block interval comes from
    # validator `timeout_commit` and network propagation, not from consensus, so only the epoch counts
    # are guaranteed.
    # Separate from jobs.avail_epoch_blocks (~300 blocks): availability challenges keep their fast cadence.
    # The two live in different modules and x/jobs reads no emission parameter.
    # Locked by chain/app/emission_lifespan_test.go, which pins the half-life range rather than the
    # integers, so a change is refused instead of followed. That range is expressed in years under the
    # declared block interval, and the test says so in its own name.
    | .app_state.emission.params.epoch_blocks = "86400"
    | .app_state.modelregistry.models = [
        {"id":"llama3.1:8b-instruct-q4_K_M","weights_sha256":"","quant":"q4_K_M","engine":"ollama","hw_class":"consumer-gpu","active":$mact,"hf_repo":"","hf_revision":""},
        {"id":"mistral-nemo","weights_sha256":"","quant":"","engine":"ollama","hw_class":"consumer-gpu","active":$mact,"hf_repo":"","hf_revision":""},
        {"id":"nomic-embed-text","weights_sha256":"","quant":"","engine":"ollama","hw_class":"consumer-gpu","active":$mact,"hf_repo":"","hf_revision":""}
      ]
  ' "$GEN" > "$tmp" && mv "$tmp" "$GEN" || { echo "[chain] FATAL: genesis jq patch failed"; exit 1; }

  # Decentralised VRF bootstrap, paired with committee_seed_source above. The asymmetry is deliberate:
  # seed_source is written by the jq block above, whose failure is fatal, while this block only warns.
  # A seed_source patched here on a best-effort basis could silently fall back to 0, the legacy seed,
  # which is what the pairing exists to prevent.
  #  - committee_min_vrf_contributors = the floor (ceil(2N/3) of the intended validator set, raised as N
  #    grows). A seed built from fewer contributors makes committeeBaseSeed raise an alert and fall back
  #    to the legacy seed (never a halt; the regression stays visible).
  #  - vote_extensions_enable_height = a consensus param (outside app_state) that enables ABCI++ vote
  #    extensions, through which the decentralised VRF produces the seed. With no VRF keys anchored,
  #    the extensions are empty -> alert plus legacy fallback (non-blocking).
  #  Each validator anchors its key with deploy/testnet/anchor_vrf_key.sh, then (re)starts with
  #  DENDRA_VRF_KEY_FILE set.
  # ADR-046: one validator at the relaunch, so the floor is 1; a floor of 2 would defer every draw
  # forever. As stated publicly, the operator alone produces the randomness of every draw until
  # independent validators join and governance raises the floor.
  MINVRF="${DENDRA_MIN_VRF_CONTRIB:-1}"; VEH="${DENDRA_VE_ENABLE_HEIGHT:-20}"
  # Operator guard on the contributor floor, like the ADR-022 one further down: a clear fatal message
  # before the jq, reproducing the precondition the chain applies, rather than an obscure trace later.
  # At 0 the floor is not dormant: it is the only bar that refuses a one-validator seed. The second bar,
  # MinVrfContributorPowerBps=6667 (committeeBaseSeedSourced), asks the contributors to carry 2/3 of the
  # commit's power, and a lone validator on a small set carries all of it. Only the count distinguishes
  # "the set agreed on a seed" from "one machine chose the randomness the audit committee is drawn from".
  # `Params.Validate` does not cover this: it rejects a floor above 0 without seed_source=1
  # (chain/x/jobs/types/params.go:206) and says nothing about 0 alongside seed_source=1.
  # The admissible value depends on the seed source, so the guard does too:
  #   seed_source=1 -> the floor must be >= 1;
  #   seed_source=0 -> the floor must be 0, since params.go:206 refuses anything else and the boot would
  #                    otherwise die at gentx with the reason buried in a validation trace.
  # No upper bound, on purpose: the floor is sized on the intended validator set (ceil(2N/3)), which is
  # larger than the set present at genesis (a first boot carries exactly one validator), so any ceiling
  # computable here would be wrong. A floor set too high is not silent either: every draw logs the
  # under-decentralized alert and the legacy fallback stays visible.
  # The value is checked as plain digits first: `[ " 2" -ge 1 ]` and `[ "+2" -ge 1 ]` are true in bash,
  # which skips blanks and accepts a sign, yet Go refuses those spellings for the uint64 the genesis
  # declares (params.proto), so the boot would die later in a codec trace. The same form is used above for
  # DENDRA_JUROR_FRESHNESS_BLOCKS and DENDRA_MIN_STAKE, so the guards of this script agree on what an
  # integer is.
  case "$MINVRF" in
    ''|*[!0-9]*) echo "[chain] FATAL: DENDRA_MIN_VRF_CONTRIB must be a non-negative integer written in plain digits -- no sign, no surrounding spaces, no 0x/1e form. It is written into the genesis as a uint64 (params.proto) and any other spelling makes the genesis unloadable at gentx, far from here. Got: [$MINVRF]"; exit 1 ;;
  esac
  if [ "$SEEDSRC" = "1" ]; then
    if ! { [ "$MINVRF" -ge 1 ]; } 2>/dev/null; then
      echo "[chain] FATAL: DENDRA_MIN_VRF_CONTRIB must be an integer >= 1 when DENDRA_COMMITTEE_SEED_SOURCE=1. At 0 the floor stops refusing anything: a decentralised seed produced by a single validator becomes acceptable for the committee draw, and that validator owns the randomness the audit is drawn from. Got: $MINVRF"; exit 1
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
    || { echo "[chain] WARNING: VRF bootstrap patch (min_vrf/vote_extensions) failed -> committee_seed_source=$SEEDSRC will alert (legacy fallback, visible and non-blocking); check the consensus section of the genesis."; rm -f "$tmp"; }
  echo "[chain] VRF bootstrap: committee_seed_source=$SEEDSRC, committee_min_vrf_contributors=$MINVRF, vote_extensions_enable_height=$VEH (each validator must anchor its VRF key: deploy/testnet/anchor_vrf_key.sh)"

  # ADR-022: slashable availability (liveness), sliding-window form.
  # Off by default (DENDRA_AVAIL_SLASH != 1 -> nothing is written, availability slashing is off and the
  # mandatory vrf_pubkey holds back farming). Arm it with DENDRA_AVAIL_SLASH=1, and only after a live
  # proof of no false positives at soft launch (an honest miner survives a brief outage, and the real
  # p_miss corresponds to ~99 % uptime). It writes epoch=300 and the 5 slash params together, calibrated
  # for the sliding window (k=5/W=25 for consumer hardware: honest bleed at 99 % uptime ~0.5 %/year,
  # exact chronic-failure rate 16 %, a 20-minute outage survived; see
  # bench-results/avail-slash-calibration-sliding.json). The tumbling-window calibration k=4/W=20 no
  # longer applies: the keeper algorithm changed. Writing the params together makes it impossible to
  # arm with a mismatched epoch. Every value is env-overridable. Params.Validate requires this
  # machinery whenever slash_bps > 0 (including W <= 64, the capacity of the bitmask).
  if [ "${DENDRA_AVAIL_SLASH:-0}" = "1" ]; then
    AEB="${DENDRA_AVAIL_EPOCH_BLOCKS:-300}"; ADL="${DENDRA_AVAIL_DEADLINE_BLOCKS:-150}"
    AFK="${DENDRA_AVAIL_FAIL_K:-5}"; AFW="${DENDRA_AVAIL_FAIL_WINDOW:-25}"
    ASB="${DENDRA_AVAIL_SLASH_BPS:-500}"; ASM="${DENDRA_AVAIL_SLASH_MAX:-0}"
    # Operator guard: a clear message before the jq. Without it validate-genesis still rejects the result
    # (fail closed), but with an obscure trace. It reproduces the preconditions of params.Validate() for a
    # mistyped env override. It also rejects epoch < 2*deadline, so an override such as
    # DENDRA_AVAIL_EPOCH_BLOCKS=16 (which satisfies "16 > 0") cannot silently break the calibration: the
    # deadline must stay within half an epoch. Window > 64 is rejected too (bitmask capacity;
    # Params.Validate would reject it anyway).
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
    echo "[chain] ADR-022 armed: availability slashing on (epoch=$AEB deadline=$ADL k=$AFK/$AFW slash=${ASB}bps max=$ASM) -- has the live no-false-positive proof been done?"
  elif [ "${DENDRA_AVAIL_MEASURE:-1}" = "1" ]; then
    # ADR-046: presence is measured, neither slashed nor paid on chain. The windows feed the presence rule
    # of the draws (ADR-048 item 5) and the presence reward of the programme (ADR-047), which reads the
    # proofs from the transaction index and pays them only on a day of verified work; on-chain slash and
    # payout stay at zero. A window of 288 blocks is about 24 minutes at ~5 s per block (an assumption),
    # and the deadline keeps the answer within the first half of it.
    AEB="${DENDRA_AVAIL_EPOCH_BLOCKS:-288}"; ADL="${DENDRA_AVAIL_DEADLINE_BLOCKS:-144}"
    if ! { [ "$ADL" -gt 0 ] && [ "$AEB" -ge $((2 * ADL)) ]; } 2>/dev/null; then
      echo "[chain] FATAL: availability measurement requires avail_deadline_blocks>0 and avail_epoch_blocks>=2*avail_deadline_blocks. Got epoch=$AEB deadline=$ADL"; exit 1
    fi
    tmp=$(mktemp)
    jq --arg eb "$AEB" --arg dl "$ADL" '
        .app_state.jobs.params.avail_epoch_blocks = $eb
      | .app_state.jobs.params.avail_deadline_blocks = $dl
      | .app_state.jobs.params.avail_payout_bps = "0"
      | .app_state.jobs.params.avail_slash_bps = "0"
    ' "$GEN" > "$tmp" && mv "$tmp" "$GEN" || { echo "[chain] FATAL: availability measurement jq failed"; exit 1; }
    echo "[chain] availability measured (epoch=$AEB deadline=$ADL), no slash, no payout: only present miners are drawn"
  else
    echo "[chain] ADR-022 availability slashing dormant (DENDRA_AVAIL_SLASH!=1) and presence not measured (DENDRA_AVAIL_MEASURE=0): every registered miner stays drawable"
  fi

  # Consensus downtime (x/slashing). The SDK defaults do not suit this network.
  # The values a chain inherits when nobody decides are the pinned SDK's: signed_blocks_window=100,
  # min_signed_per_window=0.5, downtime_jail_duration=600s, slash_fraction_downtime=0.01
  # (x/slashing/types/params.go:12-19). The network running today moved off them by governance rather
  # than by genesis, so the live values are a query, never a line in a comment.
  #
  # What they decide, read from the function that applies them. `HandleValidatorSignature` computes, in
  # the cosmos-sdk (x/slashing/keeper/infractions.go, a dependency, not this tree):
  #     maxMissed := signedBlocksWindow - round(signedBlocksWindow * minSignedPerWindow)
  # and jails plus slashes as soon as `MissedBlocksCounter > maxMissed`                       (:113).
  # At the defaults maxMissed is 100 - 50 = 50, so the 51st missed block of the window jails the
  # validator and burns 1 % of its stake.
  # The tolerance is a count of blocks; what turns it into a duration is the block interval, which
  # consensus does not fix (it emerges from validator `timeout_commit` and network propagation).
  # Measured on the public endpoint at 5.03 s per block (two independent spans, 200 and 1000 blocks),
  # 51 blocks is 4 minutes 17 seconds, shorter than a router reboot, an image pull or a home ISP
  # reconnection. Being jailed also removes the node from the VRF contributor count, which the audit
  # committee draw depends on.
  #
  # The values below, each derived. Block counts first; every duration names the 5.03 s/block it assumes,
  # measured on the public endpoint over three independent spans (heights 88000->88200, 89000->89500 and
  # 89182->90182), 5.029 s per block on each.
  #  - signed_blocks_window = 10000 blocks -> uptime is judged over the last 10000 blocks, which is
  #    ~13 h 58 at 5.03 s/block.
  #  - min_signed_per_window = 0.85 -> maxMissed = 10000 - 8500 = 1500, so the jail fires on the 1501st
  #    missed block: 1501 blocks of continuous absence, ~2 h 06 at 5.03 s/block, against the 51 blocks
  #    the SDK defaults allow (~4 min 17 at the same 5.03 s/block), 29 times more. A validator with 99 %
  #    uptime misses ~100 blocks per window and is nowhere near the bar; one at 84 % is past it.
  #    The same window has a welcome side effect: `height > minHeight` (:109/:113) gives a freshly bonded
  #    or freshly unjailed validator a full window (10000 blocks, ~14 h at 5.03 s/block) before it can be
  #    jailed at all.
  #  - The comparison point, and why only its shape transfers. The reference usually quoted for a
  #    production chain is a window of 10000 blocks at min_signed 0.05. That figure is second-hand and
  #    cannot be measured from this repository, so only its shape is borrowed: a window of thousands of
  #    blocks rather than a hundred. The ratio is not borrowed: 0.05 only catches a validator that signed
  #    almost nothing for a whole window, reasonable on a set of a hundred but not on a set of three where
  #    losing one node is a third of the power. And the same 10000 blocks are not the same duration on
  #    two chains with different intervals. 0.85 is where a hiccup costs nothing and a real absence still
  #    costs the seat.
  #  - slash_fraction_downtime = 0.0005 (0.05 %). What removes an unreliable validator is the jail:
  #    immediate exit from the set, no rewards, no VRF contribution, and a manual MsgUnjail before it can
  #    return. The token slash exists so that being jailed is not free, not so that an outage is
  #    expensive. It fires once per jail event: the counter is zeroed at :160 and a jailed validator
  #    stops accumulating misses, so a large per-event fraction does not add up. 0.05 % is visible in a
  #    balance query and scales with stake; it is still 5 times what the reference chain applies for
  #    downtime, on a network whose stakes are far smaller.
  #  - downtime_jail_duration = 600s, unchanged on purpose. Lengthening it would reach almost nobody new
  #    (being jailed at all already means 1501 missed blocks, ~2 h 06 at 5.03 s/block) while keeping the
  #    validator out of the VRF contributor count for longer, the scarcer resource on a set this small.
  #    The friction that matters is that MsgUnjail is a human act, not its delay.
  #  - slash_fraction_double_sign = 0.05, unchanged. Downtime is an accident, equivocation is not. It is
  #    written here only so that an operator can read the whole slashing policy of this deployment in
  #    one place.
  #
  # Range guard, fatal, because the module's own validation does not cover the cases that matter.
  # `Params.Validate` refuses a window <= 0, a ratio outside [0,1] and a jail duration <= 0
  # (x/slashing/types/params.go:73, :92, :105). It accepts min_signed_per_window = exactly 1, where
  # maxMissed is 0 and the first missed block jails, and it accepts a slash fraction of 0, which silently
  # disarms the downtime penalty (:140 refuses only a negative). Both are one env override away. So the
  # ratios are taken as basis points (integer arithmetic, no floating point, no locale-dependent decimal
  # parsing), and the tolerance is checked in blocks, the unit it is decided in, after the same rounding
  # the keeper applies.
  #
  # Every threshold is bounded on both sides. `min_signed_bps=1` satisfies "1 <= min_signed_bps <= 10000"
  # and yields min_signed_per_window = 0.0001, i.e. maxMissed = 9999 in a 10000-block window: one signed
  # block per window would keep the seat forever (cosmossdk.io/math v1.5.3, the version chain/go.mod
  # pins: LegacyDec("0.0001").MulInt64(10000).RoundInt64() = 1). Each bound is derived:
  #  - Tolerance floor, 60 blocks: the shortest tolerance that stays meaningful at both intervals this
  #    project has on record, ~1 min at the x/chaintime target of 1 s per block and ~5 min at the
  #    5.03 s/block measured above. Below it the jail fires on ordinary restart jitter.
  #  - Tolerance ceiling, half the window: `min_signed_per_window` is a required presence rate, and past
  #    half the window it would let a validator absent for most of the window stay in good standing.
  #    Written in blocks (`2 * tolerance <= window`) rather than as a bps floor, in the unit the keeper
  #    decides in; on an odd window a ratio of exactly 0.5 lands one block above half and is refused,
  #    which is the bound working, not an off-by-one.
  #  - Window floor, 120 blocks, derived from the two above (2 x 60), not from the SDK: the smallest
  #    window on which a tolerance of at least 60 blocks and a presence requirement of at least half the
  #    window can both hold. A floor of 100 (the SDK's window) could not be satisfied by any
  #    min_signed_bps.
  #  - Window ceiling, 100000 blocks. The window is also the immunity of a freshly bonded or freshly
  #    unjailed validator: no downtime jail applies before `height > StartHeight + signedBlocksWindow`
  #    (:113). A validator bonded at genesis cannot be jailed for downtime before the chain passes the
  #    window, so a longer window would leave a young chain with no downtime jail at all. The ceiling
  #    also keeps every product here far below 2^63: without it `signed_blocks_window = 9223372036854775807`
  #    would pass, because `SBW * MSBPS` wraps to -8500 and the tolerance comes out as 9223372036854775807
  #    (bash 5.3.9). Hence the order below: magnitudes are checked before any product is formed.
  #  - Jail ceiling, the genesis's own unbonding_time, read from the file being written rather than
  #    copied: a jail at least as long as the unbonding period is an eviction, since the stake finishes
  #    unbonding before the seat can be reclaimed. Params.Validate only refuses <= 0.
  SBW="${DENDRA_SLASH_SIGNED_BLOCKS_WINDOW:-10000}"
  MSBPS="${DENDRA_SLASH_MIN_SIGNED_BPS:-8500}"
  DJS="${DENDRA_SLASH_DOWNTIME_JAIL_SECONDS:-600}"
  DTBPS="${DENDRA_SLASH_DOWNTIME_BPS:-5}"
  DSBPS="${DENDRA_SLASH_DOUBLE_SIGN_BPS:-500}"
  # (1) Shape, before any comparison. Two traps that a "digits only" test does not catch, both measured in
  # bash 5.3.9: a 19-digit value makes `[ x -ge 100 ]` itself fail with rc=2 (the guard would then judge
  # the exit status of a broken comparison, not the value); and a leading zero is read as octal by `[` and
  # by `$(( ))`, so `[ 0600 -ge 60 ]` compares the value 384 while the genesis gets the string "0600s",
  # which the chain reads as 600s. 18 digits is the widest that `[` still compares correctly.
  for _v in "$SBW" "$MSBPS" "$DJS" "$DTBPS" "$DSBPS"; do
    case "$_v" in
      ''|*[!0-9]*) echo "[chain] FATAL: every DENDRA_SLASH_* override must be a non-negative integer, and the two ratios are basis points, not decimals (8500 = 0.85, 5 = 0.0005). An unset or empty override falls back to the armed default of this file and never to the SDK's -- \${VAR:-default} treats the two alike, so nothing here can be silently emptied back into the SDK policy. Got window=$SBW min_signed_bps=$MSBPS jail_s=$DJS downtime_bps=$DTBPS double_sign_bps=$DSBPS"; exit 1 ;;
      0|[1-9]*) : ;;
      *) echo "[chain] FATAL: a DENDRA_SLASH_* override carries a leading zero ('$_v'). Bash reads it as octal in every comparison below (0600 -> 384) while the value written into the genesis stays the decimal string, so the guard would judge one number and the chain another. Drop the zero. Got window=$SBW min_signed_bps=$MSBPS jail_s=$DJS downtime_bps=$DTBPS double_sign_bps=$DSBPS"; exit 1 ;;
    esac
    if [ "${#_v}" -gt 18 ]; then
      echo "[chain] FATAL: a DENDRA_SLASH_* override has ${#_v} digits ('$_v'). Past 18 digits bash cannot compare the value at all (rc=2), so no range check below would mean anything. Got window=$SBW min_signed_bps=$MSBPS jail_s=$DJS downtime_bps=$DTBPS double_sign_bps=$DSBPS"; exit 1
    fi
  done
  # (2) Magnitudes, still before any product.
  if ! { [ "$SBW" -ge 120 ] && [ "$SBW" -le 100000 ] && [ "$MSBPS" -ge 1 ] && [ "$MSBPS" -le 10000 ] \
      && [ "$DJS" -ge 60 ] && [ "$DTBPS" -ge 1 ] && [ "$DTBPS" -le 10000 ] \
      && [ "$DSBPS" -ge 100 ] && [ "$DSBPS" -le 10000 ]; } 2>/dev/null; then
    echo "[chain] FATAL: inconsistent slashing policy. Requires 120<=signed_blocks_window<=100000 (below 120 no ratio can satisfy the tolerance bounds at once; above 100000 the window is longer than the whole height this chain has reached, so it measures an immunity rather than recent uptime, and the products in this guard stop being exact); 1<=min_signed_bps<=10000 (0 would mean a validator never has to sign, which disarms the downtime jail entirely -- and see the tolerance check, which is what actually keeps the jail meaningful); downtime_jail_seconds>=60; 1<=downtime_bps<=10000 (0 disarms the downtime slash); 100<=double_sign_bps<=10000 (equivocation stays punished at >=1 %, it is not an accident). Got window=$SBW min_signed_bps=$MSBPS jail_s=$DJS downtime_bps=$DTBPS double_sign_bps=$DSBPS"; exit 1
  fi
  # (3) Jail vs unbonding, read from the genesis this script is writing. An unreadable bound is a
  # refusal, never a skipped check.
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
  # (4) Tolerance, in blocks, with the keeper's own tie-break. `RoundInt64` is banker's rounding (round
  # half to even, `chopPrecisionAndRound` in cosmossdk.io/math legacy_dec.go), not round-half-up. The gap
  # is at most one block, but this guard computes what the chain computes: at window=121 with a ratio of
  # 0.5 the keeper's maxMissed is 61 (measured with cosmossdk.io/math v1.5.3) where a half-up formula
  # answers 60. The keeper builds the count in x/slashing/keeper/params.go, `MinSignedPerWindow` ->
  # `MulInt64(signedBlocksWindow).RoundInt64()`, and subtracts it in cosmos-sdk's
  # x/slashing/keeper/infractions.go::HandleValidatorSignature.
  _a=$(( SBW * MSBPS )); _q=$(( _a / 10000 )); _r=$(( _a % 10000 ))
  if [ "$_r" -gt 5000 ]; then
    _q=$(( _q + 1 ))
  elif [ "$_r" -eq 5000 ] && [ $(( _q % 2 )) -eq 1 ]; then
    _q=$(( _q + 1 ))
  fi
  _TOL=$(( SBW - _q ))
  if ! { [ "$_TOL" -ge 60 ] && [ $(( 2 * _TOL )) -le "$SBW" ]; } 2>/dev/null; then
    echo "[chain] FATAL: signed_blocks_window=$SBW with min_signed_bps=$MSBPS leaves a tolerance of ${_TOL} blocks, outside [60, $(( SBW / 2 ))]. Below 60 the jail fires on ordinary restart jitter (and at min_signed_bps=10000 the tolerance is 0, so the first missed block jails -- Params.Validate accepts that, this does not). Above half the window the parameter declares a validator absent for most of the window to be in good standing, which is not a jail: min_signed_bps=1 leaves 9999 missable blocks out of 10000."; exit 1
  fi
  # bps -> the 18-decimal string the LegacyDec fields carry. Built by integer arithmetic on purpose:
  # `awk '{printf "%.18f", 8500/10000}'` prints 0.849999999999999978, a different parameter.
  _dec() { printf '%d.%04d%s' "$(( $1 / 10000 ))" "$(( $1 % 10000 ))" '00000000000000'; }
  tmp=$(mktemp)
  # Fatal, like the jobs block and unlike the VRF one: a slashing patch that fails quietly would leave the
  # SDK defaults in place, which is what this block exists to replace.
  jq --arg sbw "$SBW" --arg msw "$(_dec "$MSBPS")" --arg djd "${DJS}s" \
     --arg sfds "$(_dec "$DSBPS")" --arg sfdt "$(_dec "$DTBPS")" '
      .app_state.slashing.params.signed_blocks_window       = $sbw
    | .app_state.slashing.params.min_signed_per_window      = $msw
    | .app_state.slashing.params.downtime_jail_duration     = $djd
    | .app_state.slashing.params.slash_fraction_double_sign = $sfds
    | .app_state.slashing.params.slash_fraction_downtime    = $sfdt
  ' "$GEN" > "$tmp" && mv "$tmp" "$GEN" || { echo "[chain] FATAL: slashing params jq patch failed"; exit 1; }
  echo "[chain] slashing: window=$SBW blocks, min_signed=$(_dec "$MSBPS") -> a validator is jailed once it has"
  echo "        missed more than ${_TOL} blocks inside the window; downtime slash=$(_dec "$DTBPS"), jail=${DJS}s,"
  echo "        double-sign slash=$(_dec "$DSBPS"). Blocks are the parameter; the duration ${_TOL} blocks represent"
  echo "        depends on the block interval, which consensus does not fix - measure it, do not assume it."

  # Stderr is kept on failure. `gentx` re-validates the whole genesis before signing, so it is the first
  # command that rejects a bad parameter set, earlier than the explicit validate-genesis below.
  # Discarding its stderr turned every such rejection into a bare exit 1 after a normal-looking boot log
  # (seen with DENDRA_COMMITTEE_SEED_SOURCE=0, which Params.Validate refuses).
  GTX_ERR=$(mktemp)
  # ADR-046: the origin self-bond is 600 000 DNDR, a parameter and no longer a literal, bounded by the
  # validator pocket it is taken from.
  VBOND="${DENDRA_VALIDATOR_BOND:-600000000000}"
  case "$VBOND" in ''|*[!0-9]*) echo "[chain] FATAL: DENDRA_VALIDATOR_BOND=$VBOND is not a udndr amount"; exit 1;; esac
  { [ "$VBOND" -gt 0 ] && [ "$VBOND" -le "$VAL_AMT" ]; } || { echo "[chain] FATAL: DENDRA_VALIDATOR_BOND=$VBOND must be in ]0, $VAL_AMT] (the validator pocket)"; exit 1; }
  if ! $BIN genesis gentx validator ${VBOND}$DENOM --chain-id "$CHAIN_ID" $KB >/dev/null 2>"$GTX_ERR"; then
    echo "[chain] FATAL: gentx refused this genesis. It re-validates every module parameter before" >&2
    echo "        signing, so the reason below is the reason the chain would not have accepted:" >&2
    cat "$GTX_ERR" >&2; rm -f "$GTX_ERR"; exit 1
  fi
  if ! $BIN genesis collect-gentxs --home "$HOME_DIR" >/dev/null 2>"$GTX_ERR"; then
    echo "[chain] FATAL: collect-gentxs failed:" >&2; cat "$GTX_ERR" >&2; rm -f "$GTX_ERR"; exit 1
  fi
  rm -f "$GTX_ERR"
  if $BIN genesis validate-genesis --home "$HOME_DIR" >/dev/null 2>&1; then
    echo "[chain] genesis valid."
  else
    # Fail here with a clear message rather than crash at boot. This catches, among other
    # things, a genesis whose params violate the positive-EV invariant, via GenesisState.Validate().
    echo "[chain] FATAL: validate-genesis failed (invalid genesis; e.g. the positive-EV params invariant):" >&2
    $BIN genesis validate-genesis --home "$HOME_DIR" >&2 || true
    exit 1
  fi
  # The last step of the build, and only here: everything above either completed or exited. The next start
  # treats the genesis as existing only with this marker beside it (the check opens the `else` below).
  printf 'complete %s\n' "$CHAIN_ID" > "$GEN_DONE"
  echo "[chain] ===== testnet genesis ready (frozen in the volume) ====="
else
  # A genesis file is not a built genesis. `dendrad init` writes genesis.json at the start of the build,
  # and every fatal error after it (a cold address refused by add-genesis-account, a supply that does not
  # add up, a parameter refused by gentx or validate-genesis) left that file behind, so the next start came
  # here and took it for a frozen genesis without its accounts or its validator.
  # The build writes $GEN_DONE as its last step, after validate-genesis; a genesis without it is refused,
  # with the remedy, instead of being started.
  # A home built before the marker existed has none either, and is recognised by what it did: a half-built
  # genesis has no validator, so it never signed a block, while a chain that ran has a validator state above
  # height 0. An unreadable state reads as 0, which means refusal.
  if [ ! -f "$GEN_DONE" ]; then
    _pvh="$(jq -r '.height // "0"' "$HOME_DIR/data/priv_validator_state.json" 2>/dev/null || true)"
    case "$_pvh" in ''|*[!0-9]*) _pvh=0 ;; esac
    if [ "$_pvh" -gt 0 ]; then
      echo "[chain] this genesis predates the completion marker; its validator has signed up to height $_pvh, so it was built -> marker written."
      printf 'adopted %s height=%s\n' "$CHAIN_ID" "$_pvh" > "$GEN_DONE"
    else
      echo "[chain] FATAL: $GEN exists but the genesis build never completed ($GEN_DONE is absent, and no block was"
      echo "        ever signed on it). Starting it would run a genesis missing accounts or its validator."
      echo "        Read why the first boot stopped (docker compose logs chain), fix that cause, then remove the"
      echo "        half-built file -- nothing ever ran on it; the keyring is kept and reused:"
      echo "          docker compose run --rm --no-deps --entrypoint rm chain -f $GEN"
      echo "        or relaunch with DENDRA_FRESH=1. If this genesis was placed here on purpose (a rescue, a"
      echo "        restored home) and validates:  touch $GEN_DONE  inside the volume."
      exit 1
    fi
  fi
  echo "[chain] existing genesis -> persistent state reused."
  # A home built before the service keyring existed keeps `bob` and `gw` in the validator's keyring, and
  # the faucet and the gateway now mount only `service-keys`: they would start, find no key, and fail at
  # their first signature. Said here, at boot, rather than at the first drip a user asks for.
  for _sk in bob gw; do
    if ! $BIN keys show "$_sk" $SKB >/dev/null 2>&1; then
      echo "[chain] ERR: key '$_sk' is missing from the service keyring ($SVC_HOME). This home predates it;"
      echo "        the faucet and the gateway cannot sign until the key is there (a fresh genesis creates it)."
    fi
  done
fi

# VRF bootstrap: the validator's VRF key is managed automatically:
#  - keygen when absent -> written to the home directory (dendrad loads it through DENDRA_VRF_KEY_FILE
#    and signs its vote extensions from startup, with no restart);
#  - the pubkey is anchored on chain in the background (idempotent, best effort) so the validator counts
#    in the VRF aggregation (it contributes to `committee_min_vrf_contributors`). A failure leaves
#    anti-grinding red: visible, never blocking.
VRFKEY="$HOME_DIR/config/vrf_key"
if command -v dendra-vrf >/dev/null 2>&1; then
  [ -s "$VRFKEY" ] || { dendra-vrf keygen | cut -f1 > "$VRFKEY" && chmod 600 "$VRFKEY" && echo "[chain] validator VRF key generated ($VRFKEY)"; }
  (
    # This subshell would inherit `set -e`, which kills it without a word:
    # the loop queries the transaction five seconds after broadcasting it, often before the block that
    # includes it is committed, `dendrad query tx` then exits non-zero, and `set -e` ends the subshell with
    # no message. This task handles its own failures and must report them, so it runs without `set -e`.
    set +e
    for _ in $(seq 1 90); do curl -s http://127.0.0.1:26657/status 2>/dev/null | grep -q latest_block_height && break; sleep 2; done
    # The secret reaches the CLI through the environment, never through argv. `ps` and /proc/<pid>/cmdline
    # are world-readable, so a key on the command line is readable by any local process while the command
    # runs; /proc/<pid>/environ is owner-only. `dendra-vrf` reads DENDRA_VRF_SK, as its usage says.
    # This key is the validator's, the one `loadNodeVrfKey()` reads for vote extensions at every height.
    # Stolen alone it forges nothing (an accepted contribution also needs the consensus key), but it makes
    # this validator's contribution predictable, and unpredictability is what the key is for.
    # VSK stays a shell variable and is never exported: only the two children below receive it.
    VSK=$(cat "$VRFKEY" 2>/dev/null)
    VPK=$(DENDRA_VRF_SK="$VSK" dendra-vrf pubkey 2>/dev/null)
    A=$($BIN keys show validator -a $KB 2>/dev/null)
    POP=$(DENDRA_VRF_SK="$VSK" dendra-vrf prove "dendra/vrf-pop/$A" 2>/dev/null)
    # A one-shot anchor fails silently on the first-boot race, and the node then does not contribute to
    # the seed (anti-grinding red) until someone anchors by hand. Hence a bounded retry.
    # `"code":0` in a broadcast response only means the tx entered the mempool, not that it executed: it can
    # still fail at execution (account number unreadable at signing time, funds, state changed meanwhile),
    # and the validator would then silently not contribute to the seed. Only `query tx <hash>` returns the
    # execution result. (deploy/testnet/anchor_vrf_key.sh does the same.)
    ok=0
    for _ in $(seq 1 10); do
      OUT=$($BIN tx jobs register-validator-vrf-key "$VPK" "$POP" --from validator $KB --chain-id "$CHAIN_ID" --node tcp://127.0.0.1:26657 --yes -o json 2>/dev/null)
      HASH=$(printf '%s' "$OUT" | tr -d ' \t' | grep -o '"txhash":"[A-Fa-f0-9]*"' | head -1 | cut -d'"' -f4)
      if [ -n "$HASH" ]; then
        # let the block include the tx, then read the execution result
        for _ in $(seq 1 12); do
          sleep 5
          QOUT=$($BIN query tx "$HASH" --node tcp://127.0.0.1:26657 -o json 2>/dev/null)
# A chain answer is parsed, never grepped. proto3 omits a zero, so on a successful transaction `code` is
# absent, and a textual predicate would read that as "never included", re-broadcast an anchoring that
# already worked and report it as not confirmed. `txhash` proves an answer arrived; once it is there, an
# absent `code` is a zero. `jq` is not a new dependency: this image installs it (`docker/Dockerfile.chain`).
tx_code() { # reads a `query tx` answer on stdin; prints its code, or nothing
  jq -r 'if type=="object" and ((.txhash // "") != "") then (.code // 0) else empty end' 2>/dev/null
}

          QCODE=$(printf '%s' "$QOUT" | tx_code)
          [ -n "$QCODE" ] && break
        done
        [ "$QCODE" = "0" ] && { ok=1; break; }
        [ -n "$QCODE" ] && echo "[chain] ERR: VRF anchoring rejected at execution (code=$QCODE) -> this validator does not contribute to the seed"
      fi
      sleep 30
    done
    if [ "$ok" = 1 ]; then echo "[chain] validator VRF pubkey anchored on-chain (automatic, code:0 verified)"
    else echo "[chain] ERR: automatic VRF anchoring not confirmed after 10 attempts -> anchor manually with deploy/testnet/anchor_vrf_key.sh (anti-grinding stays red otherwise)"; fi
  ) &
else
  echo "[chain] (info) dendra-vrf missing from the image -> no automatic VRF anchoring (rebuild the chain image to enable it)"
fi

# CORS: let browser wallets and dApps reach the public RPC and REST (public testnet). Idempotent per boot.
CFGTOML="$HOME_DIR/config/config.toml"
[ -f "$CFGTOML" ] && sed -i 's|^cors_allowed_origins = .*|cors_allowed_origins = ["*"]|' "$CFGTOML"

# Block cadence, written explicitly (ADR-046). The SDK silently replaces CometBFT's default `timeout_commit`
# with 5 s when the file still holds the default, so the cadence every "about a day" parameter assumes
# rested on an SDK behaviour nobody chose. It is written at every boot and read back, because a sed that
# matched nothing would leave the file unchanged silently.
if [ -f "$CFGTOML" ]; then
  # cadence: live
  sed -i 's|^timeout_commit = .*|timeout_commit = "5s"|' "$CFGTOML"
  grep -q '^timeout_commit = "5s"' "$CFGTOML" || { echo "[chain] FATAL: timeout_commit could not be written to $CFGTOML"; exit 1; }
fi

# P2P: accept several nodes from one address. CometBFT defaults `allow_duplicate_ip` to false, which
# refuses a second inbound connection from an address that already holds one. The topology this project
# documents produces exactly that: an operator running a validator and a miner, each with its own node,
# reaches the seed from a single public IP, as does anyone with a second machine behind one connection.
# The refusal happens during the encrypted handshake, so the joiner reads `auth failure: secret conn
# failed: connection reset by peer`, which points at the node ID, the genesis or the network path, none
# of which is at fault, and nothing on the joining side can work around it.
# The seed is public and permissionless, so several nodes behind one address are expected traffic. The
# duplicate-IP rule protects a validator set against a single host claiming many peer slots; on a
# bootstrap seed it only turns away real operators.
[ -f "$CFGTOML" ] && sed -i 's|^allow_duplicate_ip = .*|allow_duplicate_ip = true|' "$CFGTOML"

# Snapshots, required once a consensus-breaking change has occurred.
# Without a snapshot, a new node replays the history from genesis. The current binary writes
# `audit_committee/*` (ADR-032) where an older one did not: replaying a block from before the upgrade at
# which an audit was drawn recomputes an AppHash that differs from the one already signed in the header,
# and the replay panics. No height guard fixes this, because the history really contains both behaviours.
# So the joiner does not replay: snapshots are served, and it state-syncs at a height after the upgrade.
# Cost: a few MB every 500 blocks.
APPTOML="$HOME_DIR/config/app.toml"
if [ -f "$APPTOML" ]; then
  sed -i 's|^snapshot-interval = .*|snapshot-interval = 500|' "$APPTOML"
  sed -i 's|^snapshot-keep-recent = .*|snapshot-keep-recent = 5|' "$APPTOML"
  grep -q '^snapshot-interval' "$APPTOML" || printf '\n[state-sync]\nsnapshot-interval = 500\nsnapshot-keep-recent = 5\n' >> "$APPTOML"
  echo "[chain] snapshots enabled (every 500 blocks, 5 kept) -> joiners can state-sync"
  echo "        instead of replaying a history that contains a consensus-breaking change."
fi

echo "[chain] starting dendrad (RPC 0.0.0.0:26657, REST 1317 with CORS)"
exec $BIN start --home "$HOME_DIR" \
  --rpc.laddr tcp://0.0.0.0:26657 \
  --api.enable --api.address tcp://0.0.0.0:1317 --api.enabled-unsafe-cors \
  --grpc.address 0.0.0.0:9090 \
  --minimum-gas-prices 0$DENOM
