package main

import (
	"context"
	"strings"
	"testing"
)

// The fixed order of the checks, and the checks the owner left out.

func TestEvaluateRunsInOrderAndHonorsSkip(t *testing.T) {
	f := newFixture(t)
	f.healthy()
	names := func(results []namedResult) string {
		var list []string
		for _, nr := range results {
			list = append(list, nr.name)
		}
		return strings.Join(list, ",")
	}
	st := newState()
	got := f.app.evaluate(context.Background(), &st)
	if names(got) != "blocks,memory,kit,grading,payout" {
		t.Fatalf("the checks did not run in the fixed order: %s", names(got))
	}
	for _, nr := range got {
		if nr.res.Err != nil {
			t.Errorf("%s: a healthy host must read: %v", nr.name, nr.res.Err)
		}
	}
	f.setConfig(map[string]string{setSkip: "blocks,payout"})
	got = f.app.evaluate(context.Background(), &st)
	if names(got) != "memory,kit,grading" {
		t.Errorf("the skipped checks must stay out: %s", names(got))
	}
}
