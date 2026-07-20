package keeper_test

import (
	"strings"
	"testing"

	"cosmossdk.io/math"
	sdk "github.com/cosmos/cosmos-sdk/types"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
	"github.com/stretchr/testify/require"

	"dendra/x/jobs/keeper"
	"dendra/x/jobs/types"
)

// F1 (audit A→Z 2026-07-10) — DOUBLE-SLASH FinalizeJob→SettleSemantic fermé par la garde CIBLÉE.
//
// Vecteur : en mode-0, FinalizeJob rend le verdict et slashe les divergents, marque `+finalized`
// SANS poser paid/settled ; SettleSemantic ne testait que jobIsPaid → il PROCÉDAIT et re-slashait
// les MÊMES commits ancrés (immuables, donc toujours divergents). Les deux handlers sont
// permissionless → double peine composée sur un mineur potentiellement honnête (non-déterminisme GPU).
//
// Arbitrage internal audit 07-10 : garde `finalized` dans SettleSemantic SEUL (PAS jobIsTerminal partout —
// le flux documenté FinalizeJob→Payout doit rester ouvert, cf. TestF1FinalizeThenPayoutStillWorks).
func TestF1FinalizeThenSettleSemanticRejectedNoDoubleSlash(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)

	addr20 := func(s string) string {
		b := make([]byte, 20)
		copy(b, s)
		out, err := f.addressCodec.BytesToString(b)
		require.NoError(t, err)
		return out
	}

	// 3 mineurs (comité complet, size=3) ; commits en VECTEURS parsables (format SettleSemantic) :
	// ma/mb = canonical identique, mc = vecteur opposé (divergent pour FinalizeJob — string ≠ —
	// ET outlier cosinus pour SettleSemantic) → sans la garde, les DEUX handlers le slashent.
	commits := map[string]string{"ma": "1,2,3", "mb": "1,2,3", "mc": "-1,-2,-3"}
	for _, id := range []string{"ma", "mb", "mc"} {
		require.NoError(t, f.keeper.Miner.Set(f.ctx, id, types.Miner{
			MinerId: id, Creator: addr20("creator-" + id), Operator: addr20("operator-" + id), Stake: 1000,
		}))
		require.NoError(t, f.keeper.Commit.Set(f.ctx, "j1__"+id, types.Commit{ResultCommit: commits[id]}))
	}
	client := addr20("client-xyz")
	require.NoError(t, f.keeper.Job.Set(f.ctx, "j1", types.Job{JobId: "j1", Fee: 10000, Client: client}))
	trigger := addr20("trigger-addr")

	// (1) FinalizeJob : verdict + slash 1× du divergent (défaut slash_leak_bps=8000 : 1000 → 200).
	_, err := srv.FinalizeJob(f.ctx, &types.MsgFinalizeJob{Creator: trigger, JobId: "j1"})
	require.NoError(t, err)
	mc, err := f.keeper.Miner.Get(f.ctx, "mc")
	require.NoError(t, err)
	require.Equal(t, uint64(200), mc.Stake, "slash FinalizeJob attendu : 1000 -> 200 (80%%)")
	job, err := f.keeper.Job.Get(f.ctx, "j1")
	require.NoError(t, err)
	require.True(t, strings.Contains(job.State, "finalized"))

	// (2) SettleSemantic sur le job finalisé : REFUSÉ (la garde F1) — avant le fix, il procédait
	// et re-slashait mc (200 → 40).
	_, err = srv.SettleSemantic(f.ctx, &types.MsgSettleSemantic{Creator: trigger, JobId: "j1"})
	require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest)

	// (3) Le stake du divergent est ponctionné UNE fois : toujours 200, pas 40.
	mc, err = f.keeper.Miner.Get(f.ctx, "mc")
	require.NoError(t, err)
	require.Equal(t, uint64(200), mc.Stake, "double-slash détecté : le stake a été re-ponctionné")
}

// F1 non-régression — le flux DOCUMENTÉ mode-0 `FinalizeJob (verdict) → Payout (paiement)` reste
// OUVERT (job_state.go:13-14 : l'exclusion de `finalized` de jobIsPaid est VOLONTAIRE ; Payout ne
// slashe pas). La garde large `jobIsTerminal` sur Payout aurait gelé l'escrow de tout job finalisé :
// gagnants jamais payés, client jamais remboursé — c'est exactement ce que ce test interdit.
func TestF1FinalizeThenPayoutStillWorks(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)

	addr20 := func(s string) string {
		b := make([]byte, 20)
		copy(b, s)
		out, err := f.addressCodec.BytesToString(b)
		require.NoError(t, err)
		return out
	}

	const fee = 10000
	f.bank.mod[types.ModuleName] = sdk.NewCoins(sdk.NewCoin("udndr", math.NewInt(fee)))

	commits := map[string]string{"ma": "1,2,3", "mb": "1,2,3", "mc": "-1,-2,-3"}
	for _, id := range []string{"ma", "mb", "mc"} {
		require.NoError(t, f.keeper.Miner.Set(f.ctx, id, types.Miner{
			MinerId: id, Creator: addr20("creator-" + id), Operator: addr20("operator-" + id), Stake: 1000,
		}))
		require.NoError(t, f.keeper.Commit.Set(f.ctx, "j2__"+id, types.Commit{ResultCommit: commits[id]}))
	}
	client := addr20("client-xyz")
	require.NoError(t, f.keeper.Job.Set(f.ctx, "j2", types.Job{JobId: "j2", Fee: fee, Client: client}))
	trigger := addr20("trigger-addr")

	// Verdict d'abord (slashe mc), puis paiement des gagnants du canonical : les DEUX passent.
	_, err := srv.FinalizeJob(f.ctx, &types.MsgFinalizeJob{Creator: trigger, JobId: "j2"})
	require.NoError(t, err)
	_, err = srv.Payout(f.ctx, &types.MsgPayout{Creator: trigger, JobId: "j2"})
	require.NoError(t, err, "non-régression F1 : FinalizeJob -> Payout est le flux documenté, il doit PASSER")

	// Paiement effectif : burn 5%% (500) + 2 gagnants × 4750 = escrow soldé ; mc (divergent) non payé.
	require.Equal(t, "500udndr", f.bank.burned.String())
	require.True(t, f.bank.mod[types.ModuleName].Empty(), "escrow non soldé : %s", f.bank.mod[types.ModuleName])
	job, err := f.keeper.Job.Get(f.ctx, "j2")
	require.NoError(t, err)
	require.True(t, strings.Contains(job.State, "paid"))
}
