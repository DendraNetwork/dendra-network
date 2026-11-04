package types

import "cosmossdk.io/collections"

// CommitKey is the prefix to retrieve all Commit
var CommitKey = collections.NewPrefix("commit/value/")
