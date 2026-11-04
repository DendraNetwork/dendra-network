package keeper

import (
	"crypto/sha256"
	"encoding/binary"
	"strconv"
)

// availSampleDomain separates the availability obligation draw from the audit and committee draws
// over the same bytes: two draws over one seed must never be correlated.
const availSampleDomain = "dendra/avail-sample/v1|"

// availabilityProofOwed decides whether `minerId` owes a presence proof at `epoch` under
// `avail_sample_bps` (since epoch 15): H(challenge | epoch | minerId) mod 10000 < bps. The
// challenge is the AppHash of the epoch boundary, unknown before it, so a miner cannot know in advance
// which epochs it may skip. 0 is the dormant value and means every bonded miner owes a proof, the
// behaviour before the field existed; 10000 and above also draw everyone. Pure, so it is testable
// outside the keeper, and free of floating point. Its one reader is `runAvailabilitySlash`
// (availability.go), which spares the absence of a miner that owed no proof.
func availabilityProofOwed(challenge string, epoch uint64, minerId string, bps uint64) bool {
	if bps == 0 || bps >= 10000 {
		return true
	}
	sum := sha256.Sum256([]byte(availSampleDomain + challenge + "|" + strconv.FormatUint(epoch, 10) + "|" + minerId))
	return binary.BigEndian.Uint64(sum[:8])%10000 < bps
}
