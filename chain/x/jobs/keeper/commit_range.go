package keeper

import "cosmossdk.io/collections"

// commitRange bounds a walk over `Commit` to the keys carrying a given prefix.
//
// THE PROBLEM. `Commit` keys have the form "<jobId>__<minerId>" (and its variants
// "<jobId>__redo__<minerId>", "<jobId>__verdict__<minerId>"). Eight handlers — settlement, payment,
// finalization, adjudication, semantic verification, anti-evasion — walked the WHOLE collection
// through `Walk(ctx, nil, ...)` and then discarded in memory everything that did not concern their
// job. The cost of a settlement therefore grew with the COMPLETE history of the chain, not with the
// size of the job being settled.
//
// WHY THAT IS SEVERE LATER AND INVISIBLE TODAY. At a few thousand commits the overhead does not
// show. But it is MONOTONIC: it can only grow, and it grows for transactions whose content does not
// change. Past a threshold, the gas of a settlement exceeds the block limit and NO job settles any
// more — a total freeze of the protocol, triggered by the mere passage of time, without a single
// faulty transaction ever being submitted. It is the kind of debt that can no longer be fixed once
// it manifests: the migration itself would require transactions to go through.
//
// THE FIX. `Prefix` restricts the walk to the subset of relevant keys, relying on the lexicographic
// order of the encoded keys, which `StringKey` preserves for a terminal key. No collision between
// jobs is possible: "j1__" does not prefix the keys of "j10", which start with "j10" ("0" != "_").
// The cost becomes proportional to the number of commits OF the job again.
//
// Callers keep their `strings.HasPrefix`: it no longer selects (the range does that) but acts as
// defence in depth. It is deliberately kept — if it disappeared and the bounding turned out to be
// wider than expected, commits of ANOTHER job would enter a payment tally. The filter is free on
// keys that are already loaded; it is the bounding, not the filter, that the test measures (it
// counts the keys actually visited).
func commitRange(prefix string) *collections.Range[string] {
	return new(collections.Range[string]).Prefix(prefix)
}
