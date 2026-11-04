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

// The committed genesis files pass the validation they will face at startup.
//
// `Params.Validate()` refuses an armed optimistic mode with no dispute bond and no unpredictable seed
// source, and `InitGenesis` calls it, so such a genesis does not boot. A configuration file shipped in
// the repository and refused at startup only fails at launch, on someone else's machine, with the
// least context.
//
// The check re-reads the parameters written in the files and applies the real validation to them. It
// copies no value: a copy would make this test a duplicate of the file, green whatever happens.
//
// It does not prove that the values are well calibrated, only that they are consistent with each
// other and that the genesis does not arm half a mechanism.

func TestShippedGenesisPassParamValidation(t *testing.T) {
	for _, name := range shippedGenesis {
		t.Run(name, func(t *testing.T) {
			p := genesisJobsParams(t, filepath.Join("..", name))
			require.NoError(t, p.Validate(),
				"%s sets jobs parameters that `Params.Validate()` refuses; `InitGenesis` calls it, so a "+
					"chain started from this genesis would not boot.", name)
		})
	}
}

// The optimistic genesis is the only one that arms mode 1, so the guards that mode presupposes must
// actually be set there; being acceptable is not enough.
func TestOptimisticGenesisCarriesItsGuards(t *testing.T) {
	p := genesisJobsParams(t, filepath.Join("..", "config.optimistic.yml"))
	require.Equal(t, uint64(1), p.VerificationMode,
		"this genesis exists to arm optimistic mode: if it stops arming it, the requirements below become "+
			"vacuous and this check measures nothing")
	require.NotZero(t, p.DisputeBond, "disputing must cost a bond")
	require.True(t, p.CommitteeSeedSource == 1 || p.CommitteeRevealDelay > 0,
		"the committee seed must be unpredictable: decentralized VRF or deferred reveal")

	// The default genesis stays redundant: these guards are not imposed on it.
	d := genesisJobsParams(t, filepath.Join("..", "config.yml"))
	require.Equal(t, uint64(0), d.VerificationMode,
		"the default genesis must stay in redundant mode (k=3): flipping it is a decision that changes "+
			"what the chain verifies")
}

// genesisJobsParams reads `genesis.app_state.jobs.params` and applies it on top of the default
// parameters, exactly as loading a partial genesis does: an absent field keeps its default value.
func genesisJobsParams(t *testing.T, path string) jobstypes.Params {
	t.Helper()
	raw, err := os.ReadFile(path)
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
	require.NoError(t, yaml.Unmarshal(raw, &doc))

	p := jobstypes.DefaultParams()
	rv := reflect.ValueOf(&p).Elem()
	for key, value := range doc.Genesis.AppState.Jobs.Params {
		field := rv.FieldByName(camelFromSnake(key))
		require.True(t, field.IsValid(),
			"%s sets `jobs.params.%s`, which matches no field of Params: the genesis configures a "+
				"parameter that does not exist, and nobody reads it.", filepath.Base(path), key)
		require.True(t, field.CanSet(), "field %s is not assignable", key)

		switch field.Kind() {
		case reflect.Uint64:
			n, err := strconv.ParseUint(strings.TrimSpace(toString(value)), 10, 64)
			require.NoError(t, err, "%s: non-integer value for %s (%v)", filepath.Base(path), key, value)
			field.SetUint(n)
		case reflect.Bool:
			b, err := strconv.ParseBool(strings.TrimSpace(toString(value)))
			require.NoError(t, err, "%s: non-boolean value for %s (%v)", filepath.Base(path), key, value)
			field.SetBool(b)
		case reflect.String:
			field.SetString(toString(value))
		default:
			t.Fatalf("unhandled type for %s: %s", key, field.Kind())
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

// camelFromSnake maps "audit_sample_bps" to "AuditSampleBps". Purely numeric segments (`sha256`) and
// the repository's acronyms (`vrf`, `bps`) follow the same rule as the proto generator: capitalize the
// first letter, leave the rest unchanged.
func camelFromSnake(s string) string {
	parties := strings.Split(s, "_")
	for i, p := range parties {
		if p == "" {
			continue
		}
		parties[i] = strings.ToUpper(p[:1]) + p[1:]
	}
	return strings.Join(parties, "")
}
