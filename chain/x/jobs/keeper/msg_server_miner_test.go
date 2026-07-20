package keeper_test

import (
	"encoding/hex"
	"strconv"
	"strings"
	"testing"

	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
	"github.com/stretchr/testify/require"

	"dendra/x/jobs/keeper"
	"dendra/x/jobs/types"
	"dendra/x/jobs/vrf"
)

func TestMinerMsgServerCreate(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	creator, err := f.addressCodec.BytesToString([]byte("signerAddr__________________"))
	require.NoError(t, err)

	for i := 0; i < 5; i++ {
		expected := &types.MsgCreateMiner{Creator: creator,
			MinerId: strconv.Itoa(i),
			Stake:   1000, // >= MinStake (regle v4 bond minimum, ADR-018)
		}
		_, err := srv.CreateMiner(f.ctx, expected)
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

	expected := &types.MsgCreateMiner{Creator: creator,
		MinerId: strconv.Itoa(0),
		Stake:   1000, // >= MinStake (regle v4 bond minimum, ADR-018)
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
				MinerId: strconv.Itoa(0),
			},
			err: sdkerrors.ErrInvalidAddress,
		},
		{
			desc: "unauthorized",
			request: &types.MsgUpdateMiner{Creator: unauthorizedAddr,
				MinerId: strconv.Itoa(0),
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
				MinerId: strconv.Itoa(0),
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

	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: creator,
		MinerId: strconv.Itoa(0),
		Stake:   1000, // >= MinStake (regle v4 bond minimum, ADR-018)
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
				MinerId: strconv.Itoa(0),
			},
			err: sdkerrors.ErrInvalidAddress,
		},
		{
			desc: "unauthorized",
			request: &types.MsgDeleteMiner{Creator: unauthorizedAddr,
				MinerId: strconv.Itoa(0),
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
				MinerId: strconv.Itoa(0),
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


// MM-02 (audit v5) : la pub X25519 est ANCRÉE on-chain à l'enregistrement, et validée (32 octets hex).
func TestMinerEncPubkeyAnchoring(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	creator, err := f.addressCodec.BytesToString([]byte("signerAddr__________________"))
	require.NoError(t, err)
	validPub := strings.Repeat("ab", 32) // 32 octets en hex

	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: creator, MinerId: "mp", Stake: 1000, EncPubkey: validPub})
	require.NoError(t, err)
	got, err := f.keeper.Miner.Get(f.ctx, "mp")
	require.NoError(t, err)
	require.Equal(t, validPub, got.EncPubkey) // ancré

	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: creator, MinerId: "mbad", Stake: 1000, EncPubkey: "abcd"})
	require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest) // mauvaise longueur

	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: creator, MinerId: "mbad2", Stake: 1000, EncPubkey: strings.Repeat("zz", 32)})
	require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest) // pas hex

	_, err = srv.CreateMiner(f.ctx, &types.MsgCreateMiner{Creator: creator, MinerId: "mempty", Stake: 1000})
	require.NoError(t, err) // vide -> rétro-compat
}

// V6-03 — rotation des clés ancrées : l'opérateur remplace enc/vrf pubkey, Demand/Stake PRÉSERVÉS.
func TestRotateMinerKeys(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	op, err := f.addressCodec.BytesToString([]byte("minerOperator_______________"))
	require.NoError(t, err)
	other, err := f.addressCodec.BytesToString([]byte("autreOperateur______________"))
	require.NoError(t, err)
	require.NoError(t, f.keeper.Miner.Set(f.ctx, "m0", types.Miner{
		MinerId: "m0", Operator: op, Stake: 2000, Demand: 42,
		EncPubkey: strings.Repeat("aa", 32), VrfPubkey: strings.Repeat("bb", 32)}))

	newEnc := strings.Repeat("cc", 32)
	newVrf := strings.Repeat("dd", 32)
	// non-opérateur -> refus
	_, err = srv.RotateMinerKeys(f.ctx, &types.MsgRotateMinerKeys{Creator: other, MinerId: "m0", NewEncPubkey: newEnc})
	require.ErrorIs(t, err, sdkerrors.ErrUnauthorized)
	// clé invalide -> refus
	_, err = srv.RotateMinerKeys(f.ctx, &types.MsgRotateMinerKeys{Creator: op, MinerId: "m0", NewEncPubkey: "zz"})
	require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest)
	// rotation valide -> pubkeys changées, Demand/Stake PRÉSERVÉS
	_, err = srv.RotateMinerKeys(f.ctx, &types.MsgRotateMinerKeys{Creator: op, MinerId: "m0", NewEncPubkey: newEnc, NewVrfPubkey: newVrf})
	require.NoError(t, err)
	m, err := f.keeper.Miner.Get(f.ctx, "m0")
	require.NoError(t, err)
	require.Equal(t, newEnc, m.EncPubkey)
	require.Equal(t, newVrf, m.VrfPubkey)
	require.Equal(t, uint64(42), m.Demand) // PRÉSERVÉ (vs Delete+Create qui le remettrait à 0)
	require.Equal(t, uint64(2000), m.Stake)
}


// E4 -- un validateur ancre sa cle publique VRF (auto-autorisee) ; cle invalide refusee ; relecture OK.
func TestRegisterValidatorVrfKey(t *testing.T) {
	f := initFixture(t)
	srv := keeper.NewMsgServerImpl(f.keeper)
	op, err := f.addressCodec.BytesToString([]byte("valOperator_________________"))
	require.NoError(t, err)

	// VE-02 : vraie paire de cles VRF + proof-of-possession sur "dendra/vrf-pop/<op>".
	pk, sk, err := vrf.GenerateKey(nil)
	require.NoError(t, err)
	pkHex := hex.EncodeToString(pk)
	pop, err := vrf.Prove(sk, []byte("dendra/vrf-pop/"+op))
	require.NoError(t, err)
	popHex := hex.EncodeToString(pop)

	// pubkey invalide -> refus
	_, err = srv.RegisterValidatorVrfKey(f.ctx, &types.MsgRegisterValidatorVrfKey{Creator: op, VrfPubkey: "zz", VrfPop: popHex})
	require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest)

	// PoP absent -> refus
	_, err = srv.RegisterValidatorVrfKey(f.ctx, &types.MsgRegisterValidatorVrfKey{Creator: op, VrfPubkey: pkHex, VrfPop: ""})
	require.ErrorIs(t, err, sdkerrors.ErrInvalidRequest)

	// PoP forge avec une AUTRE cle -> refus (on ne possede pas pk)
	_, otherSk, err := vrf.GenerateKey(nil)
	require.NoError(t, err)
	badPop, err := vrf.Prove(otherSk, []byte("dendra/vrf-pop/"+op))
	require.NoError(t, err)
	_, err = srv.RegisterValidatorVrfKey(f.ctx, &types.MsgRegisterValidatorVrfKey{Creator: op, VrfPubkey: pkHex, VrfPop: hex.EncodeToString(badPop)})
	require.ErrorIs(t, err, sdkerrors.ErrUnauthorized)

	// PoP valide -> ancree sous le compte signataire, relisible
	_, err = srv.RegisterValidatorVrfKey(f.ctx, &types.MsgRegisterValidatorVrfKey{Creator: op, VrfPubkey: pkHex, VrfPop: popHex})
	require.NoError(t, err)
	got, err := f.keeper.ValidatorVrfPubkey.Get(f.ctx, op)
	require.NoError(t, err)
	require.Equal(t, pkHex, got)
}
