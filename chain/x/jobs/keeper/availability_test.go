package keeper_test

import (
	"encoding/hex"
	"testing"

	"cosmossdk.io/collections"
	"cosmossdk.io/core/header"
	sdk "github.com/cosmos/cosmos-sdk/types"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
	"github.com/stretchr/testify/require"

	"dendra/x/jobs/keeper"
	"dendra/x/jobs/types"
	"dendra/x/jobs/vrf"
)

// Phase 1b — disponibilité. avail_epoch_blocks=0 (défaut) : tout est OFF, ProveAvailability refusé
// (e2e intacte tant que la gouvernance n'active pas la disponibilité).
func TestAvailabilityOff(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	op, err := f.addressCodec.BytesToString([]byte("operatorA___________________"))
	require.NoError(t, err)
	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: op, Operator: op, MinerId: "m1", Stake: 1000})
	require.NoError(t, err)
	_, err = srv.ProveAvailability(f.ctx, &types.MsgProveAvailability{Creator: op, MinerId: "m1", Challenge: "x"})
	require.Error(t, err, "disponibilite OFF -> refus")
}

// avail_epoch_blocks>0 : le défi est roulé depuis l'AppHash à la frontière d'époque, les preuves
// fraîches sont enregistrées, et l'AvailPool est versé PONDÉRÉ PAR LE BOND à la frontière suivante.
func TestAvailabilityChallengeAndPayout(t *testing.T) {
	f := initFixture(t)
	// activer : époque de 4 blocs, 50% de l'AvailPool versé par époque
	p := types.DefaultParams()
	p.AvailEpochBlocks = 4
	p.AvailPayoutBps = 5000
	p.AvailRequireDemand = false // ce test isole le partage du payout PAR BOND (demande gérée par TestAvailabilityRequireDemand)
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	f.emission.avail = 1_000_000 // AvailPool simulé, montant connu

	srv := keeper.NewMsgServerImpl(f.keeper)
	opA, err := f.addressCodec.BytesToString([]byte("operatorA___________________"))
	require.NoError(t, err)
	opB, err := f.addressCodec.BytesToString([]byte("operatorB___________________"))
	require.NoError(t, err)
	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: opA, Operator: opA, MinerId: "m1", Stake: 1000})
	require.NoError(t, err)
	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: opB, Operator: opB, MinerId: "m2", Stake: 3000})
	require.NoError(t, err)

	// frontière d'époque h=4 : roule le défi de l'époque 1 (époque 0 vide -> rien versé)
	ctx4 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(4).WithHeaderInfo(header.Info{Height: 4, AppHash: []byte("apphash-epoch1")})
	require.NoError(t, f.keeper.EndBlock(ctx4))
	chal, err := f.keeper.AvailChallenge.Get(ctx4)
	require.NoError(t, err)
	require.NotEmpty(t, chal, "defi roule a la frontiere d'epoque")

	// mauvais défi -> refus
	ctx5 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(5).WithHeaderInfo(header.Info{Height: 5, AppHash: []byte("apphash-epoch1")})
	_, err = srv.ProveAvailability(ctx5, &types.MsgProveAvailability{Creator: opA, MinerId: "m1", Challenge: "MAUVAIS"})
	require.Error(t, err, "defi incorrect -> refus (anti pre-calcul)")

	// bon défi -> les 2 mineurs prouvent leur présence (époque 1)
	_, err = srv.ProveAvailability(ctx5, &types.MsgProveAvailability{Creator: opA, MinerId: "m1", Challenge: chal})
	require.NoError(t, err)
	_, err = srv.ProveAvailability(ctx5, &types.MsgProveAvailability{Creator: opB, MinerId: "m2", Challenge: chal})
	require.NoError(t, err)
	has, err := f.keeper.Available.Has(ctx5, collections.Join(int64(1), "m1"))
	require.NoError(t, err)
	require.True(t, has, "presence enregistree pour l'epoque 1")

	// frontière d'époque h=8 : verse l'époque 1, pondéré par le bond (1000 vs 3000 ; budget = 50% de 1_000_000)
	ctx8 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(8).WithHeaderInfo(header.Info{Height: 8, AppHash: []byte("apphash-epoch2")})
	require.NoError(t, f.keeper.EndBlock(ctx8))

	// budget=500_000 ; total bond=4000 ; m1 = 500000*1000/4000 = 125000 ; m2 = 375000
	require.Equal(t, uint64(125_000), f.emission.availPaid[opA], "part m1 (bond 1000/4000)")
	require.Equal(t, uint64(375_000), f.emission.availPaid[opB], "part m2 (bond 3000/4000)")
	require.Equal(t, uint64(500_000), f.emission.avail, "AvailPool debite du budget verse")

	// présences de l'époque 1 purgées après versement
	has, err = f.keeper.Available.Has(ctx8, collections.Join(int64(1), "m1"))
	require.NoError(t, err)
	require.False(t, has, "epoque 1 purgee apres versement")
}


// CR-10 (ECVRF) — si le mineur a ancré une vrf_pubkey, la disponibilité doit être prouvée par une
// PREUVE VRF sur le défi (pas un echo) : absente -> refus, fausse -> refus, valide -> acceptée. Les
// mineurs sans vrf_pubkey gardent l'echo (rétro-compat).
func TestAvailabilityVrfProof(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.AvailEpochBlocks = 4
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	srv := keeper.NewMsgServerImpl(f.keeper)
	opV, err := f.addressCodec.BytesToString([]byte("operatorVrf_________________"))
	require.NoError(t, err)
	opL, err := f.addressCodec.BytesToString([]byte("operatorLegacy______________"))
	require.NoError(t, err)

	// mineur VRF : génère une paire ECVRF, ancre la pub
	pk, sk, err := vrf.GenerateKey(nil)
	require.NoError(t, err)
	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: opV, Operator: opV, MinerId: "mv", Stake: 1000, VrfPubkey: hex.EncodeToString(pk)})
	require.NoError(t, err)
	// mineur legacy : pas de vrf_pubkey
	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: opL, Operator: opL, MinerId: "ml", Stake: 1000})
	require.NoError(t, err)

	// rouler le défi à la frontière d'époque
	ctx4 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(4).WithHeaderInfo(header.Info{Height: 4, AppHash: []byte("apphash-epoch1")})
	require.NoError(t, f.keeper.EndBlock(ctx4))
	chal, err := f.keeper.AvailChallenge.Get(ctx4)
	require.NoError(t, err)
	require.NotEmpty(t, chal)

	ctx5 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(5).WithHeaderInfo(header.Info{Height: 5, AppHash: []byte("apphash-epoch1")})

	// VRF miner SANS preuve -> refus
	_, err = srv.ProveAvailability(ctx5, &types.MsgProveAvailability{Creator: opV, MinerId: "mv", Challenge: chal})
	require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest)

	// VRF miner preuve VALIDE mais sur un AUTRE défi -> refus (non rejouable)
	wrongPi, err := vrf.Prove(sk, []byte("autre-defi"))
	require.NoError(t, err)
	_, err = srv.ProveAvailability(ctx5, &types.MsgProveAvailability{Creator: opV, MinerId: "mv", Challenge: chal, VrfProof: hex.EncodeToString(wrongPi)})
	require.ErrorIs(t, err, sdkerrors.ErrUnauthorized)

	// VRF miner preuve valide sur le BON défi -> accepté + présence enregistrée
	pi, err := vrf.Prove(sk, []byte(chal))
	require.NoError(t, err)
	_, err = srv.ProveAvailability(ctx5, &types.MsgProveAvailability{Creator: opV, MinerId: "mv", Challenge: chal, VrfProof: hex.EncodeToString(pi)})
	require.NoError(t, err)
	has, err := f.keeper.Available.Has(ctx5, collections.Join(int64(1), "mv"))
	require.NoError(t, err)
	require.True(t, has)

	// mineur legacy (sans vrf_pubkey) : echo simple -> toujours accepté
	_, err = srv.ProveAvailability(ctx5, &types.MsgProveAvailability{Creator: opL, MinerId: "ml", Challenge: chal})
	require.NoError(t, err)
}

// V6-02 — anti-farming : avec avail_require_demand=true (défaut), un mineur PRÉSENT mais SANS demande
// (farmeur sans GPU) ne touche RIEN ; seul celui qui a servi de la demande est payé.
func TestAvailabilityRequireDemand(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams() // AvailRequireDemand=true par défaut (V6-02)
	p.AvailEpochBlocks = 4
	p.AvailPayoutBps = 5000
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	f.emission.avail = 1_000_000

	srv := keeper.NewMsgServerImpl(f.keeper)
	opWork, err := f.addressCodec.BytesToString([]byte("operatorWork________________"))
	require.NoError(t, err)
	opIdle, err := f.addressCodec.BytesToString([]byte("operatorIdle________________"))
	require.NoError(t, err)
	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: opWork, Operator: opWork, MinerId: "mw", Stake: 1000})
	require.NoError(t, err)
	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: opIdle, Operator: opIdle, MinerId: "mi", Stake: 1000})
	require.NoError(t, err)
	// mw a servi de la demande ; mi non
	mw, err := f.keeper.Miner.Get(f.ctx, "mw")
	require.NoError(t, err)
	mw.Demand = 10
	require.NoError(t, f.keeper.Miner.Set(f.ctx, "mw", mw))

	ctx4 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(4).WithHeaderInfo(header.Info{Height: 4, AppHash: []byte("h1")})
	require.NoError(t, f.keeper.EndBlock(ctx4))
	chal, err := f.keeper.AvailChallenge.Get(ctx4)
	require.NoError(t, err)
	ctx5 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(5).WithHeaderInfo(header.Info{Height: 5, AppHash: []byte("h1")})
	_, err = srv.ProveAvailability(ctx5, &types.MsgProveAvailability{Creator: opWork, MinerId: "mw", Challenge: chal})
	require.NoError(t, err)
	_, err = srv.ProveAvailability(ctx5, &types.MsgProveAvailability{Creator: opIdle, MinerId: "mi", Challenge: chal})
	require.NoError(t, err)
	// payout de l'époque 1
	ctx8 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(8).WithHeaderInfo(header.Info{Height: 8, AppHash: []byte("h2")})
	require.NoError(t, f.keeper.EndBlock(ctx8))
	require.Greater(t, f.emission.availPaid[opWork], uint64(0), "mineur avec demande -> payé")
	require.Equal(t, uint64(0), f.emission.availPaid[opIdle], "farmeur sans demande -> NON payé (anti-farming V6-02)")
}

// ADR-022 (internal audit 2026-06-26) — en mode INCENTIVÉ (verification_mode=1), la disponibilité EXIGE une VRF :
// un mineur LEGACY (sans vrf_pubkey) ne peut plus prouver sa présence par echo (fin du farm AvailPool sans
// GPU) ; un mineur VRF avec preuve valide passe et encaisse le pool ; le legacy n'est jamais payé. En mode
// legacy (0), l'echo reste accepté (cf. TestAvailabilityVrfProof) -> non-régression e2e.
func TestADR022AvailRequiresVrfInOptimistic(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.AvailEpochBlocks = 4
	p.AvailPayoutBps = 5000
	p.AvailRequireDemand = false // isoler le gate VRF (sans le filtre demande)
	p.VerificationMode = 1       // mode INCENTIVÉ -> VRF obligatoire pour la dispo (ADR-022)
	// verification_mode=1 a des préconditions de cohérence (cf. Params.Validate) — on les pose pour rester valide.
	p.AuditSampleBps = 1000
	p.DisputeWindow = 10
	p.AuditResolveTimeout = 120
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))
	f.emission.avail = 1_000_000

	srv := keeper.NewMsgServerImpl(f.keeper)
	opV, err := f.addressCodec.BytesToString([]byte("operatorVrf022______________"))
	require.NoError(t, err)
	opL, err := f.addressCodec.BytesToString([]byte("operatorLegacy022___________"))
	require.NoError(t, err)

	pk, sk, err := vrf.GenerateKey(nil)
	require.NoError(t, err)
	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: opV, Operator: opV, MinerId: "mv", Stake: 1000, VrfPubkey: hex.EncodeToString(pk)})
	require.NoError(t, err)
	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: opL, Operator: opL, MinerId: "ml", Stake: 1000})
	require.NoError(t, err)

	ctx4 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(4).WithHeaderInfo(header.Info{Height: 4, AppHash: []byte("apphash-022")})
	require.NoError(t, f.keeper.EndBlock(ctx4))
	chal, err := f.keeper.AvailChallenge.Get(ctx4)
	require.NoError(t, err)
	require.NotEmpty(t, chal)

	ctx5 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(5).WithHeaderInfo(header.Info{Height: 5, AppHash: []byte("apphash-022")})

	// LEGACY (sans vrf_pubkey) en mode incentivé -> REFUS (fin de l'echo farmable sans GPU)
	_, err = srv.ProveAvailability(ctx5, &types.MsgProveAvailability{Creator: opL, MinerId: "ml", Challenge: chal})
	require.ErrorIs(t, err, sdkerrors.ErrUnauthorized, "ADR-022 : echo sans VRF refuse en mode incentive")

	// VRF avec preuve VALIDE sur le bon défi -> accepté + présence enregistrée
	pi, err := vrf.Prove(sk, []byte(chal))
	require.NoError(t, err)
	_, err = srv.ProveAvailability(ctx5, &types.MsgProveAvailability{Creator: opV, MinerId: "mv", Challenge: chal, VrfProof: hex.EncodeToString(pi)})
	require.NoError(t, err)
	has, err := f.keeper.Available.Has(ctx5, collections.Join(int64(1), "mv"))
	require.NoError(t, err)
	require.True(t, has, "presence VRF enregistree (epoque 1)")

	// versement de l'époque 1 : seul le mineur VRF est présent -> tout le budget ; le farmeur legacy = 0
	ctx8 := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(8).WithHeaderInfo(header.Info{Height: 8, AppHash: []byte("apphash-022-b")})
	require.NoError(t, f.keeper.EndBlock(ctx8))
	require.Greater(t, f.emission.availPaid[opV], uint64(0), "mineur VRF -> paye")
	require.Equal(t, uint64(0), f.emission.availPaid[opL], "farmeur legacy (sans VRF) -> jamais paye en mode incentive")
}

// ADR-022 PLEIN v1 (internal audit 2026-06-27) — LIVENESS SLASHABLE : un mineur bondé ABSENT (n'a pas prouvé sa
// disponibilité) accumule des échecs ; ≥ avail_fail_k dans la fenêtre -> SLASH borné + BURN. Un mineur PRÉSENT
// n'est jamais touché ; un échec ISOLÉ (< k) ne slashe pas (anti-faux-positif). Tout DORMANT à avail_slash_bps==0.
func TestADR022AvailSlashLiveness(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.AvailEpochBlocks = 4
	p.AvailDeadlineBlocks = 2 // répondre dans les 2 blocs du défi
	p.AvailSlashBps = 2000    // 20 % du stake
	p.AvailSlashMax = 0       // pas de plafond
	p.AvailFailWindow = 10
	p.AvailFailK = 2          // 2 échecs / fenêtre avant slash (un isolé ne slashe pas)
	p.AvailPayoutBps = 0      // isoler le slash (pas de payout)
	p.AvailRequireDemand = false
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	srv := keeper.NewMsgServerImpl(f.keeper)
	opP, err := f.addressCodec.BytesToString([]byte("operatorPresent_____________"))
	require.NoError(t, err)
	opA, err := f.addressCodec.BytesToString([]byte("operatorAbsent______________"))
	require.NoError(t, err)
	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: opP, Operator: opP, MinerId: "mp", Stake: 1000})
	require.NoError(t, err)
	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: opA, Operator: opA, MinerId: "ma", Stake: 1000})
	require.NoError(t, err)

	proveEpoch := func(h int64, op, id string) {
		ctxH := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h).WithHeaderInfo(header.Info{Height: h})
		chal, err := f.keeper.AvailChallenge.Get(ctxH)
		require.NoError(t, err)
		_, err = srv.ProveAvailability(ctxH, &types.MsgProveAvailability{Creator: op, MinerId: id, Challenge: chal})
		require.NoError(t, err)
	}
	endBlock := func(h int64) {
		ctxH := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h).WithHeaderInfo(header.Info{Height: h, AppHash: []byte("ah")})
		require.NoError(t, f.keeper.EndBlock(ctxH))
	}

	endBlock(4)              // roule le défi de l'époque 1 (slash(0) exempté = bootstrap)
	proveEpoch(5, opP, "mp") // mp présent époque 1 ; ma ABSENT

	endBlock(8) // slash check époque 1 -> ma échec #1 (<2, pas de slash)
	ma, err := f.keeper.Miner.Get(f.ctx, "ma")
	require.NoError(t, err)
	require.Equal(t, uint64(1000), ma.Stake, "1 echec ISOLE -> PAS de slash (anti-faux-positif)")
	proveEpoch(9, opP, "mp") // mp présent époque 2 ; ma ABSENT

	endBlock(12) // slash check époque 2 -> ma échec #2 (>=k) -> SLASH 20 %
	ma, err = f.keeper.Miner.Get(f.ctx, "ma")
	require.NoError(t, err)
	require.Equal(t, uint64(800), ma.Stake, "2 echecs dans la fenetre -> slash 20% (1000 -> 800)")
	mp, err := f.keeper.Miner.Get(f.ctx, "mp")
	require.NoError(t, err)
	require.Equal(t, uint64(1000), mp.Stake, "mineur PRESENT -> jamais slashe")
	require.Equal(t, uint64(200), f.bank.burned.AmountOf("udndr").Uint64(), "le slash dispo est BRULE (deflationniste, pas de beneficiaire)")
}

// SLIDING-WINDOW (lot scaling 2026-07-01) — ANTI-GAMING DE FRONTIÈRE : sous le tumbling v1, deux absences
// à cheval sur un reset de fenêtre (ex. époques 4 et 5 avec W=4 démarrée à l'époque 1) ne slashaient PAS
// (le reset effaçait la 1ʳᵉ) — un chronique pouvait caler (k-1) absences avant chaque frontière à jamais.
// Le sliding slashe : ≥ k absences dans N'IMPORTE QUEL span de W époques.
func TestADR022AvailSlashSlidingNoBoundaryGaming(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.AvailEpochBlocks = 4
	p.AvailDeadlineBlocks = 2
	p.AvailSlashBps = 2000 // 20 %
	p.AvailSlashMax = 0
	p.AvailFailWindow = 4
	p.AvailFailK = 2
	p.AvailPayoutBps = 0
	p.AvailRequireDemand = false
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	srv := keeper.NewMsgServerImpl(f.keeper)
	op, err := f.addressCodec.BytesToString([]byte("operatorGaming______________"))
	require.NoError(t, err)
	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: op, Operator: op, MinerId: "mg", Stake: 1000})
	require.NoError(t, err)

	prove := func(h int64) {
		ctxH := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h).WithHeaderInfo(header.Info{Height: h})
		chal, err := f.keeper.AvailChallenge.Get(ctxH)
		require.NoError(t, err)
		_, err = srv.ProveAvailability(ctxH, &types.MsgProveAvailability{Creator: op, MinerId: "mg", Challenge: chal})
		require.NoError(t, err)
	}
	endBlock := func(h int64) {
		ctxH := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h).WithHeaderInfo(header.Info{Height: h, AppHash: []byte("ah")})
		require.NoError(t, f.keeper.EndBlock(ctxH))
	}

	endBlock(4) // roule le défi (époque 0 exemptée)
	// présent aux époques 1-3 (le tumbling v1 aurait ancré sa fenêtre à l'époque 1 et RESET avant l'époque 5)
	prove(5)
	endBlock(8)
	prove(9)
	endBlock(12)
	prove(13)
	endBlock(16)
	// ABSENT aux époques 4 et 5 (à cheval sur l'ex-frontière tumbling) -> le sliding DOIT slasher
	endBlock(20) // époque 4 traitée : 1 absence isolée -> pas de slash
	m, err := f.keeper.Miner.Get(f.ctx, "mg")
	require.NoError(t, err)
	require.Equal(t, uint64(1000), m.Stake, "1 absence isolee -> pas de slash")
	endBlock(24) // époque 5 traitée : 2 absences dans un span de 4 -> SLASH (le tumbling v1 ratait ce cas)
	m, err = f.keeper.Miner.Get(f.ctx, "mg")
	require.NoError(t, err)
	require.Equal(t, uint64(800), m.Stake, "2 absences dans un span de W -> slash 20%% (anti-gaming de frontiere)")
	require.Equal(t, uint64(200), f.bank.burned.AmountOf("udndr").Uint64(), "slash dispo BRULE")
}

// SLIDING-WINDOW — EXPIRATION : des absences ESPACÉES de plus de W époques ne s'additionnent JAMAIS
// (les échecs anciens glissent hors fenêtre) -> un honnête à coupures rares n'accumule pas de dette éternelle.
func TestADR022AvailSlashSlidingExpiry(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams()
	p.AvailEpochBlocks = 4
	p.AvailDeadlineBlocks = 2
	p.AvailSlashBps = 2000
	p.AvailFailWindow = 2 // fenêtre courte : 2 époques
	p.AvailFailK = 2
	p.AvailPayoutBps = 0
	p.AvailRequireDemand = false
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	srv := keeper.NewMsgServerImpl(f.keeper)
	op, err := f.addressCodec.BytesToString([]byte("operatorExpiry______________"))
	require.NoError(t, err)
	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: op, Operator: op, MinerId: "me", Stake: 1000})
	require.NoError(t, err)

	prove := func(h int64) {
		ctxH := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h).WithHeaderInfo(header.Info{Height: h})
		chal, err := f.keeper.AvailChallenge.Get(ctxH)
		require.NoError(t, err)
		_, err = srv.ProveAvailability(ctxH, &types.MsgProveAvailability{Creator: op, MinerId: "me", Challenge: chal})
		require.NoError(t, err)
	}
	endBlock := func(h int64) {
		ctxH := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h).WithHeaderInfo(header.Info{Height: h, AppHash: []byte("ah")})
		require.NoError(t, f.keeper.EndBlock(ctxH))
	}

	endBlock(4)  // défi roulé
	endBlock(8)  // époque 1 : ABSENT (1/2, pas de slash)
	prove(9)
	endBlock(12) // époque 2 : présent
	prove(13)
	endBlock(16) // époque 3 : présent (l'absence de l'époque 1 est sortie de la fenêtre W=2)
	endBlock(20) // époque 4 : ABSENT — jamais 2 absences dans un span de 2 -> PAS de slash
	m, err := f.keeper.Miner.Get(f.ctx, "me")
	require.NoError(t, err)
	require.Equal(t, uint64(1000), m.Stake, "absences espacees > W -> expirees, jamais de slash")
	require.True(t, f.bank.burned.IsZero(), "aucun burn")
}

// ADR-022 PLEIN — DORMANCE : avail_slash_bps==0 (défaut) -> aucun slash de disponibilité même si un mineur bondé
// n'a jamais prouvé sa présence (non-régression e2e : le flux dispo honnête reste inchangé).
func TestADR022AvailSlashDormant(t *testing.T) {
	f := initFixture(t)
	p := types.DefaultParams() // avail_slash_bps=0 -> liveness slashable OFF
	p.AvailEpochBlocks = 4
	p.AvailRequireDemand = false
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	srv := keeper.NewMsgServerImpl(f.keeper)
	opA, err := f.addressCodec.BytesToString([]byte("operatorDormant_____________"))
	require.NoError(t, err)
	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: opA, Operator: opA, MinerId: "ma", Stake: 1000})
	require.NoError(t, err)

	for _, h := range []int64{4, 8, 12, 16} { // 4 époques sans jamais prouver
		ctxH := sdk.UnwrapSDKContext(f.ctx).WithBlockHeight(h).WithHeaderInfo(header.Info{Height: h, AppHash: []byte("ah")})
		require.NoError(t, f.keeper.EndBlock(ctxH))
	}
	ma, err := f.keeper.Miner.Get(f.ctx, "ma")
	require.NoError(t, err)
	require.Equal(t, uint64(1000), ma.Stake, "dispo slashable OFF (defaut) -> stake intact malgre l'absence")
	require.True(t, f.bank.burned.IsZero(), "aucun burn en mode dormant")
}
