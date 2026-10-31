package simutil

import (
	"os"
	"path/filepath"
	"testing"

	"cosmossdk.io/collections"
	storetypes "cosmossdk.io/store/types"
	"github.com/cosmos/cosmos-sdk/runtime"
	"github.com/stretchr/testify/require"
)

func reviewSchema(t *testing.T) collections.Schema {
	t.Helper()
	key := storetypes.NewKVStoreKey("coverage")
	builder := collections.NewSchemaBuilder(runtime.NewKVStoreService(key))
	collections.NewItem(builder, collections.NewPrefix("known"), "known", collections.Uint64Value)
	schema, err := builder.Build()
	require.NoError(t, err)
	return schema
}

func TestDeclaredPrefixesMissingEmptyAndPhantoms(t *testing.T) {
	dir := t.TempDir()
	_, err := DeclaredPrefixes(filepath.Join(dir, "missing"))
	require.Error(t, err)
	require.NoError(t, os.WriteFile(filepath.Join(dir, "keys.go"), []byte("package sample\n"), 0600))
	declared, err := DeclaredPrefixes(dir)
	require.NoError(t, err)
	require.Empty(t, declared)
	source := "package sample\nvar A = collections.NewPrefix(\"known\")\nvar Z = collections.NewPrefix(\"missing-z\")\nvar B = collections.NewPrefix(\"missing-b\")\n"
	require.NoError(t, os.WriteFile(filepath.Join(dir, "keys.go"), []byte(source), 0600))
	require.NoError(t, os.WriteFile(filepath.Join(dir, "ignored_test.go"), []byte("var Ignored = collections.NewPrefix(\"test-only\")"), 0600))
	declared, err = DeclaredPrefixes(dir)
	require.NoError(t, err)
	require.Equal(t, map[string]string{"A": "known", "Z": "missing-z", "B": "missing-b"}, declared)
	require.Equal(t, []string{"B = missing-b", "Z = missing-z"}, PhantomPrefixes(reviewSchema(t), declared))
	require.Empty(t, PhantomPrefixes(reviewSchema(t), map[string]string{"A": "known"}))
}
