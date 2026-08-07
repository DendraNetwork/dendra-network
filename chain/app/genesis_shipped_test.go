package app

import (
	"os"
	"path/filepath"
	"reflect"
	"strconv"
	"strings"
	"testing"

	"github.com/stretchr/testify/require"
	"gopkg.in/yaml.v3"

	jobstypes "github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// ═══ THE COMMITTED GENESIS FILES PASS THE VALIDATION THEY WILL FACE AT STARTUP ═══════════════════
//
// WHAT THIS FILE CATCHES. `Params.Validate()` refuses an armed optimistic mode with no dispute bond
// and no unpredictable seed source; `InitGenesis` invokes it, so such a genesis does not boot. A
// configuration file shipped in the repository and refused at startup is a failure that surfaces only
// at launch, on someone else's machine — the worst moment, with the least context.
//
// The check re-reads the parameters ACTUALLY written in the files and applies the REAL validation to
// them. It copies no value: copying would make this test a duplicate of the file, green whatever
// happens.
//
// What it does not prove: that the values are well CALIBRATED. It proves they are CONSISTENT with each
// other, that the genesis does not arm half a mechanism.

func TestGenesisLivresPassentLaValidationDesParams(t *testing.T) {
	for _, nom := range genesisLivres {
		t.Run(nom, func(t *testing.T) {
			p := paramsJobsDuGenesis(t, filepath.Join("..", nom))
			require.NoError(t, p.Validate(),
				"%s sets jobs parameters that `Params.Validate()` refuses; `InitGenesis` calls it, so a "+
					"chain started from this genesis would not boot.", nom)
		})
	}
}

// The optimistic genesis is the only one that ARMS mode 1; the guards that mode presupposes must
// actually be set there, not merely acceptable.
func TestGenesisOptimistePorteSesGardes(t *testing.T) {
	p := paramsJobsDuGenesis(t, filepath.Join("..", "config.optimistic.yml"))
	require.Equal(t, uint64(1), p.VerificationMode,
		"this genesis exists to arm optimistic mode: if it stops arming it, the requirements below become "+
			"vacuous and this check measures nothing")
	require.NotZero(t, p.DisputeBond, "disputing must cost a bond")
	require.True(t, p.CommitteeSeedSource == 1 || p.CommitteeRevealDelay > 0,
		"the committee seed must be unpredictable: decentralized VRF or deferred reveal")

	// The default genesis stays redundant: these guards are not imposed on it.
	d := paramsJobsDuGenesis(t, filepath.Join("..", "config.yml"))
	require.Equal(t, uint64(0), d.VerificationMode,
		"the default genesis must stay in redundant mode (k=3): flipping it is a decision that changes "+
			"what the chain verifies")
}

// paramsJobsDuGenesis reads `genesis.app_state.jobs.params` and applies it on top of the default
// parameters, exactly as loading a partial genesis does: an absent field keeps its default value.
func paramsJobsDuGenesis(t *testing.T, chemin string) jobstypes.Params {
	t.Helper()
	brut, err := os.ReadFile(chemin)
	require.NoError(t, err)

	var doc struct {
		Genesis struct {
			AppState struct {
				Jobs struct {
					Params map[string]any `yaml:"params"`
				} `yaml:"jobs"`
			} `yaml:"app_state"`
		} `yaml:"genesis"`
	}
	require.NoError(t, yaml.Unmarshal(brut, &doc))

	p := jobstypes.DefaultParams()
	rv := reflect.ValueOf(&p).Elem()
	for cle, valeur := range doc.Genesis.AppState.Jobs.Params {
		champ := rv.FieldByName(camelDepuisSnake(cle))
		require.True(t, champ.IsValid(),
			"%s sets `jobs.params.%s`, which matches no field of Params: the genesis configures a "+
				"parameter that does not exist, and nobody reads it.", filepath.Base(chemin), cle)
		require.True(t, champ.CanSet(), "field %s is not assignable", cle)

		switch champ.Kind() {
		case reflect.Uint64:
			n, err := strconv.ParseUint(strings.TrimSpace(toString(valeur)), 10, 64)
			require.NoError(t, err, "%s: non-integer value for %s (%v)", filepath.Base(chemin), cle, valeur)
			champ.SetUint(n)
		case reflect.Bool:
			b, err := strconv.ParseBool(strings.TrimSpace(toString(valeur)))
			require.NoError(t, err, "%s: non-boolean value for %s (%v)", filepath.Base(chemin), cle, valeur)
			champ.SetBool(b)
		case reflect.String:
			champ.SetString(toString(valeur))
		default:
			t.Fatalf("unhandled type for %s: %s", cle, champ.Kind())
		}
	}
	return p
}

func toString(v any) string {
	switch x := v.(type) {
	case string:
		return x
	case bool:
		return strconv.FormatBool(x)
	case int:
		return strconv.Itoa(x)
	default:
		return ""
	}
}

// camelDepuisSnake maps "audit_sample_bps" to "AuditSampleBps". Purely numeric segments (`sha256`) and
// the repository's acronyms (`vrf`, `bps`) follow the same rule as the proto generator: capitalize the
// first letter, leave the rest unchanged.
func camelDepuisSnake(s string) string {
	parties := strings.Split(s, "_")
	for i, p := range parties {
		if p == "" {
			continue
		}
		parties[i] = strings.ToUpper(p[:1]) + p[1:]
	}
	return strings.Join(parties, "")
}
