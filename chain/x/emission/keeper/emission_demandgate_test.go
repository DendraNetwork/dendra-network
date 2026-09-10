package keeper_test

import (
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/emission/keeper"
)

// TestEpochReleaseDemandGate — ADR-017: the WORK flow is CAPPED by non-recoverable demand
// (anti self-dealing). `EpochRelease` is the deterministic core of emission.
func TestEpochReleaseDemandGate(t *testing.T) {
	p := keeper.DefaultParams()
	const reserve uint64 = 3_300_000_000000 // 3.3 M DNDR

	// (1) ZERO DEMAND -> work gated to 0; only availability (20 %) and security (~30 %) are released.
	r0 := keeper.EpochRelease(reserve, 0, p)
	require.Equal(t, uint64(0), r0.Work, "zero demand must gate work to 0")
	require.Equal(t, uint64(145_200_000000), r0.Avail)
	require.Equal(t, uint64(217_800_000000), r0.Security)
	require.Equal(t, uint64(363_000_000000), r0.Released) // avail + security = 11 % of 3.3 M (work 0)

	// (2) DEMAND > 0 -> the work flow ACTIVATES, capped at 1.5x demand (below the 50 % ceiling).
	const demand uint64 = 100_000_000000
	rd := keeper.EpochRelease(reserve, demand, p)
	require.Equal(t, uint64(150_000_000000), rd.Work, "work = 1.5x demand while below 50 % of the release")
	require.Equal(t, r0.Avail, rd.Avail, "availability is independent of demand")
	require.Equal(t, r0.Security, rd.Security, "security is independent of demand")

	// (3) CONSERVATION + ANTI-MINT: Work+Avail+Security == Released, and never more than the Reserve.
	require.Equal(t, rd.Work+rd.Avail+rd.Security, rd.Released, "flows are conserved")
	require.LessOrEqual(t, rd.Released, reserve, "anti-mint: Released <= Reserve")
	require.Equal(t, reserve-rd.Released, rd.NewReserve, "the Reserve decreases by exactly that amount")

	// (4) 50 % CEILING: a huge demand releases no more than the work share (50 % of the release).
	rBig := keeper.EpochRelease(reserve, reserve, p) // demand far above the ceiling
	require.Equal(t, uint64(363_000_000000), rBig.Work, "work capped at 50 % of the release (workPool)")
}
