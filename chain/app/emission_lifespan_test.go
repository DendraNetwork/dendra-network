package app

import (
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"strconv"
	"strings"
	"testing"

	"github.com/stretchr/testify/require"
	"gopkg.in/yaml.v3"

	"github.com/DendraNetwork/dendra-network/chain/x/chaintime"
	emissionkeeper "github.com/DendraNetwork/dendra-network/chain/x/emission/keeper"
	emissiontypes "github.com/DendraNetwork/dendra-network/chain/x/emission/types"
)

// The shipped genesis must give the Reserve a long enough life, and that duration is what this file
// pins.
//
// It does not pin the numbers. `epoch_blocks` and `reserve_release_bps` are two integers in a YAML
// file, and a check such as `require.Equal(86400, epochBlocks)` only copies the file: it stays green
// when someone edits both together, which is the change that breaks the economy. What matters is the
// duration the two produce together, so that is what this file measures.
//
// `EpochRelease` releases `reserve_release_bps` of what remains, every epoch. A geometric decay never
// reaches zero, so "when is the Reserve empty" has no answer, while "when has it halved" has exactly
// one, independent of the starting amount. The half-life is therefore the unit of this calibration,
// and the only one that survives a change of allocation.
//
// There are two regimes. The demand gate caps the work stream at `work_gate_bps` times the
// non-recoverable demand, and whatever the cap holds back stays in the Reserve. The same parameters
// therefore produce two lifetimes, and both must be bounded:
//
//	idle chain      -> work = 0        -> half the nominal slice leaves  -> the longest lifetime
//	saturated gate  -> work = workPool -> the whole slice leaves         -> the shortest lifetime
//
// Measuring only the idle regime would certify a chain that halves twice as fast once it gets the
// usage it was built for.
//
// The half-life in epochs is a fact; in years it is a hypothesis. The number of epochs it takes to
// halve the Reserve depends only on the release rate (2 bps: about 6932 epochs idle, about 3466
// saturated), whatever a block costs in seconds. Turning it into years needs the real block interval,
// which nothing in this repository can enforce: `x/chaintime` declares a target, and CometBFT derives
// the actual value from the validators' `timeout_commit` and network propagation.
//
// A green result here is therefore conditional, and the file says so. If the real interval is N times
// the declared one, every year printed here is N times too small and the tests stay green. The band
// is named as a band under a declared hypothesis, every failure message restates the hypothesis, and
// the reader is sent to the chain to measure the interval. Querying a node from a unit test would be
// worse: the test would go red whenever the network has a hiccup, and pin nothing when no node
// answers.

// Announced lifetime band for the Reserve, in years under the declared block-interval hypothesis
// (`chaintime.TargetBlockSeconds`), across both demand regimes.
//
// The floor is the real guard: below it the Reserve behaves like a devnet observability setting. The
// setting these bounds replaced (a 300-block epoch) halved the Reserve in about 3466 to 6932
// five-minute epochs under the same hypothesis, that is 12 to 24 days: the whole reward budget would
// evaporate during the testnet. `TestEmissionLifespanWitnessShortSettingUnderTheAssumption`
// re-measures that setting to show the floor refuses it. The witness also survives a wrong hypothesis
// by a wide margin: at five times the declared interval, the same setting still halves the Reserve in
// 60 to 120 days, two orders of magnitude below the floor. The refusal does not depend on the
// assumption being exact; only the announced band does.
//
// The ceiling matters too: an emission stretched over centuries is inert, pays no validator and
// secures nothing. It also catches the opposite typo (an epoch length off by a factor of a thousand),
// which would otherwise look like prudence.
//
// Widening this band is a legitimate decision, to be made openly, on numbers, with the table of bps
// per epoch length in hand. That table lives in docs/TOKENOMICS-CALIBRATION.md in the development
// repository and is not part of a published tree; from a published clone, the numbers to decide on
// are the ones this test prints.
const (
	halfLifeMinYearsUnderAssumption = 5.0
	halfLifeMaxYearsUnderAssumption = 25.0
)

// iterationBound caps the simulation, so that a setting that emits nothing measurable fails with a
// named error instead of hanging the test.
const iterationBound = 20_000_000

// assumption states, in one sentence reused by every failure message, what the year figures of this
// file depend on. It is declared from the constant and never measured at run time: a unit test that
// queried a node would go red on a network hiccup and pin nothing when no node answers. Written down,
// the assumption can be checked by hand against the chain.
func assumption() string {
	return fmt.Sprintf(
		"[hypothesis] years here assume chaintime.TargetBlockSeconds = %d s per block. That is a "+
			"declared target, not a consensus rule: CometBFT derives the real interval from validator "+
			"timeout_commit and network propagation, so no value in this repository can enforce it. If "+
			"the real interval is N times longer, every duration below is N times longer, while the "+
			"half-life in epochs is unchanged (it depends only on reserve_release_bps). Measure the real "+
			"interval on the chain (two block headers from the /blockchain endpoint) before quoting any "+
			"year figure outside this file.",
		chaintime.TargetBlockSeconds)
}

// TestEmissionLifespanOfShippedGenesisUnderTheAssumption is the main check. Its suffix matters: the
// quantity it bounds is a duration, and a duration only exists here under the block interval declared
// by x/chaintime.
func TestEmissionLifespanOfShippedGenesisUnderTheAssumption(t *testing.T) {
	for _, name := range shippedGenesis {
		t.Run(name, func(t *testing.T) {
			path := filepath.Join("..", name)
			p := genesisEmissionParams(t, path)
			reserve := genesisReserve(t, path)

			require.NoError(t, p.Validate(),
				"%s ships emission parameters that `Params.Validate()` refuses: `InitGenesis` calls it, so "+
					"a chain started from this file would not boot", name)

			for _, regime := range []struct {
				name      string
				saturated bool
			}{{"zero_demand", false}, {"saturated_demand", true}} {
				epochs := halfLifeInEpochs(t, reserve, p, regime.saturated)
				years := epochsToYearsUnderAssumption(epochs, p.EpochBlocks)

				require.GreaterOrEqualf(t, years, halfLifeMinYearsUnderAssumption,
					"%s / %s: the Reserve halves in %d epochs of %d blocks = %.2f years, below the announced "+
						"floor of %.0f years. Whatever changed (the release rate, the epoch length, the splits), "+
						"the reward budget of the chain now evaporates within its own lifetime. Re-arbitrate on "+
						"the bps-per-epoch-length table (docs/TOKENOMICS-CALIBRATION.md, which lives in "+
						"the development repository and not in a published tree), then move this "+
						"bound deliberately.\n%s",
					name, regime.name, epochs, p.EpochBlocks, years, halfLifeMinYearsUnderAssumption, assumption())

				require.LessOrEqualf(t, years, halfLifeMaxYearsUnderAssumption,
					"%s / %s: the Reserve halves in %d epochs of %d blocks = %.2f years, above the announced "+
						"ceiling of %.0f years. An emission stretched that thin pays nobody; check the epoch "+
						"length for a factor-of-a-thousand typo before widening this bound.\n%s",
					name, regime.name, epochs, p.EpochBlocks, years, halfLifeMaxYearsUnderAssumption, assumption())

				// The epoch count first, since it holds whatever the block interval turns out to be; the
				// years second, tagged with the assumption that produced them.
				t.Logf("%s / %s: half-life %d epochs (block-interval independent) = %.2f years at %d s/block (assumed)",
					name, regime.name, epochs, years, chaintime.TargetBlockSeconds)
			}
		})
	}
}

// TestEmissionLifespanDemandAcceleratesTheCurve checks the ordering between the two regimes, which is
// a property of the demand gate: what the gate holds back stays in the Reserve, so real demand can only
// shorten its life. An inversion would mean the gate stopped gating, which no lifetime bound alone
// would reveal.
func TestEmissionLifespanDemandAcceleratesTheCurve(t *testing.T) {
	for _, name := range shippedGenesis {
		path := filepath.Join("..", name)
		p := genesisEmissionParams(t, path)
		reserve := genesisReserve(t, path)

		idle := halfLifeInEpochs(t, reserve, p, false)
		saturated := halfLifeInEpochs(t, reserve, p, true)
		require.Lessf(t, saturated, idle,
			"%s: saturated demand (%d epochs) should drain the Reserve strictly faster than an idle chain "+
				"(%d epochs). If they are equal, the work stream is emitting regardless of demand — the "+
				"anti-self-dealing demand gate is no longer gating anything.", name, saturated, idle)
	}
}

// TestEmissionLifespanWitnessShortSettingUnderTheAssumption is the witness: a band is only worth what
// it refuses. It re-measures the devnet setting the shipped genesis moved away from (300-block epoch,
// same release rate) and requires it to fall outside the band. Without it, an assertion that accepts
// everything would look the same as one that protects the economy.
//
// It carries the same suffix for the same reason, but its verdict holds by a far wider margin than the band's:
// the setting is refused by a factor of at least seventy-five at the declared interval, and still by a
// factor of at least fifteen at five times that interval. A wrong hypothesis would change the number
// this test prints and leave its verdict intact.
func TestEmissionLifespanWitnessShortSettingUnderTheAssumption(t *testing.T) {
	p := genesisEmissionParams(t, filepath.Join("..", "config.optimistic.yml"))
	p.EpochBlocks = 300 // the observability setting; everything else is identical

	for _, saturated := range []bool{false, true} {
		epochs := halfLifeInEpochs(t, emissionkeeper.GenesisReserveU, p, saturated)
		years := epochsToYearsUnderAssumption(epochs, p.EpochBlocks)
		require.Less(t, years, halfLifeMinYearsUnderAssumption,
			"a 300-block epoch halves the Reserve in %d epochs = %.3f years; the floor of %.0f years is "+
				"supposed to refuse it. If this passes, the band accepts a setting that drains the Reserve in "+
				"weeks and the main check is decoration.\n%s",
			epochs, years, halfLifeMinYearsUnderAssumption, assumption())
	}
}

// TestEmissionLifespanIsNotInertUnderTheAssumption covers the opposite failure. Integer truncation
// (`x * bps / 10000`) turns a small enough rate into a release of exactly zero, forever: the Reserve
// then lasts forever, which a lifetime floor alone would accept. Every stream must move on the first
// epoch, and a year's worth of epochs must leave the Reserve in the announced range.
//
// The zero-release half is unconditional. The one-year half is not: the epoch count it walks is
// derived from the same declared interval, so it is a claim about that many epochs, and the word
// "year" carries the hypothesis.
func TestEmissionLifespanIsNotInertUnderTheAssumption(t *testing.T) {
	for _, name := range shippedGenesis {
		t.Run(name, func(t *testing.T) {
			path := filepath.Join("..", name)
			p := genesisEmissionParams(t, path)
			reserve := genesisReserve(t, path)

			r := emissionkeeper.EpochRelease(reserve, 0, keeperParams(p))
			require.NotZero(t, r.Released, "%s: the first epoch releases nothing at all (rate truncated to zero)", name)
			require.NotZero(t, r.Security, "%s: the security stream is zero on the first epoch: no validator is paid", name)
			require.NotZero(t, r.Avail, "%s: the availability stream is zero on the first epoch", name)

			// A year's worth of epochs under the declared interval, in both regimes: the Reserve must still
			// be mostly there, and must have moved.
			epochsPerYear := chaintime.Year / (p.EpochBlocks * chaintime.TargetBlockSeconds)
			require.NotZerof(t, epochsPerYear,
				"%s: an epoch lasts more than a year; that is a different economy.\n%s", name, assumption())
			for _, saturated := range []bool{false, true} {
				remaining := reserve
				for i := uint64(0); i < epochsPerYear; i++ {
					remaining = emissionkeeper.EpochRelease(remaining, demand(remaining, saturated), keeperParams(p)).NewReserve
				}
				part := float64(remaining) / float64(reserve)
				require.Greaterf(t, part, 0.80,
					"%s (saturated=%v): only %.1f %% of the Reserve is left after %d epochs.\n%s",
					name, saturated, part*100, epochsPerYear, assumption())
				require.Lessf(t, part, 1.0,
					"%s (saturated=%v): the Reserve did not move in %d epochs.\n%s",
					name, saturated, epochsPerYear, assumption())
				t.Logf("%s (saturated=%v): %.2f %% of the Reserve left after %d epochs (one year at %d s/block, assumed)",
					name, saturated, part*100, epochsPerYear, chaintime.TargetBlockSeconds)
			}
		})
	}
}

// TestEmissionLifespanAvailabilityCadenceIsIndependent: `emission.epoch_blocks` and
// `jobs.avail_epoch_blocks` look like the same cadence but live in two modules, and only the first was
// slowed down. Availability challenges must keep their own fast rhythm: they are a liveness probe, and
// probing once a day would let a miner be absent for hours at no cost. `x/jobs` reads
// `params.AvailEpochBlocks` and never the emission parameters (its only link to x/emission is the
// AvailPool balance, through PayAvail), so the independence is structural; this check keeps a
// well-meaning genesis edit from unifying the two.
func TestEmissionLifespanAvailabilityCadenceIsIndependent(t *testing.T) {
	for _, name := range shippedGenesis {
		path := filepath.Join("..", name)
		emission := genesisEmissionParams(t, path)
		jobs := genesisJobsParams(t, path)

		require.NotZero(t, jobs.AvailEpochBlocks, "%s: availability is disabled (avail_epoch_blocks=0)", name)
		require.Lessf(t, jobs.AvailEpochBlocks*20, emission.EpochBlocks,
			"%s: the availability cadence (%d blocks) is no longer far below the emission cadence (%d blocks). "+
				"These are two parameters of two modules; the release of the Reserve was slowed to a daily "+
				"rhythm on purpose, the liveness probe was not. If they have converged, availability is now "+
				"checked at the pace of a monetary epoch and an absent miner goes unnoticed for that long.",
			name, jobs.AvailEpochBlocks, emission.EpochBlocks)
	}
}

// Helpers.

// demand returns the non-recoverable demand fed to the gate for a regime. The saturated value is the
// remaining Reserve, which is far above the cap (`work_gate_bps` x demand against half a slice) and
// small enough that `mulBps` cannot overflow: `Params.Validate` bounds work_gate_bps at 100000, and
// 10^13 x 10^5 stays 18 times under 2^64.
func demand(reserve uint64, saturated bool) uint64 {
	if !saturated {
		return 0
	}
	return reserve
}

func keeperParams(p emissiontypes.Params) emissionkeeper.Params {
	return emissionkeeper.Params{
		ReserveReleaseBps: p.ReserveReleaseBps,
		WorkSplitBps:      p.WorkSplitBps,
		AvailSplitBps:     p.AvailSplitBps,
		WorkGateBps:       p.WorkGateBps,
	}
}

// halfLifeInEpochs replays EpochRelease until the Reserve has halved, and returns the number of
// epochs. Same integer arithmetic as the chain, so this measures the shipped curve rather than a
// closed-form approximation of it.
func halfLifeInEpochs(t *testing.T, reserve uint64, p emissiontypes.Params, saturated bool) uint64 {
	t.Helper()
	kp := keeperParams(p)
	target := reserve / 2
	remaining := reserve
	for e := uint64(1); e <= iterationBound; e++ {
		r := emissionkeeper.EpochRelease(remaining, demand(remaining, saturated), kp)
		require.NotZerof(t, r.Released,
			"the release truncated to zero at epoch %d with %d udndr left: emission has stopped, and a "+
				"half-life measured on a curve that no longer moves is infinite, not long", e, remaining)
		remaining = r.NewReserve
		if remaining <= target {
			return e
		}
	}
	t.Fatalf("the Reserve has not halved after %d epochs: the release rate is effectively nil", iterationBound)
	return 0
}

// epochsToYearsUnderAssumption converts through x/chaintime, so the meaning of a block count follows
// the one place where the target block interval is declared. The name says "UnderAssumption" because
// the output is the duration implied by a constant, which the network does not guarantee; every caller
// hands the result to a message that repeats the assumption.
func epochsToYearsUnderAssumption(epochs, epochBlocks uint64) float64 {
	seconds := float64(epochs) * float64(epochBlocks) * float64(chaintime.TargetBlockSeconds)
	return seconds / float64(chaintime.Year)
}

// genesisReserve reads the coins of the `emission_reserve` account, the pocket x/emission releases
// from. Reading it, rather than assuming it, means a change of allocation gets measured.
func genesisReserve(t *testing.T, path string) uint64 {
	t.Helper()
	raw, err := os.ReadFile(path)
	require.NoError(t, err)

	var doc struct {
		Accounts []struct {
			Name       string   `yaml:"name"`
			ModuleName string   `yaml:"module_name"`
			Coins      []string `yaml:"coins"`
		} `yaml:"accounts"`
	}
	require.NoError(t, yaml.Unmarshal(raw, &doc))

	for _, a := range doc.Accounts {
		if a.ModuleName != emissiontypes.ModuleName {
			continue
		}
		require.Len(t, a.Coins, 1, "%s: the emission module account holds several denoms", filepath.Base(path))
		amount, denom := splitCoin(t, a.Coins[0])
		require.Equal(t, "udndr", denom)
		require.Equalf(t, emissionkeeper.GenesisReserveU, amount,
			"%s funds the emission module account with %d udndr while the keeper falls back to %d when "+
				"genesis is silent. Two sources of truth for the size of the Reserve.",
			filepath.Base(path), amount, emissionkeeper.GenesisReserveU)
		return amount
	}
	t.Fatalf("%s declares no account with module_name: %s -> the Reserve is not funded at genesis",
		filepath.Base(path), emissiontypes.ModuleName)
	return 0
}

// genesisEmissionParams reads `genesis.app_state.emission.params` over the defaults, exactly as a
// partial genesis loads. Twin of genesisJobsParams (genesis_shipped_test.go); the reflective walk is
// what makes an unknown key a failure instead of a silently ignored line.
func genesisEmissionParams(t *testing.T, path string) emissiontypes.Params {
	t.Helper()
	raw, err := os.ReadFile(path)
	require.NoError(t, err)

	var doc struct {
		Genesis struct {
			AppState struct {
				Emission struct {
					Params map[string]any `yaml:"params"`
				} `yaml:"emission"`
			} `yaml:"app_state"`
		} `yaml:"genesis"`
	}
	require.NoError(t, yaml.Unmarshal(raw, &doc))
	require.NotEmpty(t, doc.Genesis.AppState.Emission.Params,
		"%s sets no emission parameter: the chain would fall back to a curve nobody chose here",
		filepath.Base(path))

	p := emissiontypes.DefaultParams()
	rv := reflect.ValueOf(&p).Elem()
	for key, value := range doc.Genesis.AppState.Emission.Params {
		field := rv.FieldByName(camelFromSnake(key))
		require.Truef(t, field.IsValid() && field.CanSet(),
			"%s sets `emission.params.%s`, which matches no field of Params: the genesis configures a "+
				"parameter that does not exist, and nobody reads it.", filepath.Base(path), key)
		require.Equalf(t, reflect.Uint64, field.Kind(), "unhandled type for %s: %s", key, field.Kind())
		n, err := strconv.ParseUint(strings.TrimSpace(toString(value)), 10, 64)
		require.NoErrorf(t, err, "%s: non-integer value for %s (%v)", filepath.Base(path), key, value)
		field.SetUint(n)
	}
	return p
}
