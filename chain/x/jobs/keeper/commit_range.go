package keeper

import "cosmossdk.io/collections"

// commitRange bounds a walk over `Commit` to the keys carrying a given prefix.
//
// `Commit` keys have the form "<jobId>__<minerId>" (and the variants "<jobId>__redo__<minerId>",
// "<jobId>__verdict__<minerId>"). Eight handlers (settlement, payment, finalization, adjudication,
// semantic verification, anti-evasion) walked the whole collection through `Walk(ctx, nil, ...)` and
// then discarded in memory everything that did not concern their job, so the cost of a settlement grew
// with the complete history of the chain instead of with the size of the job being settled.
//
// At a few thousand commits the overhead does not show, but it only grows, for transactions whose
// content does not change. Past a threshold, the gas of a settlement exceeds the block limit and no job
// settles any more: a total freeze of the protocol, caused by the passage of time alone, without a
// single faulty transaction. Such a debt cannot be fixed once it shows, because the migration itself
// would need transactions to go through.
//
// `Prefix` restricts the walk to the relevant keys, relying on the lexicographic order of the encoded
// keys, which `StringKey` preserves for a terminal key. Jobs cannot collide: "j1__" does not prefix the
// keys of "j10", which start with "j10" ("0" != "_"). The cost is proportional to the number of commits
// of the job again.
//
// Callers keep their `strings.HasPrefix`: it no longer selects (the range does) but stays as defence in
// depth, so that if the bounding turned out wider than expected, commits of another job would still not
// enter a payment tally. The filter is free on keys already loaded; the test measures the bounding, not
// the filter (it counts the keys actually visited).
func commitRange(prefix string) *collections.Range[string] {
	return new(collections.Range[string]).Prefix(prefix)
}
