package app

import (
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"testing"

	"cosmossdk.io/log"
	dbm "github.com/cosmos/cosmos-db"
	"github.com/cosmos/cosmos-sdk/client/flags"
	simtestutil "github.com/cosmos/cosmos-sdk/testutil/sims"
	authtypes "github.com/cosmos/cosmos-sdk/x/auth/types"
	"github.com/stretchr/testify/require"
	"gopkg.in/yaml.v3"
)

// Fixed supply of 10 M and zero inflation: the first public promise, held by tests.
//
// It is the first property an external auditor checks on a fixed-supply L1, so it needs a check of its
// own: `x/mint` is not registered, but without this test nothing would prevent it from coming back,
// and nothing would watch who holds the `minter` permission. Re-adding the module, or granting `minter`
// to one of the project's modules, would leave the rest of the suite green.
//
// The promise splits into three independent facts, hence three cases:
//
//	(a) no minting module is wired into the application;
//	(b) the exact set of module account permissions is the announced one. It is pinned as a
//	    whole set, so a new minter is caught even when its permission was unknown when this
//	    guard was written;
//	(c) the supply posted at genesis is exactly 10 000 000 DNDR.
//
// (a) and (b) without (c) would admit an 11 M genesis; (c) without (a) and (b) would let a chain that
// started at 10 M mint more.

// fixedSupplyUdndr is 10 000 000 DNDR in base units (1 DNDR = 10^6 udndr).
const fixedSupplyUdndr uint64 = 10_000_000 * 1_000_000

// shippedGenesis lists the committed genesis files whose supply is locked here. A genesis file added
// to the repository and missing from this list is not covered, which is why case (c) also requires
// the list to match the files actually present.
var shippedGenesis = []string{"config.yml", "config.optimistic.yml"}

// expectedPermissions is the exact set, per module account. Nil means no permission.
//
// Only `transfer` (IBC) holds `minter`: it mints vouchers of foreign denominations (`ibc/...`), never
// native udndr. No Dendra module mints; `jobs` only burns.
var expectedPermissions = map[string][]string{
	"fee_collector":          nil,
	"distribution":           nil,
	"bonded_tokens_pool":     {authtypes.Burner, "staking"},
	"not_bonded_tokens_pool": {authtypes.Burner, "staking"},
	"gov":                    {authtypes.Burner},
	"nft":                    nil,
	"transfer":               {authtypes.Minter, authtypes.Burner},
	"interchainaccounts":     nil,
	"jobs":                   {authtypes.Burner},
	"emission":               nil,
}

// (a) No minting module. The whole set of wired modules is pinned, so that a new module arrives by
// decision and never as a side effect of an SDK upgrade.
func TestFixedSupplyHasNoMintingModule(t *testing.T) {
	app := New(log.NewNopLogger(), dbm.NewMemDB(), nil, true,
		simtestutil.AppOptionsMap{flags.FlagHome: t.TempDir()})

	modules := make([]string, 0, len(app.ModuleManager.Modules))
	for name := range app.ModuleManager.Modules {
		modules = append(modules, name)
	}
	sort.Strings(modules)

	require.NotContains(t, modules, "mint",
		"`x/mint` is back in the application. It is the module that mints money, with its governable "+
			"inflation parameters: its mere presence voids the \"fixed supply of 10 M, zero inflation\" "+
			"promise, which holds only because the binary cannot mint.")

	expected := []string{
		"06-solomachine", "07-tendermint", "auth", "authz", "bank", "circuit", "consensus",
		"distribution", "emission", "epochs", "evidence", "feegrant", "genutil", "gov", "group",
		"ibc", "interchainaccounts", "jobs", "modelregistry", "nft", "params", "runtime",
		"slashing", "staking", "transfer", "upgrade",
	}
	require.Equal(t, expected, modules,
		"the set of wired modules has changed. That is not forbidden — it is a decision, and it must be "+
			"visible: every new module brings its own governable parameters, and some of them can create money.")
}

// (b) The exact set of module account permissions: who can mint, who can burn.
//
// Both sides are checked: the declaration (`moduleAccPerms`, what the repository announces) and what
// the auth keeper registered after wiring. The two can diverge, since the declaration goes through
// depinject; if it were ever no longer consumed, pinning the declaration alone would measure nothing.
func TestFixedSupplyModulePermissionsArePinned(t *testing.T) {
	declared := map[string][]string{}
	for _, p := range moduleAccPerms {
		declared[p.Account] = p.Permissions
	}
	require.Equal(t, normalizePerms(expectedPermissions), normalizePerms(declared),
		"the declared module account permissions have changed. Look first at who holds `minter`: it is "+
			"the permission that creates money. Only `transfer` (IBC vouchers) may have it; no Dendra "+
			"module mints native udndr.")

	app := New(log.NewNopLogger(), dbm.NewMemDB(), nil, true,
		simtestutil.AppOptionsMap{flags.FlagHome: t.TempDir()})
	wired := map[string][]string{}
	for account, perms := range app.AuthKeeper.GetModulePermissions() {
		wired[account] = perms.GetPermissions()
	}
	require.Equal(t, normalizePerms(expectedPermissions), normalizePerms(wired),
		"the permissions actually registered by x/auth differ from the ones this test pins: the "+
			"declaration and the wiring have diverged.")

	for account, perms := range normalizePerms(wired) {
		if account == "transfer" {
			continue
		}
		require.NotContains(t, perms, authtypes.Minter,
			"module account %q holds `minter`: it can create money. Fixed supply is no longer a property "+
				"of the binary.", account)
	}
}

// normalizePerms makes the sets comparable: sorted lists, with `nil` and the empty list treated alike.
func normalizePerms(m map[string][]string) map[string][]string {
	out := make(map[string][]string, len(m))
	for k, v := range m {
		c := append([]string{}, v...)
		sort.Strings(c)
		if len(c) == 0 {
			c = []string{}
		}
		out[k] = c
	}
	return out
}

// (c) The supply posted at genesis is 10 000 000 DNDR, in every committed genesis.
//
// The sum is read over the genesis accounts, including the Reserve held by the `emission` module
// account. The Reserve is part of the 10 M and `x/emission` only releases it; if it were ever added
// on top, the total would exceed 10 M and this case would go red.
func TestFixedSupplyGenesisIsTenMillion(t *testing.T) {
	presents, err := filepath.Glob(filepath.Join("..", "config*.yml"))
	require.NoError(t, err)
	names := []string{}
	for _, p := range presents {
		names = append(names, filepath.Base(p))
	}
	sort.Strings(names)
	expected := append([]string{}, shippedGenesis...)
	sort.Strings(expected)
	require.Equal(t, expected, names,
		"the list of committed genesis files has changed: an unlisted genesis is covered by no supply "+
			"check. Add the file to `shippedGenesis`, or remove it from the repository.")

	for _, name := range shippedGenesis {
		t.Run(name, func(t *testing.T) {
			raw, err := os.ReadFile(filepath.Join("..", name))
			require.NoError(t, err)

			var doc struct {
				Accounts []struct {
					Name  string   `yaml:"name"`
					Coins []string `yaml:"coins"`
				} `yaml:"accounts"`
				Genesis struct {
					AppState map[string]yaml.Node `yaml:"app_state"`
				} `yaml:"genesis"`
			}
			require.NoError(t, yaml.Unmarshal(raw, &doc))
			require.NotEmpty(t, doc.Accounts, "no account was read: this check no longer measures anything")

			var total uint64
			for _, acc := range doc.Accounts {
				for _, c := range acc.Coins {
					amount, denom := splitCoin(t, c)
					require.Equal(t, "udndr", denom,
						"account %q: unexpected denomination at genesis (%s)", acc.Name, denom)
					total += amount
				}
			}
			require.Equal(t, fixedSupplyUdndr, total,
				"the supply posted at genesis of %s is %d udndr instead of %d (10 000 000 DNDR). This is "+
					"the first public promise, and it is verified by summing the accounts, not by reading "+
					"them one by one.", name, total, fixedSupplyUdndr)

			_, aMint := doc.Genesis.AppState["mint"]
			require.False(t, aMint,
				"%s carries a `mint` section: `x/mint` is not registered, so such a genesis would not "+
					"start — and if the module came back, inflation would come back with it.", name)
		})
	}
}

// splitCoin splits "3300000000000udndr" into (3300000000000, "udndr").
func splitCoin(t *testing.T, coin string) (uint64, string) {
	t.Helper()
	i := strings.IndexFunc(coin, func(r rune) bool { return r < '0' || r > '9' })
	require.Greater(t, i, 0, "unreadable coin: %q", coin)
	amount, err := strconv.ParseUint(coin[:i], 10, 64)
	require.NoError(t, err, "unreadable coin: %q", coin)
	return amount, coin[i:]
}
