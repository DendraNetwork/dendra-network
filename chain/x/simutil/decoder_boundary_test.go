package simutil

import (
	"github.com/cosmos/cosmos-sdk/types/kv"
	"github.com/stretchr/testify/require"
	"testing"
)

func TestDecodeStoreKnownAbsentCorruptAndUnknown(t *testing.T) {
	decode := NewDecodeStore(reviewSchema(t))
	known := []byte("known")
	out := decode(kv.Pair{Key: known}, kv.Pair{Key: known, Value: []byte{1}})
	require.Contains(t, out, "known")
	require.Contains(t, out, "(absent)")
	require.Contains(t, out, "undecodable")
	out = decode(kv.Pair{}, kv.Pair{Key: []byte("outside"), Value: []byte{1}})
	require.Contains(t, out, "key outside the schema")
	require.Contains(t, renderKey(nil), "no key")
	require.Contains(t, renderKey([]byte{'a', 1}), "a·")
}
