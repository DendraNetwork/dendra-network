package app

import (
	"github.com/cosmos/cosmos-sdk/types/module"
	authtypes "github.com/cosmos/cosmos-sdk/x/auth/types"
)

// randomBaseGenesisAccounts returns the simulation's genesis accounts, all of them plain BaseAccounts.
//
// Epoch 14 removed x/auth/vesting from the application. The SDK's default generator
// (`authsims.RandomGenesisAccounts`) turns about half of the unbonded simulated accounts into vesting
// accounts, whose type URLs this application no longer registers, and the simulation then panics at
// genesis with `unable to resolve type URL /cosmos.vesting.v1beta1.ContinuousVestingAccount` before
// the first block. A network that cannot hold a vesting account has none to simulate.
func randomBaseGenesisAccounts(simState *module.SimulationState) authtypes.GenesisAccounts {
	accs := make(authtypes.GenesisAccounts, len(simState.Accounts))
	for i, acc := range simState.Accounts {
		accs[i] = authtypes.NewBaseAccountWithAddress(acc.Address)
	}
	return accs
}
