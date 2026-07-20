package keeper_test

import (
	"encoding/hex"
	"testing"

	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/stretchr/testify/require"

	"dendra/x/jobs/keeper"
	"dendra/x/jobs/types"
)

// E4 brique 4b — la sélection de comité CONSOMME la graine VRF décentralisée quand committee_seed_source==1.
// On pose une graine décentralisée à la hauteur courante (comme le PreBlocker), puis on ouvre des jobs et
// on vérifie la graine du beacon : legacy (source=0) != décentralisée ; source=1 == décentralisée ;
// source=1 sans graine à cette hauteur -> repli legacy (non vide).
func TestCommitteeSeedSourceDecentralized(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	creator, err := f.addressCodec.BytesToString([]byte("signerAddr__________________"))
	require.NoError(t, err)

	ctx7 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(7)
	dseed := []byte{0xde, 0xad, 0xbe, 0xef, 0x01, 0x02, 0x03, 0x04}
	dhex := hex.EncodeToString(dseed)
	require.NoError(t, f.keeper.SetDecentralizedSeed(ctx7, 7, dseed))

	// source=0 (legacy) -> graine du beacon = height:time, PAS la décentralisée
	p := types.DefaultParams()
	p.CommitteeSeedSource = 0
	require.NoError(t, f.keeper.Params.Set(ctx7, p))
	_, err = srv.OpenJob(ctx7, &types.MsgOpenJob{Creator: creator, JobId: "jLegacy", Fee: 10})
	require.NoError(t, err)
	b, err := f.keeper.Beacon.Get(ctx7, "jLegacy")
	require.NoError(t, err)
	require.NotEqual(t, dhex, b.Seed, "source=0 -> graine legacy (pas la décentralisée)")

	// source=1 -> graine du beacon = graine VRF décentralisée (hex) à cette hauteur
	p.CommitteeSeedSource = 1
	require.NoError(t, f.keeper.Params.Set(ctx7, p))
	_, err = srv.OpenJob(ctx7, &types.MsgOpenJob{Creator: creator, JobId: "jVrf", Fee: 10})
	require.NoError(t, err)
	b, err = f.keeper.Beacon.Get(ctx7, "jVrf")
	require.NoError(t, err)
	require.Equal(t, dhex, b.Seed, "source=1 -> graine = VRF décentralisée à la hauteur courante")

	// source=1 mais AUCUNE graine à cette hauteur -> repli legacy (non vide, != décentralisée)
	ctx9 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(9)
	require.NoError(t, f.keeper.Params.Set(ctx9, p))
	_, err = srv.OpenJob(ctx9, &types.MsgOpenJob{Creator: creator, JobId: "jNone", Fee: 10})
	require.NoError(t, err)
	b, err = f.keeper.Beacon.Get(ctx9, "jNone")
	require.NoError(t, err)
	require.NotEmpty(t, b.Seed, "source=1 sans graine à H -> repli legacy non vide")
	require.NotEqual(t, dhex, b.Seed)
}

// Bootstrap VRF (internal audit 2026-06-26) — plancher committee_min_vrf_contributors : une graine décentralisée
// PRÉSENTE mais SOUS-DÉCENTRALISÉE (contributeurs < plancher) NE tire PAS un comité (repli legacy visible) ;
// au-dessus du plancher, la graine est utilisée. Anti-régression silencieuse + jamais de halte.
func TestCommitteeMinVrfContributors(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	creator, err := f.addressCodec.BytesToString([]byte("signerAddr__________________"))
	require.NoError(t, err)

	ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(11)
	dseed := []byte{0xca, 0xfe, 0xba, 0xbe, 0x09, 0x08, 0x07, 0x06}
	dhex := hex.EncodeToString(dseed)
	require.NoError(t, f.keeper.SetDecentralizedSeed(ctx, 11, dseed))

	p := types.DefaultParams()
	p.CommitteeSeedSource = 1
	p.CommitteeMinVrfContributors = 2 // plancher = 2 contributeurs
	require.NoError(t, f.keeper.Params.Set(ctx, p))

	// graine présente mais 1 SEUL contributeur (< plancher 2) -> repli legacy (PAS la décentralisée)
	require.NoError(t, f.keeper.SetDecentralizedSeedContributors(ctx, 11, 1))
	_, err = srv.OpenJob(ctx, &types.MsgOpenJob{Creator: creator, JobId: "jUnder", Fee: 10})
	require.NoError(t, err)
	b, err := f.keeper.Beacon.Get(ctx, "jUnder")
	require.NoError(t, err)
	require.NotEqual(t, dhex, b.Seed, "1 contributeur < plancher 2 -> repli legacy (graine sous-decentralisee NON utilisee)")
	require.NotEmpty(t, b.Seed, "repli legacy non vide (pas de halte)")

	// 2 contributeurs (>= plancher) + puissance >= 2/3 (barre POUVOIR du lot scaling 2026-07-02,
	// posée par le PreBlocker au même site que la graine) -> la graine VRF décentralisée EST utilisée
	require.NoError(t, f.keeper.SetDecentralizedSeedContributors(ctx, 11, 2))
	require.NoError(t, f.keeper.SetDecentralizedSeedContributorPower(ctx, 11, 6667))
	_, err = srv.OpenJob(ctx, &types.MsgOpenJob{Creator: creator, JobId: "jOver", Fee: 10})
	require.NoError(t, err)
	b, err = f.keeper.Beacon.Get(ctx, "jOver")
	require.NoError(t, err)
	require.Equal(t, dhex, b.Seed, "2 contributeurs >= plancher (+ pouvoir >= 2/3) -> graine VRF decentralisee utilisee")
}

// LOT SCALING (2026-07-01, durci post-red-team 2026-07-02) — plancher VRF DYNAMIQUE ⌈2N/3⌉ EN POUVOIR :
// quand le plancher statique est ARMÉ (>0), la graine n'est utilisée que si les contributeurs VRF valides
// pèsent ≥ 2/3 de la PUISSANCE du commit (MinVrfContributorPowerBps=6667) — la barre BFT, insensible au
// sybil-poussière (un plancher en cardinal serait griefable : gonfler N à stake nul -> legacy permanent).
// Puissance absente (graine d'un code antérieur) = repli VISIBLE. Dormant (param=0) : AUCUNE barre implicite.
func TestCommitteeVrfFloorDynamicPower(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	creator, err := f.addressCodec.BytesToString([]byte("signerAddr__________________"))
	require.NoError(t, err)

	ctx := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(13)
	dseed := []byte{0x0d, 0x15, 0xea, 0x5e, 0x01, 0x02, 0x03, 0x04}
	dhex := hex.EncodeToString(dseed)
	require.NoError(t, f.keeper.SetDecentralizedSeed(ctx, 13, dseed))

	p := types.DefaultParams()
	p.CommitteeSeedSource = 1
	p.CommitteeMinVrfContributors = 2 // plancher statique armé = 2
	require.NoError(t, f.keeper.Params.Set(ctx, p))
	require.NoError(t, f.keeper.SetDecentralizedSeedContributors(ctx, 13, 2))

	// (a) count OK mais PUISSANCE ABSENTE (graine posée sans la part de pouvoir) -> repli legacy visible
	_, err = srv.OpenJob(ctx, &types.MsgOpenJob{Creator: creator, JobId: "jNoPow", Fee: 10})
	require.NoError(t, err)
	b, err := f.keeper.Beacon.Get(ctx, "jNoPow")
	require.NoError(t, err)
	require.NotEqual(t, dhex, b.Seed, "puissance absente -> repli legacy (fail-visible)")
	require.NotEmpty(t, b.Seed, "repli legacy non vide (pas de halte)")

	// (b) contributeurs SOUS-PONDÉRÉS (2 validateurs-poussière sur un commit domine par d'autres : 3000 bps < 6667)
	// -> repli legacy : le sybil-poussiere ne peut PAS faire accepter une graine minoritaire en pouvoir
	require.NoError(t, f.keeper.SetDecentralizedSeedContributorPower(ctx, 13, 3000))
	_, err = srv.OpenJob(ctx, &types.MsgOpenJob{Creator: creator, JobId: "jLowPow", Fee: 10})
	require.NoError(t, err)
	b, err = f.keeper.Beacon.Get(ctx, "jLowPow")
	require.NoError(t, err)
	require.NotEqual(t, dhex, b.Seed, "puissance 30%% < 2/3 -> repli legacy")

	// (c) contributeurs >= 2/3 du pouvoir (6667 bps) -> graine UTILISÉE
	require.NoError(t, f.keeper.SetDecentralizedSeedContributorPower(ctx, 13, 6667))
	_, err = srv.OpenJob(ctx, &types.MsgOpenJob{Creator: creator, JobId: "jOkPow", Fee: 10})
	require.NoError(t, err)
	b, err = f.keeper.Beacon.Get(ctx, "jOkPow")
	require.NoError(t, err)
	require.Equal(t, dhex, b.Seed, "puissance 6667 bps >= 2/3 -> graine utilisee")

	// (d) barre statique COUNT toujours active : 1 contributeur < param 2 -> repli meme a pleine puissance
	require.NoError(t, f.keeper.SetDecentralizedSeedContributors(ctx, 13, 1))
	require.NoError(t, f.keeper.SetDecentralizedSeedContributorPower(ctx, 13, 10000))
	_, err = srv.OpenJob(ctx, &types.MsgOpenJob{Creator: creator, JobId: "jLowCount", Fee: 10})
	require.NoError(t, err)
	b, err = f.keeper.Beacon.Get(ctx, "jLowCount")
	require.NoError(t, err)
	require.NotEqual(t, dhex, b.Seed, "count 1 < param 2 -> repli (barre statique v1 inchangee)")

	// (e) DORMANT : param=0 -> aucune barre implicite (ni count ni pouvoir), graine utilisee (v1 strict)
	p.CommitteeMinVrfContributors = 0
	require.NoError(t, f.keeper.Params.Set(ctx, p))
	require.NoError(t, f.keeper.SetDecentralizedSeedContributors(ctx, 13, 1))
	require.NoError(t, f.keeper.DeleteDecentralizedSeedContributorPower(ctx, 13))
	_, err = srv.OpenJob(ctx, &types.MsgOpenJob{Creator: creator, JobId: "jDormant", Fee: 10})
	require.NoError(t, err)
	b, err = f.keeper.Beacon.Get(ctx, "jDormant")
	require.NoError(t, err)
	require.Equal(t, dhex, b.Seed, "param=0 (dormant) -> pas de barre implicite, graine utilisee (v1 inchange)")
}

// Validate borne committee_seed_source à {0,1}.
func TestCommitteeSeedSourceValidate(t *testing.T) {
	p := types.DefaultParams()
	p.CommitteeSeedSource = 1
	require.NoError(t, p.Validate())
	p.CommitteeSeedSource = 2
	require.Error(t, p.Validate(), "valeur > 1 rejetée")
}
