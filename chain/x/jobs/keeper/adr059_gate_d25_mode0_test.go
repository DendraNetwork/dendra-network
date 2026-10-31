package keeper_test

import (
	"fmt"
	"os"
	"regexp"
	"strconv"
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// d25RedoQuorumFloor is the value of keeper.redoQuorumFloor (policy_params.go), which has no test
// hook. TestADR059_TheRedoFloorCopyMatchesTheKeeper reads the shipped constant and refuses a mismatch.
const d25RedoQuorumFloor = 3

func TestADR059_TheRedoFloorCopyMatchesTheKeeper(t *testing.T) {
	src, err := os.ReadFile("policy_params.go")
	require.NoError(t, err)
	m := regexp.MustCompile(`(?m)^const redoQuorumFloor = (\d+)\s*$`).FindAllSubmatch(src, -1)
	require.Len(t, m, 1, "policy_params.go declares redoQuorumFloor once, in the form this test reads")
	v, err := strconv.Atoi(string(m[0][1]))
	require.NoError(t, err)
	require.Equal(t, v, d25RedoQuorumFloor, "the copy follows the shipped redoQuorumFloor")
}

// Decision 5: in verification mode 0, Validate bounds a posted audit_committee_draw_size by
// effectiveAuditFloor, which is the governed audit_min_quorum. A quorum of 1 or 2 therefore admits a
// draw size of 1 or 2, and the dispute path is open in that mode (DisputeVerdict gates on
// dispute_window only, and the redo jury is drawn by anchorRedoCommittee). The set goes through
// governance, the audit jury is the posted size, and the redo jury is raised to redoQuorumFloor.
func TestADR059_ModeZeroSmallDrawSizeSeatsThePostedAuditJuryAndTheRedoFloor(t *testing.T) {
	const h = uint64(1_000_000)
	for _, size := range []uint64{1, 2} {
		t.Run(fmt.Sprintf("audit_min_quorum=%d/audit_committee_draw_size=%d", size, size), func(t *testing.T) {
			f := initFixture(t)
			srv := keeper.NewMsgServerImpl(f.keeper)
			p := types.DefaultParams()
			require.Equal(t, uint64(0), p.VerificationMode, "premise: the default mode is 0")
			p.AuditMinQuorum = size
			p.AuditCommitteeDrawSize = size
			p.DisputeWindow = 100
			p.AuditResolveTimeout = 240
			_, err := srv.UpdateParams(f.ctx, &types.MsgUpdateParams{Authority: f.authority, Params: p})
			require.NoError(t, err, "a mode-0 set governance can post")
			stored, err := f.keeper.Params.Get(f.ctx)
			require.NoError(t, err)
			require.Equal(t, size, stored.AuditCommitteeDrawSize)
			require.Equal(t, size, stored.AuditMinQuorum)
			require.Equal(t, uint64(100), stored.DisputeWindow, "the dispute path is open")
			require.Greater(t, stored.AuditResolveTimeout, stored.DisputeWindow, "an opened dispute has a deadline")

			for i := 0; i < 8; i++ {
				d25Judge(t, f, fmt.Sprintf("d25-m%02d", i), h, h)
			}
			audit, redo := d25DrawBoth(t, f, fmt.Sprintf("d25-jMode0q%d", size), h)
			require.Len(t, audit, int(size), "the audit jury at the posted size")
			require.Len(t, redo, d25RedoQuorumFloor, "the redo jury raised to redoQuorumFloor")
		})
	}
}
