package keeper_test

import (
	"testing"

	"github.com/stretchr/testify/require"

	"dendra/x/emission/keeper"
)

// TestEpochReleaseDemandGate — TK-02 / ADR-017 : le flux TRAVAIL est PLAFONNÉ par la demande
// non-récupérable (anti-self-dealing). Cœur déterministe `EpochRelease`, jusqu'ici NON testé dans
// chain (l'en-tête le prétendait : `emission_test.go` n'avait jamais été versionné ici).
func TestEpochReleaseDemandGate(t *testing.T) {
	p := keeper.DefaultParams()
	const reserve uint64 = 3_300_000_000000 // 3,3 M DNDR

	// (1) DEMANDE 0 -> travail gaté à 0 ; seuls avail (20 %) + sécurité (≈30 %) du release sortent.
	r0 := keeper.EpochRelease(reserve, 0, p)
	require.Equal(t, uint64(0), r0.Work, "demande 0 doit gater le travail à 0")
	require.Equal(t, uint64(145_200_000000), r0.Avail)
	require.Equal(t, uint64(217_800_000000), r0.Security)
	require.Equal(t, uint64(363_000_000000), r0.Released) // avail + sécu = 11 % de 3,3 M (work 0)

	// (2) DEMANDE > 0 -> le flux travail S'ACTIVE, plafonné à 1,5× la demande (sous le plafond 50 %).
	const demand uint64 = 100_000_000000
	rd := keeper.EpochRelease(reserve, demand, p)
	require.Equal(t, uint64(150_000_000000), rd.Work, "travail = 1,5× demande tant que < 50 % du release")
	require.Equal(t, r0.Avail, rd.Avail, "avail indépendant de la demande")
	require.Equal(t, r0.Security, rd.Security, "sécurité indépendante de la demande")

	// (3) CONSERVATION + ANTI-MINT : Work+Avail+Security == Released ; jamais plus que la Réserve.
	require.Equal(t, rd.Work+rd.Avail+rd.Security, rd.Released, "conservation des flux")
	require.LessOrEqual(t, rd.Released, reserve, "anti-mint : Released <= Réserve")
	require.Equal(t, reserve-rd.Released, rd.NewReserve, "Réserve décroissante exacte")

	// (4) PLAFOND 50 % : une demande énorme ne libère pas plus que la part travail (50 % du release).
	rBig := keeper.EpochRelease(reserve, reserve, p) // demande >> plafond
	require.Equal(t, uint64(363_000_000000), rBig.Work, "travail plafonné à 50 % du release (workPool)")
}
