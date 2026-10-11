// emission.go is the core of emission from the Reserve (ADR-023).
//
// It is pure and deterministic: everything is integer arithmetic (udndr, bps = 1/10000), never
// floating point, so every validator reproduces the same result, which consensus requires. An epoch
// hook (BeginBlock every EpochBlocks) calls EpochRelease, credits the three pools (work /
// availability / security) and decrements the Reserve through the bank keeper.
//
// Invariants, each with the test that pins it:
//   - No mint, ever: Released <= reserve, so total supply stays <= 10 M. Emission only releases
//     what was pre-allocated at genesis.
//     (emission_demandgate_test.go, TestEpochReleaseDemandGate)
//   - The Reserve only shrinks: NewReserve <= reserve.
//     (emission_demandgate_test.go, TestEpochReleaseDemandGate)
//   - Conservation: Work + Avail + Security == Released.
//     (emission_demandgate_test.go, TestEpochReleaseDemandGate)
//   - Demand gate (ADR-017): whatever the work stream does not use stays in the Reserve. Releasing
//     it anyway would subsidise self-dealing volume.
//     (emission_demandgate_test.go, TestEpochReleaseDemandGate; the upper bound on the gate parameter
//     itself in p3_workgate_bound_test.go)
//
// No test here pins that the module account holds the coins these counters promise, because that is
// not a property of this file. Arithmetic conservation and provision are two different claims; see
// the deferral in epoch.go, the only place the second one is checked.
//
// uint64 is enough for a 10^13 udndr Reserve. Amounts beyond that range need `cosmossdk.io/math.Int`
// to stay overflow-free.
package keeper

// Params holds the governable emission parameters, in basis points. Every rate here applies per
// epoch, never per unit of real time: a rate quoted "per year" is true only under a block interval
// the consensus does not fix, and the interval was measured at about 5 s where 1 s was assumed.
//
// These comments name the unit and the meaning and quote no shipped value: the compiled default is
// not what ships (the launch genesis carries `reserve_release_bps = 2`, three orders of magnitude
// below 2200), and a comment naming one value goes stale the day the genesis names another. Read the
// live values with `dendrad query emission params -o json`.
type Params struct {
	ReserveReleaseBps uint64 // share of what remains, released once per epoch (decaying curve)
	WorkSplitBps      uint64 // 5000 = 50 % of the release
	AvailSplitBps     uint64 // 2000 = 20 %; security takes the remainder (about 30 %)
	WorkGateBps       uint64 // 15000 = 1.5x the non-recoverable demand (cap on the work subsidy)
}

// DefaultParams returns the shipped parameter set.
func DefaultParams() Params {
	return Params{ReserveReleaseBps: 2200, WorkSplitBps: 5000, AvailSplitBps: 2000, WorkGateBps: 15000}
}

// Release is the outcome of one epoch.
type Release struct {
	Released   uint64 // total taken out of the Reserve this epoch
	Work       uint64 // work stream (demand-gated)
	Avail      uint64 // availability stream
	Security   uint64 // security stream
	NewReserve uint64 // Reserve after the release
}

// mulBps returns x * bps / 10000 in integers (deterministic).
func mulBps(x, bps uint64) uint64 { return x * bps / 10000 }

// EpochRelease releases one epoch from `reserve` (udndr). `nonRecDemand` (udndr) caps the work
// subsidy, so a miner cannot subsidise itself beyond the demand it does not get back.
//
// The caller passes the drop in supply, which is the fee burn alone (`RunEpoch`, epoch.go). That is
// narrower than "everything a miner cannot refund to itself": the treasury and dev shares move coins
// between accounts without destroying them, so they leave total supply untouched and never appear in
// a supply delta.
//
// The narrow reading is deliberate (a supply delta needs no jobs->emission wiring, and with minting at
// zero the supply can only fall), but anything sized on the wider reading over-estimates this cap by
// the treasury and dev shares, roughly a factor of 2.5 on the flow that pays miners. Any model or
// document quoting this gate must quote the supply delta, not the fee split.
func EpochRelease(reserve, nonRecDemand uint64, p Params) Release {
	release := mulBps(reserve, p.ReserveReleaseBps) // the governed share of what remains, this epoch
	workPool := mulBps(release, p.WorkSplitBps)     // 50 %
	avail := mulBps(release, p.AvailSplitBps)       // 20 %
	security := release - workPool - avail          // exact remainder (about 30 %): no rounding loss

	work := workPool // demand gate: bound by WorkGate x non-recoverable demand
	if capWork := mulBps(nonRecDemand, p.WorkGateBps); work > capWork {
		work = capWork
	}
	released := work + avail + security // the unused part (workPool - work) stays in the Reserve
	return Release{
		Released:   released,
		Work:       work,
		Avail:      avail,
		Security:   security,
		NewReserve: reserve - released,
	}
}
