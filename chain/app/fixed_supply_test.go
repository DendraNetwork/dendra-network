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

// ═══ "FIXED SUPPLY OF 10 M, ZERO INFLATION" — THE FIRST PUBLIC PROMISE, HELD BY TESTS ════════════
//
// This is the first property an external auditor checks on a fixed-supply L1, and it used to rest on
// NO check at all: `x/mint` is not registered, but nothing prevented it from coming back, and
// nothing watched who holds the `minter` permission. Re-adding the module, or granting `minter` to
// an in-house module, would have left the whole suite green.
//
// The promise decomposes into three INDEPENDENT facts, hence three cases:
//
//	(a) no minting module is wired into the application;
//	(b) the EXACT SET of module account permissions is the one announced — a set, not an absence:
//	    "no new minter" is verified by pinning EVERYTHING, otherwise the guard only covers the
//	    permissions that were already known when it was written;
//	(c) the supply POSTED AT GENESIS is exactly 10 000 000 DNDR.
//
// (a) and (b) without (c) would admit an 11 M genesis; (c) without (a) and (b) would let a chain
// that started at 10 M mint more. None of the three replaces the others.

// offreFixeUdndr — 10 000 000 DNDR in base units (1 DNDR = 10^6 udndr).
const offreFixeUdndr uint64 = 10_000_000 * 1_000_000

// shippedGenesis — the COMMITTED genesis files whose supply is locked here. A genesis file added to
// the repository and absent from this list is not covered, which is why case (c) also requires the
// list to match the files actually present.
var shippedGenesis = []string{"config.yml", "config.optimistic.yml"}

// permissionsAttendues — the EXACT set, per module account. Nil means no permission.
//
// Only `transfer` (IBC) holds `minter`: it mints vouchers of FOREIGN denominations (`ibc/...`),
// never native udndr. No Dendra module mints; `jobs` only burns.
var permissionsAttendues = map[string][]string{
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

// (a) NO MINTING MODULE — and the whole set of wired modules is pinned, so that the arrival of any
// module is a decision rather than a side effect of an SDK upgrade.
func TestFixedSupplyHasNoMintingModule(t *testing.T) {
	app := New(log.NewNopLogger(), dbm.NewMemDB(), nil, true,
		simtestutil.AppOptionsMap{flags.FlagHome: t.TempDir()})

	modules := make([]string, 0, len(app.ModuleManager.Modules))
	for nom := range app.ModuleManager.Modules {
		modules = append(modules, nom)
	}
	sort.Strings(modules)

	require.NotContains(t, modules, "mint",
		"`x/mint` is back in the application. It is the module that MINTS money, with its governable "+
			"inflation parameters: its mere presence voids the \"fixed supply of 10 M, zero inflation\" "+
			"promise, which holds only because the BINARY cannot mint.")

	attendus := []string{
		"06-solomachine", "07-tendermint", "auth", "authz", "bank", "circuit", "consensus",
		"distribution", "emission", "epochs", "evidence", "feegrant", "genutil", "gov", "group",
		"ibc", "interchainaccounts", "jobs", "modelregistry", "nft", "params", "runtime",
		"slashing", "staking", "transfer", "upgrade", "vesting",
	}
	require.Equal(t, attendus, modules,
		"the set of wired modules has changed. That is not forbidden — it is a decision, and it must be "+
			"visible: every new module brings its own governable parameters, and some of them can create money.")
}

// (b) THE EXACT SET OF MODULE ACCOUNT PERMISSIONS — who can mint, who can burn.
//
// Both faces are checked: the declaration (`moduleAccPerms`, what the repository announces) and what
// the auth keeper actually registered after wiring. The two can diverge — the declaration goes
// through depinject, and on a day where it were no longer consumed, pinning the declaration alone
// would give a green that measures nothing.
func TestFixedSupplyModulePermissionsArePinned(t *testing.T) {
	declare := map[string][]string{}
	for _, p := range moduleAccPerms {
		declare[p.Account] = p.Permissions
	}
	require.Equal(t, normaliserPerms(permissionsAttendues), normaliserPerms(declare),
		"the declared module account permissions have changed. Look FIRST at who holds `minter`: it is "+
			"the permission that creates money. Only `transfer` (IBC vouchers) may have it; no Dendra "+
			"module mints native udndr.")

	app := New(log.NewNopLogger(), dbm.NewMemDB(), nil, true,
		simtestutil.AppOptionsMap{flags.FlagHome: t.TempDir()})
	cable := map[string][]string{}
	for compte, perms := range app.AuthKeeper.GetModulePermissions() {
		cable[compte] = perms.GetPermissions()
	}
	require.Equal(t, normaliserPerms(permissionsAttendues), normaliserPerms(cable),
		"the permissions ACTUALLY registered by x/auth differ from the ones this test pins: the "+
			"declaration and the wiring have diverged.")

	for compte, perms := range normaliserPerms(cable) {
		if compte == "transfer" {
			continue
		}
		require.NotContains(t, perms, authtypes.Minter,
			"module account %q holds `minter`: it can create money. Fixed supply is no longer a property "+
				"of the binary.", compte)
	}
}

// normaliserPerms makes the sets comparable: sorted lists, with `nil` and the empty list treated alike.
func normaliserPerms(m map[string][]string) map[string][]string {
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

// (c) THE SUPPLY POSTED AT GENESIS IS 10 000 000 DNDR — in every committed genesis.
//
// The sum is read over the genesis ACCOUNTS, including the Reserve held by the `emission` module
// account. The Reserve is not deferred minting: it is already inside the 10 M, and `x/emission` only
// releases it. That is exactly what this sum proves — if the Reserve were one day added ON TOP, the
// total would exceed 10 M and this case would go red.
func TestFixedSupplyGenesisIsTenMillion(t *testing.T) {
	presents, err := filepath.Glob(filepath.Join("..", "config*.yml"))
	require.NoError(t, err)
	noms := []string{}
	for _, p := range presents {
		noms = append(noms, filepath.Base(p))
	}
	sort.Strings(noms)
	attendus := append([]string{}, shippedGenesis...)
	sort.Strings(attendus)
	require.Equal(t, attendus, noms,
		"the list of committed genesis files has changed: an unlisted genesis is covered by NO supply "+
			"check. Add the file to `shippedGenesis`, or remove it from the repository.")

	for _, nom := range shippedGenesis {
		t.Run(nom, func(t *testing.T) {
			brut, err := os.ReadFile(filepath.Join("..", nom))
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
			require.NoError(t, yaml.Unmarshal(brut, &doc))
			require.NotEmpty(t, doc.Accounts, "no account was read: this check no longer measures anything")

			var total uint64
			for _, acc := range doc.Accounts {
				for _, c := range acc.Coins {
					montant, denom := decouperCoin(t, c)
					require.Equal(t, "udndr", denom,
						"account %q: unexpected denomination at genesis (%s)", acc.Name, denom)
					total += montant
				}
			}
			require.Equal(t, offreFixeUdndr, total,
				"the supply posted at genesis of %s is %d udndr instead of %d (10 000 000 DNDR). This is "+
					"the first public promise, and it is verified by summing the accounts, not by reading "+
					"them one by one.", nom, total, offreFixeUdndr)

			_, aMint := doc.Genesis.AppState["mint"]
			require.False(t, aMint,
				"%s carries a `mint` section: `x/mint` is not registered, so such a genesis would not "+
					"start — and if the module came back, inflation would come back with it.", nom)
		})
	}
}

// decouperCoin splits "3300000000000udndr" into (3300000000000, "udndr").
func decouperCoin(t *testing.T, coin string) (uint64, string) {
	t.Helper()
	i := strings.IndexFunc(coin, func(r rune) bool { return r < '0' || r > '9' })
	require.Greater(t, i, 0, "unreadable coin: %q", coin)
	montant, err := strconv.ParseUint(coin[:i], 10, 64)
	require.NoError(t, err, "unreadable coin: %q", coin)
	return montant, coin[i:]
}
