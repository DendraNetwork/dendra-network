// emission.go — the core of emission from the Reserve (ADR-023).
//
// Pure and deterministic: everything is INTEGER arithmetic (udndr, bps = 1/10000), never floating
// point, so every validator reproduces the same result — a consensus requirement, not a style
// preference. An epoch hook (BeginBlock every EpochBlocks) calls EpochRelease, credits the three
// pools (work / availability / security) and decrements the Reserve through the bank keeper.
//
// Invariants. Each one names the test that pins it, and a named test is a claim like any other: it has
// to be a file that exists, in this package, with that function in it — a pointer to a bench nobody can
// run reads as coverage and is none.
//   - No mint, ever: Released <= reserve, so total supply stays <= 10 M. Emission only RELEASES
//     what was pre-allocated at genesis.
//     (emission_demandgate_test.go, TestEpochReleaseDemandGate)
//   - The Reserve only shrinks: NewReserve <= reserve.
//     (emission_demandgate_test.go, TestEpochReleaseDemandGate)
//   - Conservation: Work + Avail + Security == Released.
//     (emission_demandgate_test.go, TestEpochReleaseDemandGate)
//   - Demand gate (ADR-017): whatever the work stream does not use STAYS in the Reserve. Releasing
//     it anyway would subsidise self-dealing volume.
//     (emission_demandgate_test.go, TestEpochReleaseDemandGate; the upper bound on the gate parameter
//     itself in p3_workgate_bound_test.go)
//
// What NO test here pins, because it is not a property of this file: that the module ACCOUNT holds the
// coins these counters promise. Arithmetic conservation and provision are two different claims — see
// the deferral in epoch.go, which is the only place the second one is checked.
//
// uint64 is sufficient for a 10^13 udndr Reserve. Amounts beyond that range require
// `cosmossdk.io/math.Int` to stay overflow-free.
package keeper

// Params — governable, expressed in basis points. Every rate here applies PER EPOCH, never per unit
// of real time. `epoch.go` states that rule for `EpochBlocks` ("never restate this rate per unit of
// time without naming the interval assumed") and this struct broke it one file away, in the same
// package: `ReserveReleaseBps` was documented as "22 %/year", which is true only under a block
// interval the consensus does not fix — and the interval was MEASURED at ~5 s, not the assumed 1.
//
// The second half of the same defect: the value quoted was the COMPILED default, not what ships. The
// launch genesis carries `reserve_release_bps = 2`, three orders of magnitude below 2200. A comment
// that names one value expires silently the day the genesis names another, and it stays wrong while
// looking authoritative. So the comments below name the UNIT and the SEMANTICS; read the live values
// with `dendrad query emission params -o json`.
type Params struct {
	ReserveReleaseBps uint64 // share of what REMAINS, released once per epoch (decaying curve)
	WorkSplitBps      uint64 // 5000 = 50 % of the release
	AvailSplitBps     uint64 // 2000 = 20 %; SECURITY takes the remainder (about 30 %)
	WorkGateBps       uint64 // 15000 = 1.5x the NON-RECOVERABLE demand (cap on the work subsidy)
}

// DefaultParams returns the shipped parameter set.
func DefaultParams() Params {
	return Params{ReserveReleaseBps: 2200, WorkSplitBps: 5000, AvailSplitBps: 2000, WorkGateBps: 15000}
}

// Release — the outcome of one epoch.
type Release struct {
	Released   uint64 // total taken out of the Reserve this epoch
	Work       uint64 // WORK stream (demand-gated)
	Avail      uint64 // AVAILABILITY stream
	Security   uint64 // SECURITY stream
	NewReserve uint64 // Reserve after the release
}

// mulBps = x * bps / 10000 in integers (deterministic).
func mulBps(x, bps uint64) uint64 { return x * bps / 10000 }

// EpochRelease releases ONE epoch from `reserve` (udndr). `nonRecDemand` (udndr) is the epoch's
// NON-RECOVERABLE demand (burn + treasury + dev); it CAPS the work subsidy, so a miner cannot
// subsidise itself beyond the real demand it does not get back.
func EpochRelease(reserve, nonRecDemand uint64, p Params) Release {
	release := mulBps(reserve, p.ReserveReleaseBps) // the governed share of what remains, this epoch
	workPool := mulBps(release, p.WorkSplitBps)     // 50 %
	avail := mulBps(release, p.AvailSplitBps)       // 20 %
	security := release - workPool - avail          // EXACT remainder (about 30 %): no rounding loss

	work := workPool // demand gate: bound by WorkGate x non-recoverable demand
	if capWork := mulBps(nonRecDemand, p.WorkGateBps); work > capWork {
		work = capWork
	}
	released := work + avail + security // the unused part (workPool - work) STAYS in the Reserve
	return Release{
		Released:   released,
		Work:       work,
		Avail:      avail,
		Security:   security,
		NewReserve: reserve - released,
	}
}
