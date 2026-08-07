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

// ═══ THE SHIPPED GENESIS MUST BUY THE RESERVE A LIFETIME, AND THAT IS WHAT IS PINNED ═════════════
//
// WHAT THIS FILE PROTECTS, AND WHY IT DOES NOT PIN THE NUMBERS. `epoch_blocks` and
// `reserve_release_bps` are two integers in a YAML file. A check written as
// `require.Equal(86400, epochBlocks)` protects nothing: it is a copy of the file, so it goes green on
// whatever the file says the moment someone edits both together — which is exactly the gesture that
// breaks the economy. The property that matters is not the value of either integer, it is the
// DURATION they produce together, and a duration is what this file measures.
//
// THE SHAPE OF THE CURVE. `EpochRelease` releases `reserve_release_bps` of what REMAINS, every epoch.
// A geometric decay never reaches zero, so "when is the Reserve empty" has no answer; "when has it
// halved" has exactly one, and it is independent of the starting amount. The half-life is therefore
// the natural unit of this calibration, and the only one that survives a change of allocation.
//
// TWO REGIMES, NOT ONE. The demand gate (ADR-017) caps the WORK stream at `work_gate_bps` times the
// non-recoverable demand, and whatever the cap holds back STAYS in the Reserve. So the same parameters
// produce two different lifetimes, and only bounding BOTH bounds the economy:
//
//	idle chain      -> work = 0        -> half the nominal slice leaves  -> the LONGEST lifetime
//	saturated gate  -> work = workPool -> the whole slice leaves         -> the SHORTEST lifetime
//
// Measuring only the idle regime would certify a chain that halves twice as fast the day it gets the
// usage it was built for.
//
// THE HALF-LIFE IN EPOCHS IS THE FACT; THE HALF-LIFE IN YEARS IS A HYPOTHESIS. `EpochRelease` counts
// in EPOCHS, and the number of epochs it takes to halve the Reserve depends ONLY on the release rate
// (2 bps -> about 6932 epochs idle, about 3466 saturated). That count is true whatever a block costs
// in seconds. Turning it into YEARS requires one outside quantity nothing in this repository can
// enforce: the real inter-block interval. `x/chaintime` DECLARES a target for it; CometBFT derives the
// actual value from validator `timeout_commit` and network propagation, so the declaration is an
// assumption, not a measurement.
//
// WHICH MAKES THIS FILE'S GREEN A CONDITIONAL GREEN, AND IT SAYS SO. A test that derives its quantity
// from an unmeasured assumption certifies the assumption, not the system: if the real interval is N
// times the declared one, every year printed here is N times too small and this file stays green
// throughout. So the band is NAMED for what it is — a band under a declared hypothesis — the
// hypothesis is restated in every failure message, and the reader is sent to the chain to measure the
// interval. The alternative (querying a node from a unit test) is worse on both counts: it turns a
// pure test red whenever the network coughs, and it pins nothing at all when no node answers.
//
// The remedy is therefore a HUMAN one and it is deliberate: the assumption is written down where it
// can be compared to a measurement, instead of being spread implicitly across every duration.

// Announced lifetime band for the Reserve, in years UNDER THE DECLARED BLOCK-INTERVAL HYPOTHESIS
// (`chaintime.TargetBlockSeconds`), across BOTH demand regimes.
//
// The FLOOR is the real guard: below it the Reserve is a devnet observability setting, not an economy.
// The setting these bounds replaced (a 300-block epoch) halved the Reserve in about 3466 to 6932
// epochs of five minutes each under the same hypothesis — 12 to 24 DAYS, a chain whose whole reward
// budget evaporates during its own testnet. `TestEmissionDureeDeVieTemoinReglageCourtSousHypothese`
// re-measures that setting to prove the floor has teeth. The witness also survives a wrong hypothesis
// by a wide margin: at an interval five times the declared one that same setting still halves the
// Reserve in 60 to 120 days, two orders of magnitude below the floor. The refusal this file exists for
// therefore does not rest on the assumption being exact — only the announced BAND does.
//
// The CEILING is not decoration either: an emission stretched over centuries is inert, and inert
// emission pays no validator and secures nothing. It also catches the opposite typo (an epoch length
// off by a factor of a thousand), which would otherwise look like prudence.
//
// Widening this band is a legitimate decision. Doing it silently is not: the bounds are here so that
// the arbitration happens on numbers, with docs/TOKENOMICS-CALIBRATION.md in hand.
const (
	demiVieMinAnneesSousHypothese = 5.0
	demiVieMaxAnneesSousHypothese = 25.0
)

// borneIterations caps the simulation. It is not a performance guard: it is what turns "this setting
// emits nothing measurable" into a named failure instead of a test that hangs.
const borneIterations = 20_000_000

// hypothese states, in one sentence reused by every failure message, what the year figures of this
// file are conditioned on. It is DECLARED from the constant, never measured at run time: a unit test
// that queried a node would go red on a network hiccup and would pin nothing at all when no node
// answers. Writing the assumption down is what makes it checkable by hand against the chain.
func hypothese() string {
	return fmt.Sprintf(
		"[HYPOTHESIS] years here assume chaintime.TargetBlockSeconds = %d s per block. That is a "+
			"DECLARED target, not a consensus rule: CometBFT derives the real interval from validator "+
			"timeout_commit and network propagation, so no value in this repository can enforce it. If "+
			"the real interval is N times longer, every duration below is N times longer, while the "+
			"half-life IN EPOCHS is unchanged (it depends only on reserve_release_bps). Measure the real "+
			"interval on the chain (two block headers from the /blockchain endpoint) before quoting any "+
			"year figure outside this file.",
		chaintime.TargetBlockSeconds)
}

// TestEmissionDureeDeVieDesGenesisLivresSousHypothese — the load-bearing check. The suffix is not
// decoration: the quantity it bounds is a duration, and a duration only exists here under the block
// interval declared by x/chaintime.
func TestEmissionDureeDeVieDesGenesisLivresSousHypothese(t *testing.T) {
	for _, nom := range genesisLivres {
		t.Run(nom, func(t *testing.T) {
			chemin := filepath.Join("..", nom)
			p := paramsEmissionDuGenesis(t, chemin)
			reserve := reserveDuGenesis(t, chemin)

			require.NoError(t, p.Validate(),
				"%s ships emission parameters that `Params.Validate()` refuses: `InitGenesis` calls it, so "+
					"a chain started from this file would not boot", nom)

			for _, regime := range []struct {
				nom     string
				saturee bool
			}{{"demande_nulle", false}, {"demande_saturee", true}} {
				epoques := demiVieEnEpoques(t, reserve, p, regime.saturee)
				annees := epoquesEnAnneesSousHypothese(epoques, p.EpochBlocks)

				require.GreaterOrEqualf(t, annees, demiVieMinAnneesSousHypothese,
					"%s / %s: the Reserve halves in %d epochs of %d blocks = %.2f years, below the announced "+
						"floor of %.0f years. Whatever changed — the release rate, the epoch length, the splits — "+
						"the reward budget of the chain now evaporates within its own lifetime. Re-arbitrate on "+
						"docs/TOKENOMICS-CALIBRATION.md, then move this bound deliberately.\n%s",
					nom, regime.nom, epoques, p.EpochBlocks, annees, demiVieMinAnneesSousHypothese, hypothese())

				require.LessOrEqualf(t, annees, demiVieMaxAnneesSousHypothese,
					"%s / %s: the Reserve halves in %d epochs of %d blocks = %.2f years, above the announced "+
						"ceiling of %.0f years. An emission stretched that thin pays nobody; check the epoch "+
						"length for a factor-of-a-thousand typo before widening this bound.\n%s",
					nom, regime.nom, epoques, p.EpochBlocks, annees, demiVieMaxAnneesSousHypothese, hypothese())

				// The epoch count first, because it is the part that holds whatever the block interval
				// turns out to be; the years second, tagged with the assumption that produced them.
				t.Logf("%s / %s: half-life %d epochs (block-interval independent) = %.2f years at %d s/block (assumed)",
					nom, regime.nom, epoques, annees, chaintime.TargetBlockSeconds)
			}
		})
	}
}

// TestEmissionDureeDeVieLaDemandeAccelereLaCourbe — the ordering between the two regimes is a
// PROPERTY of the demand gate, not an accident of the current numbers: what the gate holds back stays
// in the Reserve, so real demand can only shorten its life. An inversion here would mean the gate has
// stopped gating, which no lifetime bound alone would reveal.
func TestEmissionDureeDeVieLaDemandeAccelereLaCourbe(t *testing.T) {
	for _, nom := range genesisLivres {
		chemin := filepath.Join("..", nom)
		p := paramsEmissionDuGenesis(t, chemin)
		reserve := reserveDuGenesis(t, chemin)

		vide := demiVieEnEpoques(t, reserve, p, false)
		saturee := demiVieEnEpoques(t, reserve, p, true)
		require.Lessf(t, saturee, vide,
			"%s: saturated demand (%d epochs) should drain the Reserve STRICTLY faster than an idle chain "+
				"(%d epochs). If they are equal, the work stream is emitting regardless of demand — the "+
				"anti-self-dealing gate of ADR-017 is no longer gating anything.", nom, saturee, vide)
	}
}

// TestEmissionDureeDeVieTemoinReglageCourtSousHypothese — THE WITNESS. A band is only worth what it
// refuses. This case re-measures the devnet setting the shipped genesis moved away from (300-block
// epoch, same release rate) and requires it to be OUTSIDE the band. Without it, an assertion that
// happens to accept everything would look identical to one that protects the economy.
//
// It carries the same suffix for the same reason, but its verdict is far more robust than the band's:
// the setting is refused by a factor of at least seventy-five at the declared interval, and still by a
// factor of at least fifteen at five times that interval. A wrong hypothesis moves this test's NUMBER,
// not its answer.
func TestEmissionDureeDeVieTemoinReglageCourtSousHypothese(t *testing.T) {
	p := paramsEmissionDuGenesis(t, filepath.Join("..", "config.optimistic.yml"))
	p.EpochBlocks = 300 // the observability setting: everything else identical

	for _, saturee := range []bool{false, true} {
		epoques := demiVieEnEpoques(t, emissionkeeper.GenesisReserveU, p, saturee)
		annees := epoquesEnAnneesSousHypothese(epoques, p.EpochBlocks)
		require.Less(t, annees, demiVieMinAnneesSousHypothese,
			"a 300-block epoch halves the Reserve in %d epochs = %.3f years; the floor of %.0f years is "+
				"supposed to refuse it. If this passes, the band accepts a setting that drains the Reserve in "+
				"weeks and the main check is decoration.\n%s",
			epoques, annees, demiVieMinAnneesSousHypothese, hypothese())
	}
}

// TestEmissionDureeDeVieNEstPasInerteSousHypothese — the mirror failure. Integer truncation
// (`x * bps / 10000`) turns a small enough rate into a release of exactly ZERO, forever: the Reserve
// then lasts an infinite time, which a lifetime FLOOR alone would happily certify. Every stream must
// actually move on the first epoch, and a year's worth of epochs must leave the Reserve in the
// announced range.
//
// The zero-release half is unconditional. The "after one year" half is not: the epoch count it walks
// is derived from the same declared interval, so it is a claim about that many EPOCHS, and the word
// "year" attached to it is the hypothesis speaking.
func TestEmissionDureeDeVieNEstPasInerteSousHypothese(t *testing.T) {
	for _, nom := range genesisLivres {
		t.Run(nom, func(t *testing.T) {
			chemin := filepath.Join("..", nom)
			p := paramsEmissionDuGenesis(t, chemin)
			reserve := reserveDuGenesis(t, chemin)

			r := emissionkeeper.EpochRelease(reserve, 0, parametresKeeper(p))
			require.NotZero(t, r.Released, "%s: the first epoch releases nothing at all (rate truncated to zero)", nom)
			require.NotZero(t, r.Security, "%s: the SECURITY stream is zero on the first epoch: no validator is paid", nom)
			require.NotZero(t, r.Avail, "%s: the AVAILABILITY stream is zero on the first epoch", nom)

			// A year's worth of epochs UNDER THE DECLARED INTERVAL, both regimes: the Reserve must still be
			// mostly there, and must have moved.
			epoquesParAn := chaintime.An / (p.EpochBlocks * chaintime.TargetBlockSeconds)
			require.NotZerof(t, epoquesParAn,
				"%s: an epoch lasts more than a year; that is a different economy.\n%s", nom, hypothese())
			for _, saturee := range []bool{false, true} {
				restant := reserve
				for i := uint64(0); i < epoquesParAn; i++ {
					restant = emissionkeeper.EpochRelease(restant, demande(restant, saturee), parametresKeeper(p)).NewReserve
				}
				part := float64(restant) / float64(reserve)
				require.Greaterf(t, part, 0.80,
					"%s (saturee=%v): only %.1f %% of the Reserve is left after %d epochs.\n%s",
					nom, saturee, part*100, epoquesParAn, hypothese())
				require.Lessf(t, part, 1.0,
					"%s (saturee=%v): the Reserve did not move in %d epochs.\n%s",
					nom, saturee, epoquesParAn, hypothese())
				t.Logf("%s (saturee=%v): %.2f %% of the Reserve left after %d epochs (one year at %d s/block, assumed)",
					nom, saturee, part*100, epoquesParAn, chaintime.TargetBlockSeconds)
			}
		})
	}
}

// TestEmissionDureeDeVieCadenceDisponibiliteIndependante — `emission.epoch_blocks` and
// `jobs.avail_epoch_blocks` are two homonymous-looking cadences in two different modules, and only one
// of them was slowed down. Availability challenges must keep firing at their own fast rhythm: they are
// a LIVENESS probe, and probing once a day would let a miner be absent for hours at no cost.
// `x/jobs` reads `params.AvailEpochBlocks` and never reads the emission parameters (its only link to
// x/emission is the AvailPool BALANCE, through PayAvail), so the independence is structural — this
// check keeps the two from being unified by a well-meaning edit of the genesis.
func TestEmissionDureeDeVieCadenceDisponibiliteIndependante(t *testing.T) {
	for _, nom := range genesisLivres {
		chemin := filepath.Join("..", nom)
		emis := paramsEmissionDuGenesis(t, chemin)
		jobs := paramsJobsDuGenesis(t, chemin)

		require.NotZero(t, jobs.AvailEpochBlocks, "%s: availability is disabled (avail_epoch_blocks=0)", nom)
		require.Lessf(t, jobs.AvailEpochBlocks*20, emis.EpochBlocks,
			"%s: the availability cadence (%d blocks) is no longer far below the emission cadence (%d blocks). "+
				"These are two parameters of two modules; the release of the Reserve was slowed to a daily "+
				"rhythm on purpose, the liveness probe was NOT. If they have converged, availability is now "+
				"checked at the pace of a monetary epoch and an absent miner goes unnoticed for that long.",
			nom, jobs.AvailEpochBlocks, emis.EpochBlocks)
	}
}

// ── helpers ──────────────────────────────────────────────────────────────────────────────────────

// demande returns the non-recoverable demand fed to the gate for a regime. The saturated value is the
// remaining Reserve, which is both far above the cap (`work_gate_bps` x demand vs half a slice) and
// small enough that `mulBps` cannot overflow — `Params.Validate` bounds work_gate_bps at 100000, and
// 10^13 x 10^5 stays 18x under 2^64.
func demande(reserve uint64, saturee bool) uint64 {
	if !saturee {
		return 0
	}
	return reserve
}

func parametresKeeper(p emissiontypes.Params) emissionkeeper.Params {
	return emissionkeeper.Params{
		ReserveReleaseBps: p.ReserveReleaseBps,
		WorkSplitBps:      p.WorkSplitBps,
		AvailSplitBps:     p.AvailSplitBps,
		WorkGateBps:       p.WorkGateBps,
	}
}

// demiVieEnEpoques replays EpochRelease until the Reserve has halved, and returns the number of
// epochs. Same integer arithmetic as the chain, so this measures the shipped curve rather than a
// closed-form approximation of it.
func demiVieEnEpoques(t *testing.T, reserve uint64, p emissiontypes.Params, saturee bool) uint64 {
	t.Helper()
	kp := parametresKeeper(p)
	cible := reserve / 2
	restant := reserve
	for e := uint64(1); e <= borneIterations; e++ {
		r := emissionkeeper.EpochRelease(restant, demande(restant, saturee), kp)
		require.NotZerof(t, r.Released,
			"the release truncated to zero at epoch %d with %d udndr left: emission has stopped, and a "+
				"half-life measured on a curve that no longer moves is infinite, not long", e, restant)
		restant = r.NewReserve
		if restant <= cible {
			return e
		}
	}
	t.Fatalf("the Reserve has not halved after %d epochs: the release rate is effectively nil", borneIterations)
	return 0
}

// epoquesEnAnneesSousHypothese converts through x/chaintime, so the meaning of a block count follows
// the single place where the target block interval is DECLARED. The name carries "SousHypothese"
// because that is the whole point: the output is not a duration the network guarantees, it is the
// duration implied by a constant. Every caller therefore hands the result to a message that repeats
// the assumption.
func epoquesEnAnneesSousHypothese(epoques, epochBlocks uint64) float64 {
	secondes := float64(epoques) * float64(epochBlocks) * float64(chaintime.TargetBlockSeconds)
	return secondes / float64(chaintime.An)
}

// reserveDuGenesis reads the coins of the `emission_reserve` account — the pocket x/emission releases
// from. Reading it rather than assuming it means a change of allocation is measured, not ignored.
func reserveDuGenesis(t *testing.T, chemin string) uint64 {
	t.Helper()
	brut, err := os.ReadFile(chemin)
	require.NoError(t, err)

	var doc struct {
		Accounts []struct {
			Name       string   `yaml:"name"`
			ModuleName string   `yaml:"module_name"`
			Coins      []string `yaml:"coins"`
		} `yaml:"accounts"`
	}
	require.NoError(t, yaml.Unmarshal(brut, &doc))

	for _, a := range doc.Accounts {
		if a.ModuleName != emissiontypes.ModuleName {
			continue
		}
		require.Len(t, a.Coins, 1, "%s: the emission module account holds several denoms", filepath.Base(chemin))
		montant, denom := decouperCoin(t, a.Coins[0])
		require.Equal(t, "udndr", denom)
		require.Equalf(t, emissionkeeper.GenesisReserveU, montant,
			"%s funds the emission module account with %d udndr while the keeper falls back to %d when "+
				"genesis is silent. Two sources of truth for the size of the Reserve.",
			filepath.Base(chemin), montant, emissionkeeper.GenesisReserveU)
		return montant
	}
	t.Fatalf("%s declares no account with module_name: %s -> the Reserve is not funded at genesis",
		filepath.Base(chemin), emissiontypes.ModuleName)
	return 0
}

// paramsEmissionDuGenesis reads `genesis.app_state.emission.params` over the defaults, exactly as a
// partial genesis loads. Twin of paramsJobsDuGenesis (genesis_shipped_test.go); the reflective walk is
// what makes an unknown key a failure instead of a silently ignored line.
func paramsEmissionDuGenesis(t *testing.T, chemin string) emissiontypes.Params {
	t.Helper()
	brut, err := os.ReadFile(chemin)
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
	require.NoError(t, yaml.Unmarshal(brut, &doc))
	require.NotEmpty(t, doc.Genesis.AppState.Emission.Params,
		"%s sets no emission parameter: the chain would fall back to a curve nobody chose here",
		filepath.Base(chemin))

	p := emissiontypes.DefaultParams()
	rv := reflect.ValueOf(&p).Elem()
	for cle, valeur := range doc.Genesis.AppState.Emission.Params {
		champ := rv.FieldByName(camelDepuisSnake(cle))
		require.Truef(t, champ.IsValid() && champ.CanSet(),
			"%s sets `emission.params.%s`, which matches no field of Params: the genesis configures a "+
				"parameter that does not exist, and nobody reads it.", filepath.Base(chemin), cle)
		require.Equalf(t, reflect.Uint64, champ.Kind(), "unhandled type for %s: %s", cle, champ.Kind())
		n, err := strconv.ParseUint(strings.TrimSpace(toString(valeur)), 10, 64)
		require.NoErrorf(t, err, "%s: non-integer value for %s (%v)", filepath.Base(chemin), cle, valeur)
		champ.SetUint(n)
	}
	return p
}
