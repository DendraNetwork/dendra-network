package app

import (
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"testing"

	sdk "github.com/cosmos/cosmos-sdk/types"
	stakingtypes "github.com/cosmos/cosmos-sdk/x/staking/types"
	"github.com/stretchr/testify/require"
	"gopkg.in/yaml.v3"
)

// ═══ THE FLOOR TO BECOME A VALIDATOR: WHERE IT IS, WHERE IT IS NOT, AND WHAT IT IS WORTH ═════════
//
// WHAT `query staking validators` SHOWS AND WHAT IT MEANS. Every validator on this chain reports
// `min_self_delegation: 1`. Read as a chain rule, that says one micro-DNDR is enough to hold a seat
// on a REWARDED network — an open Sybil surface. Read correctly, it says nothing at all about the
// chain: `min_self_delegation` is supplied BY THE CANDIDATE in its own `MsgCreateValidator` and is
// only ever compared against that same candidate's stake. It is a promise a validator makes to its
// delegators, not a bar the protocol raises. An attacker simply declares 1 as well.
//
// WHAT IS ACTUALLY IN FORCE, and it is an accident rather than a decision: consensus power is
// `tokens / PowerReduction` in whole units, and the bonding loop STOPS at the first validator whose
// potential power is zero. With `PowerReduction = 10^6` udndr, anything below 1 DNDR has power 0 and
// is NEVER bonded. So a floor does exist, it equals exactly one DNDR out of a ten-million supply, and
// it is written nowhere: it is a side effect of a constant chosen for power granularity. Lower that
// constant for finer granularity — a plausible, well-intentioned change — and the floor disappears
// without a line of the repository having mentioned it. Case (2) is what makes that visible.
//
// WHAT IS NOT TRUE, and is worth writing down because the opposite is easy to assume. Dust validators
// do NOT capture the rewarded stream. The security slice is sent to `fee_collector` and distributed by
// `x/distribution` STRICTLY IN PROPORTION TO VOTING POWER, with no per-head term and no proposer bonus
// in this SDK line. A hundred validators holding one DNDR each collect exactly what one validator
// holding a hundred DNDR collects. The reason to raise the floor is therefore not reward capture; it
// is that identities are the unit several other guards count in, and identities must not be cheap.
//
// WHERE A REAL FLOOR CAN BE POSED. Not in the staking genesis parameters: that message carries no
// chain-wide self-delegation minimum at all, which case (1) re-reads from the SDK type rather than
// asserting from memory. Not in the create-validator field, for the reason above. Only two places
// remain:
//
//	GENESIS   — the `validators[].bonded` amounts of the committed genesis files. This covers the
//	            launch set only, and case (3) holds it.
//	RUNTIME   — a gate on `MsgCreateValidator`, which this application does not have: it uses the
//	            runtime's default ante chain and declares no decorator of its own. Posing the floor for
//	            validators created AFTER genesis therefore requires a custom ante handler in this
//	            package's non-test code. Until that exists, case (3) is a genesis check and nothing
//	            more, and saying otherwise would be the same kind of illusion this file exists to fight.
//
// THE VALUE, AND WHY THIS ONE. The binding constraint is not the attacker, it is recruitment: third
// party operators are the scarce resource, and a floor that keeps them out defeats its own purpose.
// So the floor is squeezed from both sides, against quantities READ FROM the shipped genesis rather
// than copied here — case (4). Below, everything in DNDR:
//
//	supply 10 000 000 · community pocket 3 400 000 · faucet drip 10 · 100 validator slots
//
//	1 000  x 100 slots  = 100 000 = 1 % of the whole supply to fill every seat
//	3 400 000 / 1 000   = 3 400 operators the community pocket alone could seed
//	1 000 / 10          = 100 faucet draws, so a seat is not free-riding distance from the tap
//	1 000 / 1 DNDR      = power 1 000, so the reward share and the two-thirds power measurement of the
//	                      VRF seed are not computed on an integer that truncates to nothing
//
// A thousandfold rise over the accidental floor, and one part in five hundred of the smallest real
// validator currently bonded: it excludes nobody who exists and prices a hundred identities at one
// percent of the supply.

// plancherAutoDelegationUdndr — the floor a validator's own bonded stake must reach: 1 000 DNDR.
const plancherAutoDelegationUdndr uint64 = 1_000 * 1_000_000

// champsParamsStaking — the EXACT field set of `staking.Params` in the SDK line in use. Pinned as a
// SET, not as an absence: "there is no chain-wide floor" is only verified as long as nothing new
// appeared, and a check written around one missing name would stay green for every other one.
var champsParamsStaking = []string{
	"BondDenom", "HistoricalEntries", "MaxEntries", "MaxValidators", "MinCommissionRate", "UnbondingTime",
}

// (1) THE STAKING MODULE OFFERS NO CHAIN-WIDE FLOOR — so none can be posed in a genesis file.
//
// This is the check that stops the floor from being "fixed" in the wrong place. The natural reflex is
// to add `min_self_delegation` under `staking.params` in the genesis yml; the module would ignore an
// unknown key or refuse to boot, and in the first case the network would ship believing it is guarded.
// The day the SDK does introduce such a parameter, this test goes RED — and that red means "pose the
// floor there and drop the ante-handler plan", which is a cheaper answer than the one below.
func TestValidatorFloorHasNoChainParamInStaking(t *testing.T) {
	typ := reflect.TypeOf(stakingtypes.Params{})
	champs := []string{}
	for i := 0; i < typ.NumField(); i++ {
		champs = append(champs, typ.Field(i).Name)
	}
	sort.Strings(champs)

	require.Equal(t, champsParamsStaking, champs,
		"the field set of `staking.Params` has changed: %v.\n"+
			"If a chain-wide self-delegation minimum appeared, THAT is where the validator floor belongs "+
			"— a genesis parameter, enforced by the module for every validator, at genesis and after. "+
			"Update this list and move the floor there. If a field merely disappeared, the genesis files "+
			"may be posting a key the module no longer knows.", champs)
}

// (2) THE ONLY FLOOR CURRENTLY IN FORCE IS `PowerReduction`, AND IT IS AN ACCIDENT.
//
// `ApplyAndReturnValidatorSetUpdates` walks the power index and breaks at the first validator whose
// potential consensus power is zero, so a candidate holding less than one whole unit of power is never
// bonded. That unit is `PowerReduction`, a package-level VARIABLE of the SDK that any init function
// can reassign. Nothing else on this chain refuses a dust validator.
//
// Two directions, and the second is the one that matters: if `PowerReduction` is lowered — the obvious
// move when someone wants finer power granularity — the implicit floor drops with it, silently, and
// the only bar between a dust identity and a validator seat is gone. This test does not forbid the
// change; it forces whoever makes it to see what else it moves.
func TestValidatorFloorImplicitOneIsThePowerReduction(t *testing.T) {
	require.True(t, sdk.DefaultPowerReduction.IsInt64(), "PowerReduction no longer fits in an int64")
	pr := sdk.DefaultPowerReduction.Uint64()

	require.Equal(t, uint64(1_000_000), pr,
		"`sdk.DefaultPowerReduction` is %d udndr instead of 1 000 000. It is not only a display scale: it "+
			"is the IMPLICIT floor to hold a validator seat, since a candidate below one unit of power is "+
			"never bonded. Changing it changes who can validate.", pr)

	// The proposed floor must sit STRICTLY above the accidental one, and by enough that the power
	// arithmetic is not degenerate: a validator worth power 1 rounds to nothing in the proportional
	// reward split and in the two-thirds-of-power measurement of the VRF seed.
	require.GreaterOrEqual(t, plancherAutoDelegationUdndr/pr, uint64(1_000),
		"the proposed floor is worth %d units of consensus power. Below about a thousand, a validator's "+
			"share is computed on an integer small enough to truncate away, which brings back in the "+
			"arithmetic the negligibility the floor is meant to remove.", plancherAutoDelegationUdndr/pr)
}

// (3) THE COMMITTED GENESIS FILES RESPECT THE FLOOR.
//
// This is the only place the floor BITES today. It reads the bonded amounts actually written in the
// files, so it goes red on the file rather than on a copy of it.
func TestShippedGenesisRespectTheValidatorStakeFloor(t *testing.T) {
	for _, nom := range shippedGenesis {
		t.Run(nom, func(t *testing.T) {
			vals := validateursDuGenesis(t, filepath.Join("..", nom))
			require.NotEmpty(t, vals,
				"%s declares no validator: this check no longer measures anything. A genesis with no "+
					"validator does not produce blocks either.", nom)

			for nomVal, bonded := range vals {
				require.GreaterOrEqualf(t, bonded, plancherAutoDelegationUdndr,
					"%s bonds %d udndr to validator %q, below the floor of %d udndr (1 000 DNDR). A genesis "+
						"validator below the floor makes the floor untellable: the first thing a third party "+
						"copies is the shipped file.", nom, bonded, nomVal, plancherAutoDelegationUdndr)
			}
		})
	}
}

// (4) THE FLOOR IS SQUEEZED FROM BOTH SIDES, AGAINST THE GENESIS ECONOMY.
//
// A floor is not a number, it is a ratio to the pockets that exist. Both bounds are computed from the
// shipped genesis, so raising the floor eventually breaks the recruitment bound and lowering it breaks
// the Sybil bound — which is what stops this value from drifting on someone's intuition.
//
// The anti-decoration point, measured rather than hoped for: put the constant back at the accidental
// floor of 1 DNDR and the Sybil bound fails ("100 seats for 100 000 000 udndr, under 1 %"); push it to
// 5 000 DNDR and the recruitment bound fails ("680 operators"); set it to 0 and the package no longer
// compiles, the constant being a divisor. There is no value of the constant that makes this check
// vacuous.
func TestValidatorFloorHoldsBothBounds(t *testing.T) {
	for _, nom := range shippedGenesis {
		t.Run(nom, func(t *testing.T) {
			chemin := filepath.Join("..", nom)
			poches := comptesDuGenesis(t, chemin)
			robinet := robinetDuGenesis(t, chemin)

			communaute, ok := poches["communaute"]
			require.True(t, ok, "%s no longer carries a `communaute` pocket: the recruitment bound below "+
				"is measured against it, and without it this check measures nothing", nom)

			// COST OF A SYBIL — filling every seat must cost a visible fraction of the whole supply.
			// `max_validators` is not posted in the genesis files, so the module default applies.
			coutTousLesSieges := plancherAutoDelegationUdndr * uint64(stakingtypes.DefaultMaxValidators)
			require.GreaterOrEqualf(t, coutTousLesSieges, offreFixeUdndr/100,
				"occupying the %d validator seats costs %d udndr, under 1 %% of the %d udndr supply. At that "+
					"price identities are cheap, and identities are the unit the committee and seed guards "+
					"count in.", stakingtypes.DefaultMaxValidators, coutTousLesSieges, offreFixeUdndr)

			// COST TO AN HONEST OPERATOR — the community pocket alone must be able to seed a large,
			// permissionless operator set. Recruiting third parties is the scarce resource; a floor that
			// prices them out closes the door it was raised to protect.
			require.GreaterOrEqualf(t, communaute/plancherAutoDelegationUdndr, uint64(1_000),
				"the community pocket (%d udndr) covers only %d operators at the floor of %d udndr. A floor "+
					"the community allocation cannot seed a thousand times over is a barrier to entry before "+
					"it is an anti-Sybil measure.", communaute, communaute/plancherAutoDelegationUdndr,
				plancherAutoDelegationUdndr)

			// DISTANCE FROM THE TAP — a seat must not be within casual reach of the faucet, otherwise the
			// floor is paid by patience rather than by capital.
			require.NotZero(t, robinet, "%s no longer declares a faucet amount", nom)
			require.GreaterOrEqualf(t, plancherAutoDelegationUdndr/robinet, uint64(100),
				"the floor is %d faucet draws of %d udndr. A seat that close to the tap costs an attacker "+
					"time, not stake, and the faucet is the one pocket designed to be given away.",
				plancherAutoDelegationUdndr/robinet, robinet)
		})
	}
}

// validateursDuGenesis reads `validators[]` and returns name -> bonded amount in udndr.
func validateursDuGenesis(t *testing.T, chemin string) map[string]uint64 {
	t.Helper()
	brut, err := os.ReadFile(chemin)
	require.NoError(t, err)

	var doc struct {
		Validators []struct {
			Name   string `yaml:"name"`
			Bonded string `yaml:"bonded"`
		} `yaml:"validators"`
	}
	require.NoError(t, yaml.Unmarshal(brut, &doc))

	out := map[string]uint64{}
	for _, v := range doc.Validators {
		montant, denom := decouperCoin(t, v.Bonded)
		require.Equal(t, "udndr", denom, "validator %q: unexpected bonding denomination (%s)", v.Name, denom)
		out[v.Name] = montant
	}
	return out
}

// comptesDuGenesis reads `accounts[]` and returns name -> total udndr held.
func comptesDuGenesis(t *testing.T, chemin string) map[string]uint64 {
	t.Helper()
	brut, err := os.ReadFile(chemin)
	require.NoError(t, err)

	var doc struct {
		Accounts []struct {
			Name  string   `yaml:"name"`
			Coins []string `yaml:"coins"`
		} `yaml:"accounts"`
	}
	require.NoError(t, yaml.Unmarshal(brut, &doc))

	out := map[string]uint64{}
	for _, a := range doc.Accounts {
		for _, c := range a.Coins {
			montant, denom := decouperCoin(t, c)
			require.Equal(t, "udndr", denom, "account %q: unexpected denomination (%s)", a.Name, denom)
			out[a.Name] += montant
		}
	}
	return out
}

// robinetDuGenesis reads the amount a single faucet draw hands out, in udndr.
func robinetDuGenesis(t *testing.T, chemin string) uint64 {
	t.Helper()
	brut, err := os.ReadFile(chemin)
	require.NoError(t, err)

	var doc struct {
		Faucet struct {
			Coins []string `yaml:"coins"`
		} `yaml:"faucet"`
	}
	require.NoError(t, yaml.Unmarshal(brut, &doc))
	require.Len(t, doc.Faucet.Coins, 1, "the faucet no longer hands out exactly one denomination")

	montant, denom := decouperCoin(t, doc.Faucet.Coins[0])
	require.Equal(t, "udndr", denom, "the faucet hands out %s and not udndr", denom)
	return montant
}
