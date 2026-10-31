package main

import (
	"context"
	"strings"
	"testing"
	"time"
)

// A block time the clock cannot read, and a catching-up flag on its own.

func TestBlocksBadTimeStaysUsable(t *testing.T) {
	f := newFixture(t)
	st := newState()
	f.rpc = func() (int, string) { return 200, rpcJSON("7", "not-a-time", false) }
	r := f.app.checkBlocks(context.Background(), &st)
	if r.Err != nil || len(r.Findings) != 0 {
		t.Fatalf("a bad block time must leave the check usable: %+v", r)
	}
	if !strings.Contains(r.Note, "unreadable") {
		t.Errorf("the note must say the block time could not be read: %q", r.Note)
	}
	if !st.Blocks.Seen || st.Blocks.Height != 7 {
		t.Errorf("the height must still be recorded so the stall is measured: %+v", st.Blocks)
	}
}

func TestBlocksCatchingUpAloneDoesNotFire(t *testing.T) {
	f := newFixture(t)
	st := newState()
	f.rpc = func() (int, string) { return 200, rpcJSON("11", f.clock.Format(time.RFC3339Nano), true) }
	r := f.app.checkBlocks(context.Background(), &st)
	if r.Err != nil || len(r.Findings) != 0 {
		t.Errorf("a catching-up node with a moving height is fine: %+v", r)
	}
	if !strings.Contains(r.Note, "catching_up=true") {
		t.Errorf("the note must report the flag: %q", r.Note)
	}
}
