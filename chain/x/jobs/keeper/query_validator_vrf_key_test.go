package keeper_test

import (
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// READING BACK AN ANCHORING IS A CHECK AN OPERATOR MUST BE ABLE TO RUN BEFORE BONDING.
//
// A validator bonded without an anchored VRF key contributes nothing to the decentralized seed while
// every published indicator reports it healthy — synced, never jailed, never absent. The write path
// existed and no read path did, so the only ways to find out were the receipt of one's own
// transaction or the node's diagnostic log after bonding, which is too late.
//
// The store is written directly here rather than through `MsgRegisterValidatorVrfKey`: what is under
// test is the READ path and its three answers, and driving the write message would drag in a bonded
// validator fixture that tests something else.
func TestQueryValidatorVrfKeyRendTroisReponses(t *testing.T) {
	f := initFixture(t)
	qs := keeper.NewQueryServerImpl(f.keeper)

	op, err := f.addressCodec.BytesToString([]byte("operatorAddress_____________"))
	require.NoError(t, err)

	// ── ABSENCE IS AN ANSWER, NOT AN ERROR ──────────────────────────────────────────────────────
	// The call must SUCCEED and say "nothing anchored". Returning an error here would make "there is
	// no key" indistinguishable from "I could not find out", and a caller that cannot tell them apart
	// reads the unknown as reassurance on the one criterion that decides whether a validator counts.
	resp, err := qs.ValidatorVrfKey(f.ctx, &types.QueryValidatorVrfKeyRequest{Operator: op})
	require.NoError(t, err, "an operator with no anchored key is a FACT, not a failure to read")
	require.False(t, resp.Anchored)
	require.Empty(t, resp.PubkeyHex)
	require.Equal(t, op, resp.Operator, "the answer names whose key it is about")

	// ── AN ANCHORED KEY READS BACK, VERBATIM ────────────────────────────────────────────────────
	const pub = "cec5fb7e43250745a1b2c3d4e5f60718293a4b5c6d7e8f900112233445566778"
	require.NoError(t, f.keeper.ValidatorVrfPubkey.Set(f.ctx, op, pub))

	resp, err = qs.ValidatorVrfKey(f.ctx, &types.QueryValidatorVrfKeyRequest{Operator: op})
	require.NoError(t, err)
	require.True(t, resp.Anchored)
	require.Equal(t, pub, resp.PubkeyHex,
		"the operator compares this against the public part its node holds: a truncated or reformatted "+
			"value would make a correct anchoring look wrong")

	// ── A MALFORMED ADDRESS IS REFUSED, NOT ANSWERED ────────────────────────────────────────────
	// Answering "not anchored" on a typo sends the operator to re-run a registration that already
	// succeeded under the address they meant.
	_, err = qs.ValidatorVrfKey(f.ctx, &types.QueryValidatorVrfKeyRequest{Operator: "not-an-address"})
	require.Error(t, err, "a malformed address must not come back as a confident 'not anchored'")

	_, err = qs.ValidatorVrfKey(f.ctx, &types.QueryValidatorVrfKeyRequest{Operator: ""})
	require.Error(t, err, "an empty operator is a caller mistake, not an absence of key")
}

// THE SEED HEALTH MUST EXPOSE BOTH BARS, NOT ONE.
//
// `committeeBaseSeedSourced` refuses the seed under EITHER the contributor COUNT or the contributor
// POWER share. Only the count was published, so a health check could report the floor as held while
// the power bar was silently sending every draw back to the legacy fallback.
func TestQueryCommitteeSeedHealthCarriesThePowerShare(t *testing.T) {
	f := initFixture(t)
	qs := keeper.NewQueryServerImpl(f.keeper)

	resp, err := qs.CommitteeSeedHealth(f.ctx, &types.QueryCommitteeSeedHealthRequest{})
	require.NoError(t, err)
	// No seed at this height in the fixture: the field is present in the answer and reads zero, which
	// is the honest value — not a missing field the caller has to guess about.
	require.Equal(t, uint64(0), resp.ContributorPowerBps)
	require.False(t, resp.HasRecentSeed)
}
