package keeper_test

import (
	"encoding/hex"
	"strconv"
	"strings"
	"testing"

	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/vrf"
)

// FIVE DISTINCT SIGNERS, not five identifiers from one signer.
//
// This loop used to register ids "0".."4" from a SINGLE address. Under ADR-039 the identifier is
// derived from the signer, so that shape is not merely refused — it is meaningless: the second
// iteration would ask the same address for a second identity. The property under test is "a
// registration records its creator", which is about the PAIRING, so the quantity that must vary is
// the address. Rewriting the loop this way keeps the assertion and drops the assumption.
func TestMinerMsgServerCreate(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)

	seen := map[string]bool{}
	for i := 0; i < 5; i++ {
		creator, err := f.addressCodec.BytesToString([]byte("minerSigner" + strconv.Itoa(i) + strings.Repeat("_", 16)))
		require.NoError(t, err)
		id := deriveIDFor(t, f, creator)
		require.False(t, seen[id], "two distinct signers derived the same miner id")
		seen[id] = true

		expected := &types.MsgCreateMiner{Creator: creator,
			MinerId: id,
			Stake:   1000, // >= MinStake (minimum bond rule, ADR-018)
		}
		_, err = srv.CreateMiner(f.ctx, expected)
		require.NoError(t, err)
		rst, err := f.keeper.Miner.Get(f.ctx, expected.MinerId)
		require.NoError(t, err)
		require.Equal(t, expected.Creator, rst.Creator)
	}
}

func TestMinerMsgServerUpdate(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)

	creator, err := f.addressCodec.BytesToString([]byte("signerAddr__________________"))
	require.NoError(t, err)

	unauthorizedAddr, err := f.addressCodec.BytesToString([]byte("unauthorizedAddr___________"))
	require.NoError(t, err)

	idM := deriveIDFor(t, f, creator)
	expected := &types.MsgCreateMiner{Creator: creator,
		MinerId: idM,
		Stake:   1000, // >= MinStake (minimum bond rule, ADR-018)
	}
	_, err = srv.CreateMiner(f.ctx, expected)
	require.NoError(t, err)

	tests := []struct {
		desc    string
		request *types.MsgUpdateMiner
		err     error
	}{
		{
			desc: "invalid address",
			request: &types.MsgUpdateMiner{Creator: "invalid",
				MinerId: idM,
			},
			err: sdkerrors.ErrInvalidAddress,
		},
		{
			desc: "unauthorized",
			request: &types.MsgUpdateMiner{Creator: unauthorizedAddr,
				MinerId: idM,
			},
			err: sdkerrors.ErrUnauthorized,
		},
		{
			desc: "key not found",
			request: &types.MsgUpdateMiner{Creator: creator,
				MinerId: strconv.Itoa(100000),
			},
			err: sdkerrors.ErrKeyNotFound,
		},
		{
			desc: "completed",
			request: &types.MsgUpdateMiner{Creator: creator,
				MinerId: idM,
			},
		},
	}
	for _, tc := range tests {
		t.Run(tc.desc, func(t *testing.T) {
			_, err = srv.UpdateMiner(f.ctx, tc.request)
			if tc.err != nil {
				require.ErrorIs(t, err, tc.err)
			} else {
				require.NoError(t, err)
				rst, err := f.keeper.Miner.Get(f.ctx, expected.MinerId)
				require.NoError(t, err)
				require.Equal(t, expected.Creator, rst.Creator)
			}
		})
	}
}

func TestMinerMsgServerDelete(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)

	creator, err := f.addressCodec.BytesToString([]byte("signerAddr__________________"))
	require.NoError(t, err)

	unauthorizedAddr, err := f.addressCodec.BytesToString([]byte("unauthorizedAddr___________"))
	require.NoError(t, err)

	idM := deriveIDFor(t, f, creator)
	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: creator,
		MinerId: idM,
		Stake:   1000, // >= MinStake (minimum bond rule, ADR-018)
	})
	require.NoError(t, err)

	tests := []struct {
		desc    string
		request *types.MsgDeleteMiner
		err     error
	}{
		{
			desc: "invalid address",
			request: &types.MsgDeleteMiner{Creator: "invalid",
				MinerId: idM,
			},
			err: sdkerrors.ErrInvalidAddress,
		},
		{
			desc: "unauthorized",
			request: &types.MsgDeleteMiner{Creator: unauthorizedAddr,
				MinerId: idM,
			},
			err: sdkerrors.ErrUnauthorized,
		},
		{
			desc: "key not found",
			request: &types.MsgDeleteMiner{Creator: creator,
				MinerId: strconv.Itoa(100000),
			},
			err: sdkerrors.ErrKeyNotFound,
		},
		{
			desc: "completed",
			request: &types.MsgDeleteMiner{Creator: creator,
				MinerId: idM,
			},
		},
	}
	for _, tc := range tests {
		t.Run(tc.desc, func(t *testing.T) {
			_, err = srv.DeleteMiner(f.ctx, tc.request)
			if tc.err != nil {
				require.ErrorIs(t, err, tc.err)
			} else {
				require.NoError(t, err)
				found, err := f.keeper.Miner.Has(f.ctx, tc.request.MinerId)
				require.NoError(t, err)
				require.False(t, found)
			}
		})
	}
}

// The X25519 public key is ANCHORED on-chain at registration and validated (32 bytes, hex encoded).
//
// EVERY case below sends the DERIVED miner id, including the ones expected to fail, and that is the
// point of this comment. The ADR-039 identity guard sits at the TOP of CreateMiner and returns the
// same sentinel error as the pubkey check (`ErrInvalidRequest`). Had the failing cases kept their old
// literal ids, they would still be green — refused by the identity guard, never reaching the pubkey
// validation this test exists to exercise. A test can be defeated by a guard added upstream of it
// without a single assertion changing, so the failures assert on the MESSAGE, not only on the class.
//
// The four cases also use FOUR DIFFERENT SIGNERS: with a derived id, one address registers one miner,
// so "mp" and "mempty" could no longer both come from the same key.
func TestMinerEncPubkeyAnchoring(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	validPub := strings.Repeat("ab", 32) // 32 bytes in hex

	creatorOK, err := f.addressCodec.BytesToString([]byte("encPubkeyOK_________________"))
	require.NoError(t, err)
	idOK := deriveIDFor(t, f, creatorOK)
	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: creatorOK, MinerId: idOK, Stake: 1000, EncPubkey: validPub})
	require.NoError(t, err)
	got, err := f.keeper.Miner.Get(f.ctx, idOK)
	require.NoError(t, err)
	require.Equal(t, validPub, got.EncPubkey) // anchored

	creatorBad, err := f.addressCodec.BytesToString([]byte("encPubkeyBad________________"))
	require.NoError(t, err)
	idBad := deriveIDFor(t, f, creatorBad)

	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: creatorBad, MinerId: idBad, Stake: 1000, EncPubkey: "abcd"})
	require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest)
	require.Contains(t, err.Error(), "enc_pubkey", "must be refused by the PUBKEY check, not by the identity guard") // wrong length

	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: creatorBad, MinerId: idBad, Stake: 1000, EncPubkey: strings.Repeat("zz", 32)})
	require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest)
	require.Contains(t, err.Error(), "enc_pubkey", "must be refused by the PUBKEY check, not by the identity guard") // not hex

	// Neither refusal registered anything.
	has, err := f.keeper.Miner.Has(f.ctx, idBad)
	require.NoError(t, err)
	require.False(t, has)

	creatorEmpty, err := f.addressCodec.BytesToString([]byte("encPubkeyEmpty______________"))
	require.NoError(t, err)
	idEmpty := deriveIDFor(t, f, creatorEmpty)
	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: creatorEmpty, MinerId: idEmpty, Stake: 1000})
	require.NoError(t, err) // empty is accepted for backward compatibility
}

// Rotation of anchored keys: the operator replaces the enc/vrf pubkeys while Demand and Stake are PRESERVED.
func TestRotateMinerKeys(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	op, err := f.addressCodec.BytesToString([]byte("minerOperator_______________"))
	require.NoError(t, err)
	other, err := f.addressCodec.BytesToString([]byte("otherOperator_______________"))
	require.NoError(t, err)
	require.NoError(t, gvSetMiner(f, f.ctx, "m0", types.Miner{
		MinerId: "m0", Operator: op, Stake: 2000, Demand: 42,
		EncPubkey: strings.Repeat("aa", 32), VrfPubkey: strings.Repeat("bb", 32)}))

	newEnc := strings.Repeat("cc", 32)
	newVrf := strings.Repeat("dd", 32)
	// non-operator -> refused
	_, err = srv.RotateMinerKeys(f.ctx, &types.MsgRotateMinerKeys{Creator: other, MinerId: "m0", NewEncPubkey: newEnc})
	require.ErrorIs(t, err, sdkerrors.ErrUnauthorized)
	// invalid key -> refused
	_, err = srv.RotateMinerKeys(f.ctx, &types.MsgRotateMinerKeys{Creator: op, MinerId: "m0", NewEncPubkey: "zz"})
	require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest)
	// valid rotation -> pubkeys replaced, Demand and Stake PRESERVED
	_, err = srv.RotateMinerKeys(f.ctx, &types.MsgRotateMinerKeys{Creator: op, MinerId: "m0", NewEncPubkey: newEnc, NewVrfPubkey: newVrf})
	require.NoError(t, err)
	m, err := f.keeper.Miner.Get(f.ctx, "m0")
	require.NoError(t, err)
	require.Equal(t, newEnc, m.EncPubkey)
	require.Equal(t, newVrf, m.VrfPubkey)
	require.Equal(t, uint64(42), m.Demand) // PRESERVED, unlike a Delete followed by a Create, which resets it to 0
	require.Equal(t, uint64(2000), m.Stake)
}

// A validator anchors its own VRF public key (self-authorised); an invalid key is refused; the anchored
// value reads back correctly.
func TestRegisterValidatorVrfKey(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	op, err := f.addressCodec.BytesToString([]byte("valOperator_________________"))
	require.NoError(t, err)

	// Real VRF key pair plus a proof of possession over "dendra/vrf-pop/<op>".
	pk, sk, err := vrf.GenerateKey(nil)
	require.NoError(t, err)
	pkHex := hex.EncodeToString(pk)
	pop, err := vrf.Prove(sk, []byte("dendra/vrf-pop/"+op))
	require.NoError(t, err)
	popHex := hex.EncodeToString(pop)

	// invalid pubkey -> refused
	_, err = srv.RegisterValidatorVrfKey(f.ctx, &types.MsgRegisterValidatorVrfKey{Creator: op, VrfPubkey: "zz", VrfPop: popHex})
	require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest)

	// missing proof of possession -> refused
	_, err = srv.RegisterValidatorVrfKey(f.ctx, &types.MsgRegisterValidatorVrfKey{Creator: op, VrfPubkey: pkHex, VrfPop: ""})
	require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest)

	// proof of possession forged with ANOTHER key -> refused (the signer does not hold pk)
	_, otherSk, err := vrf.GenerateKey(nil)
	require.NoError(t, err)
	badPop, err := vrf.Prove(otherSk, []byte("dendra/vrf-pop/"+op))
	require.NoError(t, err)
	_, err = srv.RegisterValidatorVrfKey(f.ctx, &types.MsgRegisterValidatorVrfKey{Creator: op, VrfPubkey: pkHex, VrfPop: hex.EncodeToString(badPop)})
	require.ErrorIs(t, err, sdkerrors.ErrUnauthorized)

	// valid proof of possession -> anchored under the signing account and readable back
	_, err = srv.RegisterValidatorVrfKey(f.ctx, &types.MsgRegisterValidatorVrfKey{Creator: op, VrfPubkey: pkHex, VrfPop: popHex})
	require.NoError(t, err)
	got, err := f.keeper.ValidatorVrfPubkey.Get(f.ctx, op)
	require.NoError(t, err)
	require.Equal(t, pkHex, got)
}
