package keeper_test

import (
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/simutil"
)

// NO GHOST PREFIX IN `x/modelregistry/types`.
//
// There are only two prefixes here, and that is precisely why the check earns its place: the module
// nobody ever inspects is the one where an orphaned declaration survives longest. A class of defect
// closed on a single module is not closed.
func TestModelRegistryHasNoGhostPrefix(t *testing.T) {
	declares, err := simutil.PrefixesDeclares(filepath.Join("..", "types"))
	require.NoError(t, err)
	require.GreaterOrEqual(t, len(declares), 2,
		"only %d prefixes read: the scan no longer bites", len(declares))

	f := initFixture(t)
	require.Empty(t, simutil.PhantomPrefixes(f.keeper.Schema, declares),
		"prefix declared in `x/modelregistry/types` but absent from the schema: reachable only through "+
			"direct store access, therefore invisible to the export and to the round trip")
}
