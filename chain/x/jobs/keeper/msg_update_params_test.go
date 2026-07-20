package keeper_test

import (
	"testing"

	"github.com/stretchr/testify/require"

	"dendra/x/jobs/keeper"
	"dendra/x/jobs/types"
)

func TestMsgUpdateParams(t *testing.T) {
	f := initFixture(t)
	ms := keeper.NewMsgServerImpl(f.keeper)

	params := types.DefaultParams()
	require.NoError(t, f.keeper.Params.Set(f.ctx, params))

	authorityStr, err := f.addressCodec.BytesToString(f.keeper.GetAuthority())
	require.NoError(t, err)

	// INVARIANT #8 (internal audit 2026-06-26) — params HOSTILES : work_gate_bps assez haut pour rendre le wash Sybil
	// +EV (>= 26667 au split par défaut) -> la garde runtime doit REFUSER la MAJ (anti-drain d'émission).
	hostileParams := types.DefaultParams()
	hostileParams.WorkGateBps = 26667

	// default params
	testCases := []struct {
		name      string
		input     *types.MsgUpdateParams
		expErr    bool
		expErrMsg string
	}{
		{
			name: "invalid authority",
			input: &types.MsgUpdateParams{
				Authority: "invalid",
				Params:    params,
			},
			expErr:    true,
			expErrMsg: "invalid authority",
		},
		{
			name: "send enabled param",
			input: &types.MsgUpdateParams{
				Authority: authorityStr,
				Params:    types.Params{},
			},
			expErr: false,
		},
		{
			name: "all good",
			input: &types.MsgUpdateParams{
				Authority: authorityStr,
				Params:    params,
			},
			expErr: false,
		},
		{
			name: "invariant #8 viole (work_gate trop haut -> wash +EV)",
			input: &types.MsgUpdateParams{
				Authority: authorityStr,
				Params:    hostileParams,
			},
			expErr:    true,
			expErrMsg: "invariant #8",
		},
	}

	for _, tc := range testCases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := ms.UpdateParams(f.ctx, tc.input)

			if tc.expErr {
				require.Error(t, err)
				require.Contains(t, err.Error(), tc.expErrMsg)
			} else {
				require.NoError(t, err)
			}
		})
	}
}
