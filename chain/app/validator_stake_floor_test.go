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

// The floor to become a validator: where it is, where it is not, and what it is worth.
//
// Every validator on this chain reports `min_self_delegation: 1` in `query staking validators`. Read
// as a chain rule, that says one micro-DNDR is enough to hold a seat on a rewarded network, an open
// Sybil surface. It is not a chain rule: `min_self_delegation` is supplied by the candidate in its own
// `MsgCreateValidator` and only ever compared with that candidate's stake. It is a promise a validator
// makes to its delegators, and an attacker simply declares 1 as well.
//
// What is in force is an accident. Consensus power is `tokens / PowerReduction` in whole units, and
// the bonding loop stops at the first validator whose potential power is zero. With
// `PowerReduction = 10^6` udndr, anything below 1 DNDR has power 0 and is never bonded. So there is a
// floor of one DNDR out of a ten-million supply, written nowhere, a side effect of a constant chosen
// for power granularity. Lowering that constant for finer granularity, a plausible change, would
// remove the floor without any line of the repository mentioning it. Case (2) makes that visible.
//
// Dust validators do not capture the rewarded stream. The security slice is sent to `fee_collector`
// and distributed by `x/distribution` in proportion to voting power, with no per-head term and no
// proposer bonus in this SDK line: a hundred validators holding one DNDR each collect what one
// validator holding a hundred DNDR collects. The reason to raise the floor is that identities are the
// unit several other guards count in, and identities must not be cheap.
//
// Where a real floor can be set. The staking genesis parameters carry no chain-wide self-delegation
// minimum (case (1) reads this from the SDK type), and the create-validator field is the candidate's
// own choice. Two places remain:
//
//	Genesis:  the `validators[].bonded` amounts of the committed genesis files. This covers the
//	          launch set only, and case (3) holds it.
//	Runtime:  a gate on `MsgCreateValidator`, which this application does not have: it uses the
//	          runtime's default ante chain and declares no decorator of its own. A floor for
//	          validators created after genesis needs a custom ante handler in this package's
//	          non-test code. Until then, case (3) is a genesis check and nothing more.
//
// The value. The binding constraint is recruitment: third-party operators are the scarce resource,
// and a floor that keeps them out defeats its purpose. The floor is therefore bounded from both sides,
// against quantities read from the shipped genesis (case (4)). In DNDR:
//
//	supply 10 000 000 · community pocket 3 400 000 · faucet drip 10 · 100 validator slots
//
//	1 000  x 100 slots  = 100 000 = 1 % of the whole supply to fill every seat
//	3 400 000 / 1 000   = 3 400 operators the community pocket alone could seed
//	1 000 / 10          = 100 faucet draws, so a seat is far from a free ride on the tap
//	1 000 / 1 DNDR      = power 1 000, so the reward share and the two-thirds power measurement of the
//	                      VRF seed are not computed on an integer that truncates to nothing
//
// That is a thousandfold rise over the accidental floor, and one five-hundredth of the smallest real
// validator bonded when the floor was chosen: it excludes nobody who existed and prices a hundred
// identities at one percent of the supply.

// selfDelegationFloorUdndr is the floor a validator's own bonded stake must reach: 1 000 DNDR.
const selfDelegationFloorUdndr uint64 = 1_000 * 1_000_000

// stakingParamsFields is the exact field set of `staking.Params` in the SDK line in use. It is pinned
// as a set: "there is no chain-wide floor" only holds while nothing new appears, and a check written
// around one missing name would stay green for every other one.
var stakingParamsFields = []string{
	"BondDenom", "HistoricalEntries", "MaxEntries", "MaxValidators", "MinCommissionRate", "UnbondingTime",
}

// (1) The staking module offers no chain-wide floor, so none can be set in a genesis file.
//
// This check stops the floor from being "fixed" in the wrong place. The natural reflex is to add
// `min_self_delegation` under `staking.params` in the genesis file; the module would ignore an unknown
// key or refuse to boot, and in the first case the network would ship believing it is guarded. If the
// SDK ever introduces such a parameter, this test goes red, meaning: set the floor there and drop the
// ante-handler plan, which is cheaper.
func TestValidatorFloorHasNoChainParamInStaking(t *testing.T) {
	typ := reflect.TypeOf(stakingtypes.Params{})
	fields := []string{}
	for i := 0; i < typ.NumField(); i++ {
		fields = append(fields, typ.Field(i).Name)
	}
	sort.Strings(fields)

	require.Equal(t, stakingParamsFields, fields,
		"the field set of `staking.Params` has changed: %v.\n"+
			"If a chain-wide self-delegation minimum appeared, that is where the validator floor belongs "+
			"— a genesis parameter, enforced by the module for every validator, at genesis and after. "+
			"Update this list and move the floor there. If a field merely disappeared, the genesis files "+
			"may be posting a key the module no longer knows.", fields)
}

// (2) The only floor currently in force is `PowerReduction`, and it is an accident.
//
// `ApplyAndReturnValidatorSetUpdates` walks the power index and stops at the first validator whose
// potential consensus power is zero, so a candidate holding less than one whole unit of power is never
// bonded. That unit is `PowerReduction`, a package-level variable of the SDK that any init function
// can reassign. Nothing else on this chain refuses a dust validator.
//
// If `PowerReduction` is lowered, the obvious move for finer power granularity, the implicit floor
// drops with it, silently, and nothing stands between a dust identity and a validator seat. This test
// lets the change happen, and shows whoever makes it what else it moves.
func TestValidatorFloorImplicitOneIsThePowerReduction(t *testing.T) {
	require.True(t, sdk.DefaultPowerReduction.IsInt64(), "PowerReduction no longer fits in an int64")
	pr := sdk.DefaultPowerReduction.Uint64()

	require.Equal(t, uint64(1_000_000), pr,
		"`sdk.DefaultPowerReduction` is %d udndr instead of 1 000 000. Beyond the display scale, it "+
			"is the implicit floor to hold a validator seat, since a candidate below one unit of power is "+
			"never bonded. Changing it changes who can validate.", pr)

	// The proposed floor must sit strictly above the accidental one, and far enough that the power
	// arithmetic is not degenerate: a validator worth power 1 rounds to nothing in the proportional
	// reward split and in the two-thirds power measurement of the VRF seed.
	require.GreaterOrEqual(t, selfDelegationFloorUdndr/pr, uint64(1_000),
		"the proposed floor is worth %d units of consensus power. Below about a thousand, a validator's "+
			"share is computed on an integer small enough to truncate away, which brings back in the "+
			"arithmetic the negligibility the floor is meant to remove.", selfDelegationFloorUdndr/pr)
}

// (3) The committed genesis files respect the floor.
//
// This is the only place the floor applies today. It reads the bonded amounts written in the files,
// so it goes red on the file itself.
func TestShippedGenesisRespectTheValidatorStakeFloor(t *testing.T) {
	for _, name := range shippedGenesis {
		t.Run(name, func(t *testing.T) {
			vals := genesisValidators(t, filepath.Join("..", name))
			require.NotEmpty(t, vals,
				"%s declares no validator: this check no longer measures anything. A genesis with no "+
					"validator does not produce blocks either.", name)

			for valName, bonded := range vals {
				require.GreaterOrEqualf(t, bonded, selfDelegationFloorUdndr,
					"%s bonds %d udndr to validator %q, below the floor of %d udndr (1 000 DNDR). A genesis "+
						"validator below the floor makes the floor untellable: the first thing a third party "+
						"copies is the shipped file.", name, bonded, valName, selfDelegationFloorUdndr)
			}
		})
	}
}

// (4) The floor is bounded from both sides, against the genesis economy.
//
// A floor is a ratio to the pockets that exist. Both bounds are computed from the shipped genesis, so
// raising the floor eventually breaks the recruitment bound and lowering it breaks the Sybil bound,
// which keeps the value from drifting on intuition.
//
// Measured: put the constant back at the accidental floor of 1 DNDR and the Sybil bound fails ("100
// seats for 100 000 000 udndr, under 1 %"); push it to 5 000 DNDR and the recruitment bound fails
// ("680 operators"); set it to 0 and the package no longer compiles, since the constant is a divisor.
// No value of the constant makes this check vacuous.
func TestValidatorFloorHoldsBothBounds(t *testing.T) {
	for _, name := range shippedGenesis {
		t.Run(name, func(t *testing.T) {
			path := filepath.Join("..", name)
			pockets := genesisAccounts(t, path)
			faucet := genesisFaucet(t, path)

			community, ok := pockets["community"]
			require.True(t, ok, "%s no longer carries a `community` pocket: the recruitment bound below "+
				"is measured against it, and without it this check measures nothing", name)

			// Cost of a Sybil: filling every seat must cost a visible fraction of the whole supply.
			// `max_validators` is not set in the genesis files, so the module default applies.
			costOfAllSeats := selfDelegationFloorUdndr * uint64(stakingtypes.DefaultMaxValidators)
			require.GreaterOrEqualf(t, costOfAllSeats, fixedSupplyUdndr/100,
				"occupying the %d validator seats costs %d udndr, under 1 %% of the %d udndr supply. At that "+
					"price identities are cheap, and identities are the unit the committee and seed guards "+
					"count in.", stakingtypes.DefaultMaxValidators, costOfAllSeats, fixedSupplyUdndr)

			// Cost to an honest operator: the community pocket alone must be able to seed a large,
			// permissionless operator set. Third-party operators are the scarce resource, and a floor that
			// prices them out closes the door it was raised to protect.
			require.GreaterOrEqualf(t, community/selfDelegationFloorUdndr, uint64(1_000),
				"the community pocket (%d udndr) covers only %d operators at the floor of %d udndr. A floor "+
					"the community allocation cannot seed a thousand times over is a barrier to entry before "+
					"it is an anti-Sybil measure.", community, community/selfDelegationFloorUdndr,
				selfDelegationFloorUdndr)

			// Distance from the tap: a seat must not be within casual reach of the faucet, otherwise the
			// floor is paid with patience instead of capital.
			require.NotZero(t, faucet, "%s no longer declares a faucet amount", name)
			require.GreaterOrEqualf(t, selfDelegationFloorUdndr/faucet, uint64(100),
				"the floor is %d faucet draws of %d udndr. A seat that close to the tap costs an attacker "+
					"time, not stake, and the faucet is the one pocket designed to be given away.",
				selfDelegationFloorUdndr/faucet, faucet)
		})
	}
}

// genesisValidators reads `validators[]` and returns name -> bonded amount in udndr.
func genesisValidators(t *testing.T, path string) map[string]uint64 {
	t.Helper()
	raw, err := os.ReadFile(path)
	require.NoError(t, err)

	var doc struct {
		Validators []struct {
			Name   string `yaml:"name"`
			Bonded string `yaml:"bonded"`
		} `yaml:"validators"`
	}
	require.NoError(t, yaml.Unmarshal(raw, &doc))

	out := map[string]uint64{}
	for _, v := range doc.Validators {
		amount, denom := splitCoin(t, v.Bonded)
		require.Equal(t, "udndr", denom, "validator %q: unexpected bonding denomination (%s)", v.Name, denom)
		out[v.Name] = amount
	}
	return out
}

// genesisAccounts reads `accounts[]` and returns name -> total udndr held.
func genesisAccounts(t *testing.T, path string) map[string]uint64 {
	t.Helper()
	raw, err := os.ReadFile(path)
	require.NoError(t, err)

	var doc struct {
		Accounts []struct {
			Name  string   `yaml:"name"`
			Coins []string `yaml:"coins"`
		} `yaml:"accounts"`
	}
	require.NoError(t, yaml.Unmarshal(raw, &doc))

	out := map[string]uint64{}
	for _, a := range doc.Accounts {
		for _, c := range a.Coins {
			amount, denom := splitCoin(t, c)
			require.Equal(t, "udndr", denom, "account %q: unexpected denomination (%s)", a.Name, denom)
			out[a.Name] += amount
		}
	}
	return out
}

// genesisFaucet reads the amount a single faucet draw hands out, in udndr.
func genesisFaucet(t *testing.T, path string) uint64 {
	t.Helper()
	raw, err := os.ReadFile(path)
	require.NoError(t, err)

	var doc struct {
		Faucet struct {
			Coins []string `yaml:"coins"`
		} `yaml:"faucet"`
	}
	require.NoError(t, yaml.Unmarshal(raw, &doc))
	require.Len(t, doc.Faucet.Coins, 1, "the faucet no longer hands out exactly one denomination")

	amount, denom := splitCoin(t, doc.Faucet.Coins[0])
	require.Equal(t, "udndr", denom, "the faucet hands out %s and not udndr", denom)
	return amount
}
