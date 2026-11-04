package keeper_test

import (
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/simutil"
)

// No ghost prefix in `x/modelregistry/types`.
//
// There are only two prefixes here, which is why the check is worth having: the module nobody
// inspects is the one where an orphaned declaration survives longest. A defect class closed on a
// single module is not closed.
func TestModelRegistryHasNoGhostPrefix(t *testing.T) {
	declared, err := simutil.DeclaredPrefixes(filepath.Join("..", "types"))
	require.NoError(t, err)
	require.GreaterOrEqual(t, len(declared), 2,
		"only %d prefixes read: the scan no longer bites", len(declared))

	f := initFixture(t)
	require.Empty(t, simutil.PhantomPrefixes(f.keeper.Schema, declared),
		"prefix declared in `x/modelregistry/types` but absent from the schema: reachable only through "+
			"direct store access, therefore invisible to the export and to the round trip")
}
