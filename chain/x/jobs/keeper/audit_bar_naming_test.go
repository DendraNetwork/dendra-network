package keeper_test

import (
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/keeper"
	"github.com/DendraNetwork/dendra-network/chain/x/jobs/types"
)

// The absolute floor must never become a "committee size" again.
//
// Correct arithmetic is not enough. The seat bar is relative to the anchored seats in both branches, so
// the computation is right; the hazard is the name of the constant feeding the absolute floor. That
// constant equals 5 while the draw produces 15. Under the name `AuditCommitteeSize` it claimed to
// describe the committee size, and a threshold whose name claims to be current is never re-read, so its
// staleness stays invisible. It is named `AuditLegacyFloorBasis`, which claims nothing about the
// committee.
//
// A drifting reference can be a name as well as a line number, and a name gets copied into every new
// site by whoever read it in good faith. Fixing the computation without fixing the name would leave
// the next occurrence ready: whoever writes the next bar reads the constant, understands "the
// committee is 5", and reproduces the fault.
//
// These tests keep the fault from returning:
//   (1) the misleading name no longer exists anywhere;
//   (2) the constant has exactly one consumer, the absolute floor, so it cannot become the
//       denominator of a bar again without this test saying so;
//   (3) absolute floor and relative bar remain two distinct quantities, and the second really depends
//       on the seats (a bar that ignores its argument is the original defect).

// packageSources returns the production .go files of the keeper package (tests are excluded: they
// legitimately quote the old vocabulary when they describe a historical case).
func packageSources(t *testing.T) map[string]string {
	t.Helper()
	files, err := filepath.Glob("*.go")
	require.NoError(t, err)
	out := map[string]string{}
	for _, f := range files {
		if strings.HasSuffix(f, "_test.go") {
			continue
		}
		b, e := os.ReadFile(f)
		require.NoError(t, e)
		out[f] = string(b)
	}
	require.GreaterOrEqual(t, len(out), 10,
		"almost no source read: the glob is broken and this check would measure nothing")
	return out
}

// withoutComments strips `//...` from each line, so the count below sees only what the compiler sees.
// A comment naming the constant does not consume it, and prose has to name it to explain why its use
// is restricted; counting prose mentions as usages would make the guard fire on its own documentation.
func withoutComments(src string) string {
	var b strings.Builder
	for _, l := range strings.Split(src, "\n") {
		if i := strings.Index(l, "//"); i >= 0 {
			l = l[:i]
		}
		b.WriteString(l)
		b.WriteString("\n")
	}
	return b.String()
}

// (1) The misleading name is dead, including in comments. Letting it live in prose is enough for it to
// be copied: it spreads through reading, not through compilation.
func TestBarNamingNoReturnOfTheMisleadingName(t *testing.T) {
	for name, src := range packageSources(t) {
		require.NotContains(t, src, "AuditCommitteeSize",
			"%s resurrects `AuditCommitteeSize`: that name says \"audit committee size\" and equals 5, "+
				"while the draw produces %d. A name like that is what hides 4/15 = 27 %%",
			name, keeper.AuditCommitteeDrawSizeForTest)
	}
}

// (2) One consumer. As long as the constant only feeds `auditSlashFloor`, it cannot slip into the
// denominator of a bar. A second use is not necessarily a fault, but it must be seen and decided
// rather than appear by itself.
func TestNamingFloorHasASingleConsumer(t *testing.T) {
	decl := regexp.MustCompile(`(?m)^const AuditLegacyFloorBasis\b`)
	foundDecl := false
	usages := []string{}
	for name, src := range packageSources(t) {
		if decl.MatchString(src) {
			foundDecl = true
		}
		for _, l := range strings.Split(withoutComments(src), "\n") {
			if !strings.Contains(l, "AuditLegacyFloorBasis") {
				continue
			}
			if strings.HasPrefix(strings.TrimSpace(l), "const AuditLegacyFloorBasis") {
				continue // the declaration itself
			}
			usages = append(usages, name+" : "+strings.TrimSpace(l))
		}
	}
	require.True(t, foundDecl, "the constant has disappeared — this check no longer guards anything")
	require.Len(t, usages, 1,
		"the absolute floor must have exactly one consumer (`auditSlashFloor`). Usages found: %v. "+
			"A second site reading this constant is the start of the next occurrence: it will believe it "+
			"is reading a committee size", usages)
	require.Contains(t, usages[0], "auditSlashFloor",
		"the single consumer must be the absolute floor, not a bar")
}

// (3) Two quantities, not one, and the relative one really depends on its argument.
//
// The original defect is "a bar that does not use the seats". A test comparing only numbers could stay
// green on a bar that became constant again, so the bar is checked to move with the seats, and never to
// fall below the floor.
func TestNamingAbsoluteFloorAndRelativeBarAreDistinct(t *testing.T) {
	require.Equal(t, 4, keeper.AuditSlashFloorForTest(),
		"absolute floor = ceil(5/2)+1: if it changes, every governed threshold must be re-read")
	require.Less(t, keeper.AuditLegacyFloorBasis, keeper.AuditCommitteeDrawSizeForTest,
		"The floor basis (%d) is smaller than the draw (%d) — that gap is precisely what made an absolute "+
			"threshold a minority one. It is acceptable, but only because the seat bar is relative; if these "+
			"two numbers ever coincide again, re-read auditRelativeBar",
		keeper.AuditLegacyFloorBasis, keeper.AuditCommitteeDrawSizeForTest)

	p := types.DefaultParams()
	previous := 0
	for _, seats := range []int{0, 5, 9, 15, 30} {
		bar := keeper.AuditRelativeBarForTest(p, seats)
		require.GreaterOrEqual(t, bar, keeper.AuditSlashFloorForTest(),
			"the relative bar never falls below the absolute floor (seats=%d)", seats)
		require.GreaterOrEqual(t, bar, previous,
			"the bar must grow with the seats — a constant bar is the original defect")
		previous = bar
	}
	require.Greater(t, keeper.AuditRelativeBarForTest(p, 30), keeper.AuditRelativeBarForTest(p, 5),
		"witness: at 30 seats the bar must exceed the one at 5 seats. If they are identical, "+
			"the bar ignores its argument — exactly the state this guard exists to prevent")
}
