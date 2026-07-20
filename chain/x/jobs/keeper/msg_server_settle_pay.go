package keeper

import (
	"context"
	"sort"
	"strings"

	"dendra/x/jobs/types"

	errorsmod "cosmossdk.io/errors"
	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
)

// SettlePay -- PAIEMENT REEL (L2) borne au COMITE ASSIGNE (L3). Le CLIENT (signataire) recompense
// en vrais "udndr" les mineurs de la MAJORITE honnete PARMI LES MINEURS ASSIGNES, DEPUIS SON wallet.
//
// CORRIGE (audit 2026-06-10) GO-01/GO-10 : anti-rejeu -- charge le Job, refuse s'il est deja reglé,
// et ecrit le flag "+settled" (erreur verifiee) en fin. Sans ca, un double-appel re-paie. (msg.Amount
// reste : c'est le wallet DU CLIENT, son choix de montant -- pas l'escrow du module, donc pas GO-02.)
func (k msgServer) SettlePay(ctx context.Context, msg *types.MsgSettlePay) (*types.MsgSettlePayResponse, error) {
	fromBz, err := k.addressCodec.StringToBytes(msg.Creator)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidAddress, "invalid creator address")
	}
	// GO-01 : anti-rejeu sur l'etat du job.
	job, jobErr := k.Job.Get(ctx, msg.JobId)
	if jobErr != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrKeyNotFound, "job non ouvert")
	}
	if jobIsPaid(job.State) { // GO-08 : anti-rejeu de règlement UNIFIÉ (paid || settled)
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "job deja regle (anti-rejeu)")
	}
	committee, err := k.assignedCommittee(ctx, msg.JobId, CommitteeSize) // L3
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}

	prefix := msg.JobId + "__"
	commitByMiner := map[string]string{}
	tally := map[string]int{}
	if err := k.Commit.Walk(ctx, commitRange(prefix), func(key string, c types.Commit) (bool, error) {
		if strings.HasPrefix(key, prefix) {
			mid := strings.TrimPrefix(key, prefix)
			if committee[mid] {
				commitByMiner[mid] = c.ResultCommit
				tally[c.ResultCommit]++
			}
		}
		return false, nil
	}); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	if len(commitByMiner) == 0 {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "aucun commit de mineur assigne pour ce job")
	}
	// GO-06 PROPAGÉ — cf. msg_server_payout.go : sans comité COMPLET, le premier
	// commiteur-relayeur rafle l'escrow entier avant que ses pairs aient soumis.
	if len(commitByMiner) < CommitteeSize {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "comite incomplet -> reglement refuse")
	}

	commits := make([]string, 0, len(tally))
	for c := range tally {
		commits = append(commits, c)
	}
	sort.Strings(commits)
	canonical, best := "", -1
	for _, c := range commits {
		if tally[c] > best {
			best, canonical = tally[c], c
		}
	}

	mids := make([]string, 0, len(commitByMiner))
	for mid := range commitByMiner {
		mids = append(mids, mid)
	}
	sort.Strings(mids)
	// NEW-GO-38 (audit v2) : le client paie un TOTAL `msg.Amount`, RÉPARTI entre les gagnants
	// (per = Amount / nGagnants) — et NON `Amount` à CHACUN (ce qui faisait débiter Amount × N du client).
	winners := make([]string, 0, len(mids))
	for _, mid := range mids {
		if commitByMiner[mid] == canonical {
			winners = append(winners, mid)
		}
	}
	if len(winners) == 0 {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "pas de majorite -> rien a payer")
	}
	per := msg.Amount / uint64(len(winners))
	remainder := msg.Amount - per*uint64(len(winners)) // NEW-GO-42 (audit v3) : reliquat (≤ N-1 udndr)
	for i, mid := range winners {
		miner, mErr := k.Miner.Get(ctx, mid)
		if mErr != nil {
			continue
		}
		toBz, aErr := k.addressCodec.StringToBytes(miner.Operator)
		if aErr != nil {
			continue
		}
		amt := per
		if i == 0 {
			amt += remainder // au 1er gagnant -> le client paie EXACTEMENT Amount (plus de reliquat perdu)
		}
		coins := sdk.NewCoins(sdk.NewCoin("udndr", math.NewIntFromUint64(amt)))
		if err := k.bankKeeper.SendCoins(ctx, sdk.AccAddress(fromBz), sdk.AccAddress(toBz), coins); err != nil {
			return nil, errorsmod.Wrap(sdkerrors.ErrInsufficientFunds, err.Error())
		}
	}
	// GO-01/GO-10 : marque reglé (erreur verifiee).
	job.State = job.State + "+settled"
	if err := k.Job.Set(ctx, job.JobId, job); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, "echec marquage +settled : "+err.Error())
	}
	return &types.MsgSettlePayResponse{}, nil
}
