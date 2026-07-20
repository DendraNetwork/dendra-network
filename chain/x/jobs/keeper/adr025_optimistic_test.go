package keeper_test

import (
	"testing"

	"github.com/stretchr/testify/require"

	"dendra/x/jobs/keeper"
	"dendra/x/jobs/types"
)

// ADR-025 — vérification OPTIMISTE par échantillonnage (M0 params / M2 règlement k=1 / M5 garde Nash).
// Tout est DORMANT par défaut (verification_mode=0) : les tests existants (mode 0) restent verts sans
// modification — c'est la preuve de non-régression du chemin redondant k=3.

// M0 — les nouveaux paramètres valent 0/0 par défaut (dormant) et sont bornés par Validate.
func TestADR025ParamsDefaultDormant(t *testing.T) {
	p := types.DefaultParams()
	require.Equal(t, uint64(0), p.VerificationMode, "défaut = redundant (dormant)")
	require.Equal(t, uint64(0), p.AuditSampleBps, "défaut = aucun audit")
	require.NoError(t, p.Validate())

	bad := types.DefaultParams()
	bad.VerificationMode = 2
	require.Error(t, bad.Validate(), "verification_mode hors {0,1} rejeté")

	bad2 := types.DefaultParams()
	bad2.AuditSampleBps = 10001
	require.Error(t, bad2.Validate(), "audit_sample_bps > 10000 rejeté")
}

// M5 — en mode optimiste, une fee qui casse l'inégalité de Nash est REFUSÉE à l'ouverture ; une petite
// fee passe. (Mode 0 : garde inerte — couvert par les tests OpenJob existants.)
func TestADR025NashGuardOpenJob(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	creator, err := f.addressCodec.BytesToString([]byte("signerAddr__________________"))
	require.NoError(t, err)

	p := types.DefaultParams() // min_stake=1000, slash_leak_bps=8000
	p.VerificationMode = 1
	p.AuditSampleBps = 1000 // 10 %
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	// Nash sûr ssi  audit·slash·min_stake > (10000-audit)·10000·fee  =>  8e9 > 9e7·fee  =>  fee < ~89.
	_, err = srv.OpenJob(f.ctx, &types.MsgOpenJob{Creator: creator, JobId: "nashHigh", Fee: 100000})
	require.Error(t, err, "fee trop élevée -> refusée (Nash)")

	_, err = srv.OpenJob(f.ctx, &types.MsgOpenJob{Creator: creator, JobId: "nashOk", Fee: 10})
	require.NoError(t, err, "petite fee -> acceptée")
}

// M2 — en mode optimiste, le job est réglé en payant UN SEUL mineur primaire (k=1) sur son commit unique ;
// le job est marqué paid+optimistic ; un second règlement est rejeté (anti-rejeu jobIsPaid).
func TestADR025OptimisticSettleK1(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	creator, err := f.addressCodec.BytesToString([]byte("signerAddr__________________"))
	require.NoError(t, err)

	p := types.DefaultParams()
	p.VerificationMode = 1
	p.AuditSampleBps = 1000
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	// Un seul mineur enregistré -> il est le primaire k=1 (et le comité k=3 dégénéré).
	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: creator, MinerId: "m1", Operator: creator, Stake: 1000})
	require.NoError(t, err)

	// Petite fee (passe la garde Nash). delay=0 -> graine figée à l'open -> commit immédiat OK.
	_, err = srv.OpenJob(f.ctx, &types.MsgOpenJob{Creator: creator, JobId: "jobO", Fee: 30})
	require.NoError(t, err)

	// Le primaire ancre un commit (vecteur entier valide).
	_, err = srv.CreateCommit(f.ctx, &types.MsgCreateCommit{Creator: creator, JobId: "jobO__m1", ResultCommit: "1,2,3"})
	require.NoError(t, err)

	// Règlement OPTIMISTE k=1 : paie le primaire, marque le job.
	_, err = srv.SettleSemantic(f.ctx, &types.MsgSettleSemantic{Creator: creator, JobId: "jobO"})
	require.NoError(t, err, "règlement optimiste k=1 accepté")

	job, err := f.keeper.Job.Get(f.ctx, "jobO")
	require.NoError(t, err)
	require.Contains(t, job.State, "optimistic", "job marqué optimistic")
	require.Contains(t, job.State, "paid", "job marqué paid (anti-rejeu)")
	require.Equal(t, "m1", job.MinerId, "primaire enregistré sur le job")

	// Burn doux v5 appliqué depuis l'escrow (fee=30, fee_burn_bps=500 -> 1 udndr brûlé).
	require.False(t, f.bank.burned.IsZero(), "burn doux appliqué au règlement optimiste")

	// Anti-rejeu : second règlement refusé.
	_, err = srv.SettleSemantic(f.ctx, &types.MsgSettleSemantic{Creator: creator, JobId: "jobO"})
	require.Error(t, err, "double règlement rejeté")
}

// B0.4 (décision internal audit 2026-06-21) — le règlement OPTIMISTE prend désormais le cut protocole (split
// validators/team/treasury) ET crédite `Demand = treasury+team` quand client≠operateur (sinon la subvention
// d'émission ne récompensait jamais le travail d'inférence + tokenomics 85/15 incohérente). Anti-self-dealing
// conservé. (Le slash d'un primaire audité reverse ce Demand — couvert par les tests ADR-028.)
func TestADR025OptimisticCutAndDemand(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	op, err := f.addressCodec.BytesToString([]byte("signerAddr__________________"))
	require.NoError(t, err)
	client, err := f.addressCodec.BytesToString([]byte("clientAAAA__________________"))
	require.NoError(t, err)

	p := types.DefaultParams() // protocol_fee_bps=1500, validator=5000, team=2000, fee_burn_bps=500
	p.VerificationMode = 1
	p.AuditSampleBps = 1000
	require.NoError(t, f.keeper.Params.Set(f.ctx, p))

	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: op, Operator: op, MinerId: "m1", Stake: 1000})
	require.NoError(t, err)

	// Client DISTINCT de l'opérateur (anti-self-dealing) ; fee=80 passe la garde Nash (fee < ~89).
	_, err = srv.OpenJob(f.ctx, &types.MsgOpenJob{Creator: client, JobId: "jCD", Fee: 80})
	require.NoError(t, err)
	_, err = srv.CreateCommit(f.ctx, &types.MsgCreateCommit{Creator: op, JobId: "jCD__m1", ResultCommit: "1,2,3"})
	require.NoError(t, err)
	_, err = srv.SettleSemantic(f.ctx, &types.MsgSettleSemantic{Creator: client, JobId: "jCD"})
	require.NoError(t, err)

	// fee=80 -> burn=4, cut=12 (validators=6, team=2, treasury=4), minerNet=64 ; Demand=treasury+team=6.
	m, err := f.keeper.Miner.Get(f.ctx, "m1")
	require.NoError(t, err)
	require.Equal(t, uint64(6), m.Demand, "Demand=treasury+team crédité au règlement optimiste (client≠operateur)")

	pools, err := f.keeper.Pools.Get(f.ctx)
	require.NoError(t, err)
	require.Equal(t, uint64(6), pools.Validators, "cut -> validators")
	require.Equal(t, uint64(2), pools.Team, "cut -> team")
	require.Equal(t, uint64(4), pools.Treasury, "cut -> treasury")
}
