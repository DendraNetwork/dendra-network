package keeper_test

import (
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// An operator must be able to read back an anchored VRF key before bonding.
//
// A validator bonded without an anchored VRF key contributes nothing to the decentralized seed while
// every published indicator reports it healthy (synced, never jailed, never absent). Without a read
// path, the only ways to find out were the receipt of one's own transaction or the node's log after
// bonding, which is too late.
//
// The store is written directly rather than through `MsgRegisterValidatorVrfKey`: the read path and its
// three answers are under test, and the write message would need a bonded validator fixture.
func TestQueryValidatorVrfKeyGivesThreeAnswers(t *testing.T) {
	f := initFixture(t)
	qs := keeper.NewQueryServerImpl(f.keeper)

	op, err := f.addressCodec.BytesToString([]byte("operatorAddress_____________"))
	require.NoError(t, err)

	// Absence is an answer. The call must succeed and say "nothing anchored": an error here would make
	// "there is no key" indistinguishable from "I could not find out", and a caller that cannot tell them
	// apart reads the unknown as reassurance on the criterion that decides whether a validator counts.
	resp, err := qs.ValidatorVrfKey(f.ctx, &types.QueryValidatorVrfKeyRequest{Operator: op})
	require.NoError(t, err, "an operator with no anchored key is a fact, not a failure to read")
	require.False(t, resp.Anchored)
	require.Empty(t, resp.PubkeyHex)
	require.Equal(t, op, resp.Operator, "the answer names whose key it is about")

	// An anchored key reads back verbatim.
	const pub = "cec5fb7e43250745a1b2c3d4e5f60718293a4b5c6d7e8f900112233445566778"
	require.NoError(t, f.keeper.ValidatorVrfPubkey.Set(f.ctx, op, pub))

	resp, err = qs.ValidatorVrfKey(f.ctx, &types.QueryValidatorVrfKeyRequest{Operator: op})
	require.NoError(t, err)
	require.True(t, resp.Anchored)
	require.Equal(t, pub, resp.PubkeyHex,
		"the operator compares this against the public part its node holds: a truncated or reformatted "+
			"value would make a correct anchoring look wrong")

	// A malformed address is refused. Answering "not anchored" on a typo would send the operator to re-run a
	// registration that already succeeded under the address they meant.
	_, err = qs.ValidatorVrfKey(f.ctx, &types.QueryValidatorVrfKeyRequest{Operator: "not-an-address"})
	require.Error(t, err, "a malformed address must not come back as a confident 'not anchored'")

	_, err = qs.ValidatorVrfKey(f.ctx, &types.QueryValidatorVrfKeyRequest{Operator: ""})
	require.Error(t, err, "an empty operator is a caller mistake, not an absence of key")
}

// The seed health exposes both bars.
//
// `committeeBaseSeedSourced` refuses the seed under either the contributor count or the contributor
// power share. When only the count was published, a health check could report the floor as held while
// the power bar sent every draw back to the legacy fallback.
func TestQueryCommitteeSeedHealthCarriesThePowerShare(t *testing.T) {
	f := initFixture(t)
	qs := keeper.NewQueryServerImpl(f.keeper)

	resp, err := qs.CommitteeSeedHealth(f.ctx, &types.QueryCommitteeSeedHealthRequest{})
	require.NoError(t, err)
	// No seed at this height in the fixture: the field is present in the answer and reads zero, which is the
	// honest value, so the caller has nothing to guess.
	require.Equal(t, uint64(0), resp.ContributorPowerBps)
	require.False(t, resp.HasRecentSeed)
}
