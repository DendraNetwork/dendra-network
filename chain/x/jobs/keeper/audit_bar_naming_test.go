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

// THE ABSOLUTE FLOOR MUST NEVER BECOME A "COMMITTEE SIZE" AGAIN
//
// WHAT THIS FILE GUARDS, AND WHY CORRECT ARITHMETIC IS NOT ENOUGH.
//
// The seat bar is relative to the ANCHORED seats in both branches, so the computation is correct. The
// hazard is the NAME of the constant feeding the ABSOLUTE floor. That constant equals 5 while the draw
// produces 15; under the name `AuditCommitteeSize` it claimed to describe the committee size, and a
// threshold whose name claims to be current is never re-read, so the staleness stays invisible. It is
// named `AuditLegacyFloorBasis`, which claims nothing about the committee.
//
// A reference that drifts is not always a line number: it can be a NAME — and a name gets copied into
// every new site, with the clear conscience of whoever read it. Fixing the computation without fixing
// the name leaves the next occurrence ARMED: whoever writes the next bar reads the constant,
// understands "the committee is 5", and reproduces the fault cleanly.
//
// These tests forbid the fault from returning through its ENTRY POINT:
//   (1) the misleading name no longer exists anywhere;
//   (2) the constant has exactly ONE consumer — the absolute floor — so it cannot become the
//       denominator of a bar again without this test saying so;
//   (3) absolute floor and relative bar remain TWO distinct quantities, and the second really depends
//       on the seats (a bar that ignores its argument is the original defect).

// sourcesDuPaquet returns the production .go files of the keeper package (tests are excluded: they
// legitimately quote the old vocabulary when they describe a historical case).
func sourcesDuPaquet(t *testing.T) map[string]string {
	t.Helper()
	fichiers, err := filepath.Glob("*.go")
	require.NoError(t, err)
	out := map[string]string{}
	for _, f := range fichiers {
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

// sansCommentaires strips `//...` from each line, so the count below sees only what the compiler
// sees. A comment naming the constant does not CONSUME it, and prose has to name it to explain why
// its use is restricted; counting prose mentions as usages would make the guard fire on its own
// documentation.
func sansCommentaires(src string) string {
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

// (1) THE MISLEADING NAME IS DEAD — including in comments. Letting it live in prose is enough for it
// to be copied: it propagates through reading, not through compilation.
func TestNommagePlancherAucunRetourDuNomTrompeur(t *testing.T) {
	for nom, src := range sourcesDuPaquet(t) {
		require.NotContains(t, src, "AuditCommitteeSize",
			"%s resurrects `AuditCommitteeSize`: that name says \"audit committee size\" and equals 5, "+
				"while the draw produces %d. A name like that is what hides 4/15 = 27 %%",
			nom, keeper.AuditCommitteeDrawSizeForTest)
	}
}

// (2) ONE SINGLE CONSUMER. As long as the constant only feeds `auditSlashFloor`, it cannot slip into
// the denominator of a bar. A second use is not necessarily a fault — but it must be SEEN and decided,
// not appear by itself.
func TestNommagePlancherUnSeulConsommateur(t *testing.T) {
	decl := regexp.MustCompile(`(?m)^const AuditLegacyFloorBasis\b`)
	trouveDecl := false
	usages := []string{}
	for nom, src := range sourcesDuPaquet(t) {
		if decl.MatchString(src) {
			trouveDecl = true
		}
		for _, l := range strings.Split(sansCommentaires(src), "\n") {
			if !strings.Contains(l, "AuditLegacyFloorBasis") {
				continue
			}
			if strings.HasPrefix(strings.TrimSpace(l), "const AuditLegacyFloorBasis") {
				continue // the declaration itself
			}
			usages = append(usages, nom+" : "+strings.TrimSpace(l))
		}
	}
	require.True(t, trouveDecl, "the constant has disappeared — this check no longer guards anything")
	require.Len(t, usages, 1,
		"the absolute floor must have EXACTLY ONE consumer (`auditSlashFloor`). Usages found: %v. "+
			"A second site reading this constant is the start of the next occurrence: it will believe it "+
			"is reading a committee size", usages)
	require.Contains(t, usages[0], "auditSlashFloor",
		"the single consumer must be the absolute floor, not a bar")
}

// (3) TWO QUANTITIES, NOT ONE — and the relative one really depends on its argument.
//
// The original defect is exactly "a bar that does not use the seats". A test comparing only numbers
// could stay green on a bar that became constant again; so the bar is checked to MOVE with the seats,
// and never to fall below the floor.
func TestNommagePlancherAbsoluEtBarreRelativeSontDistincts(t *testing.T) {
	require.Equal(t, 4, keeper.AuditSlashFloorForTest(),
		"absolute floor = ceil(5/2)+1: if it changes, every governed threshold must be re-read")
	require.Less(t, keeper.AuditLegacyFloorBasis, keeper.AuditCommitteeDrawSizeForTest,
		"THE FLOOR BASIS (%d) IS SMALLER THAN THE DRAW (%d) — that gap is precisely what made an absolute "+
			"threshold a minority one. It is ACCEPTABLE, but only because the seat bar is relative; if these "+
			"two numbers ever coincide again, re-read auditRelativeBar",
		keeper.AuditLegacyFloorBasis, keeper.AuditCommitteeDrawSizeForTest)

	p := types.DefaultParams()
	precedent := 0
	for _, sieges := range []int{0, 5, 9, 15, 30} {
		barre := keeper.AuditRelativeBarForTest(p, sieges)
		require.GreaterOrEqual(t, barre, keeper.AuditSlashFloorForTest(),
			"the relative bar never falls below the absolute floor (seats=%d)", sieges)
		require.GreaterOrEqual(t, barre, precedent,
			"the bar must GROW with the seats — a constant bar is the original defect")
		precedent = barre
	}
	require.Greater(t, keeper.AuditRelativeBarForTest(p, 30), keeper.AuditRelativeBarForTest(p, 5),
		"WITNESS: at 30 seats the bar must exceed the one at 5 seats. If they are identical, "+
			"the bar ignores its argument — exactly the state this guard exists to prevent")
}
