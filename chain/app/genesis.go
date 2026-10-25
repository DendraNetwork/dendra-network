package app

import (
	"encoding/json"
)

// GenesisState of the blockchain is represented here as a map of raw json messages keyed by an
// identifier string. The identifier determines which module the genesis information belongs to, so it
// can be routed during InitChain. In this application the default genesis information is retrieved from
// the ModuleBasicManager, which populates json from each BasicModule object provided to it during init.
type GenesisState map[string]json.RawMessage
