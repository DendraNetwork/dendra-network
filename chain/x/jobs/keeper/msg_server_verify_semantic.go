package keeper

import (
	"context"
	"errors"
	"math/big"
	"sort"
	"strconv"
	"strings"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"

	"cosmossdk.io/collections"
	errorsmod "cosmossdk.io/errors"
	sdkerrors "github.com/cosmos/cosmos-sdk/types/errors"
)

// Semantic threshold, fixed in code rather than taken from the message: a caller-supplied threshold is
// a caller-supplied slash. Value in bps (7000 = cos >= 0.70).
const semanticThresholdBps int64 = 7000

// Input bounds: maximum dimension and per-component magnitude.
const (
	embedMaxDim = 384 // enough for a real semantic embedder (all-MiniLM-L6-v2 is 384-dimensional)
	embedMaxMag = 1 << 20
)

// VerifySemantic -- SEMANTIC mode (ADR-020): clustering by integer cosine over the anchored embeddings.
//
// The guards this handler carries, and why each one exists:
//
//	anti-replay   -- the Job is loaded and the verdict refused once the k=3 committee has been judged,
//	                 then the flag is written at the end. Without it, anyone could replay
//	                 VerifySemantic to grind an honest miner's stake down to zero.
//	fixed threshold -- the threshold is a compiled constant, never a message field: letting the caller
//	                 choose it lets the caller choose who gets slashed.
//	big-int cosine -- cosineGE works in math/big, so no int64 overflow can flip a comparison.
//	full committee -- a COMPLETE committee is required (>= CommitteeSize) with a STRICT majority; with
//	                 no strict majority, NOBODY is slashed, which rules out an arbitrary slash.
//	bounded input  -- parseIntVec bounds both dimension and magnitude, and cosineGE requires equal
//	                 lengths.
func (k msgServer) VerifySemantic(ctx context.Context, msg *types.MsgVerifySemantic) (*types.MsgVerifySemanticResponse, error) {
	if _, err := k.addressCodec.StringToBytes(msg.Creator); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidAddress, "invalid creator address")
	}
	params, err := k.Params.Get(ctx)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	// DISABLED IN OPTIMISTIC MODE. Cosine-based k=3 semantic verification has NO role in optimistic mode
	// (verification_mode=1): verification there goes through the VRF audit plus the LLM judge, because
	// cosine is not a judge of correctness (ADR-025/026). This transaction is PERMISSIONLESS, and if the
	// gateway seals the job to 3 miners, 3 commits exist — an attacker could then slash the two HONEST
	// non-primary miners over a mere GPU divergence. It is therefore restricted to mode 0.
	if params.VerificationMode != 0 {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest,
			"verify-semantic is disabled in optimistic mode (verification_mode!=0): verification goes through the VRF audit")
	}
	// Anti-replay.
	job, jobErr := k.Job.Get(ctx, msg.JobId)
	if jobErr != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrKeyNotFound, "job not open")
	}
	// UNIFIED ANTI-DOUBLE-SLASH: guarding only on `+verified` still lets `settle(k3) -> verify` re-slash
	// an outlier that was already punished. The refusal triggers as soon as the k=3 committee is judged.
	if jobK3Adjudicated(job.State) {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "k=3 committee already judged (settled/verified/finalised): anti-replay")
	}
	committee, err := k.assignedCommittee(ctx, msg.JobId, CommitteeSize)
	if err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}

	prefix := msg.JobId + "__"
	vecs := map[string][]int64{}
	if err := k.Commit.Walk(ctx, commitRange(prefix), func(key string, c types.Commit) (bool, error) {
		if strings.HasPrefix(key, prefix) {
			mid := strings.TrimPrefix(key, prefix)
			if committee[mid] {
				if v := parseIntVec(c.ResultCommit); v != nil {
					vecs[mid] = v
				}
			}
		}
		return false, nil
	}); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	// COMPLETE committee: otherwise two miners validate each other with required=1.
	if len(vecs) < CommitteeSize {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "incomplete committee -> verification refused")
	}

	mids := make([]string, 0, len(vecs))
	for mid := range vecs {
		mids = append(mids, mid)
	}
	sort.Strings(mids)
	required := len(vecs)/2 + 1 // STRICT majority (> n/2)

	neighbors := map[string]int{}
	majorityExists := false
	for _, mid := range mids {
		c := 0
		for _, other := range mids {
			if other != mid && cosineGE(vecs[mid], vecs[other], semanticThresholdBps) {
				c++
			}
		}
		neighbors[mid] = c
		if c+1 >= required {
			majorityExists = true
		}
	}
	if !majorityExists {
		return nil, errorsmod.Wrap(sdkerrors.ErrInvalidRequest, "no strict semantic majority -> nobody is slashed")
	}

	pools, err := k.Pools.Get(ctx)
	if err != nil {
		if errors.Is(err, collections.ErrNotFound) {
			pools = types.Pools{}
		} else {
			return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
		}
	}
	for _, mid := range mids {
		if neighbors[mid]+1 >= required {
			continue // inside the strict majority
		}
		miner, mErr := k.Miner.Get(ctx, mid) // OUTLIER -> slash
		if mErr != nil {
			continue
		}
		amt := miner.Stake * params.SlashLeakBps / 10000
		miner.Stake -= amt
		if err := k.Miner.Set(ctx, miner.MinerId, miner); err != nil {
			return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
		}
		pools.Treasury += amt
		// TRACE THE SLASH so that an UPHELD dispute can restore it. SettleSemantic already writes this
		// SlashRecord; without it here, an honest miner slashed by verify and later cleared would NEVER be
		// refunded, because ResolveDispute/AdjudicateDispute only iterate job.SlashRecords.
		job.SlashRecords = append(job.SlashRecords, types.SlashRecord{MinerId: mid, Amount: amt})
	}
	if err := k.Pools.Set(ctx, pools); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, err.Error())
	}
	// Mark the job verified (the write error is checked).
	job.State = job.State + "+verified"
	if err := k.Job.Set(ctx, job.JobId, job); err != nil {
		return nil, errorsmod.Wrap(sdkerrors.ErrLogic, "failed to mark +verified: "+err.Error())
	}
	return &types.MsgVerifySemanticResponse{}, nil
}

// parseIntVec parses "n0,n1,..." into []int64, bounding both dimension and magnitude.
// Components are SIGNED (real sentence-transformer embeddings carry negative values), bounded by
// |v| <= embedMaxMag. A non-negative bag-of-words vector remains a valid case.
// (Also used by settle_semantic.go -> keep the name.)
func parseIntVec(s string) []int64 {
	parts := strings.Split(s, ",")
	if len(parts) == 0 || len(parts) > embedMaxDim {
		return nil
	}
	out := make([]int64, 0, len(parts))
	for _, p := range parts {
		v, err := strconv.ParseInt(strings.TrimSpace(p), 10, 64)
		if err != nil || v < -embedMaxMag || v > embedMaxMag {
			return nil
		}
		out = append(out, v)
	}
	return out
}

// cosineGE reports cos(a,b) >= tbps/10000 using big integers, so NO overflow can flip the comparison.
// It requires vectors of the SAME length (the cosine is otherwise ill-defined). Signed components are
// supported: the `dot.Sign() < 0 -> false` guard prevents a NEGATIVE cosine from wrongly passing the
// threshold, since the comparison is made on dot squared, which would lose the sign.
// (Also used by settle_semantic.go -> keep the name and the signature.)
func cosineGE(a, b []int64, tbps int64) bool {
	if len(a) != len(b) || len(a) == 0 {
		return false
	}
	dot, na, nb := new(big.Int), new(big.Int), new(big.Int)
	t := new(big.Int)
	for i := range a {
		dot.Add(dot, t.Mul(big.NewInt(a[i]), big.NewInt(b[i])))
	}
	for i := range a {
		na.Add(na, t.Mul(big.NewInt(a[i]), big.NewInt(a[i])))
	}
	for i := range b {
		nb.Add(nb, t.Mul(big.NewInt(b[i]), big.NewInt(b[i])))
	}
	if dot.Sign() < 0 || na.Sign() == 0 || nb.Sign() == 0 {
		return false
	}
	lhs := new(big.Int).Mul(dot, dot)
	lhs.Mul(lhs, big.NewInt(100000000))
	rhs := new(big.Int).Mul(big.NewInt(tbps), big.NewInt(tbps))
	rhs.Mul(rhs, na)
	rhs.Mul(rhs, nb)
	return lhs.Cmp(rhs) >= 0
}
