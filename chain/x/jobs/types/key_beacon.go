package types

import "cosmossdk.io/collections"

// BeaconKey is the prefix to retrieve all Beacon
var BeaconKey = collections.NewPrefix("beacon/value/")
