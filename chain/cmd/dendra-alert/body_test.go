package main

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"
)

// The size limit of a read and the refusal of a URL the request cannot carry.

func TestGetBodySizeLimit(t *testing.T) {
	f := newFixture(t)
	st := newState()
	ctx := context.Background()
	raw := rpcJSON("100", f.clock.Format(time.RFC3339Nano), false)
	pad := strings.Repeat("a", maxBody-len(raw)-9)
	exact := `{"pad":"` + pad + `",` + raw[1:]
	if len(exact) != maxBody {
		t.Fatalf("the test body is %d bytes, want exactly %d", len(exact), maxBody)
	}
	f.rpc = func() (int, string) { return 200, exact }
	if r := f.app.checkBlocks(ctx, &st); r.Err != nil {
		t.Errorf("a body of exactly the limit must be read: %v", r.Err)
	}
	f.rpc = func() (int, string) { return 200, exact + "a" }
	r := f.app.checkBlocks(ctx, &st)
	if r.Err == nil || !errors.Is(r.Err, errTooLarge) {
		t.Errorf("one byte over the limit must stop the read: %v", r.Err)
	}
}

func TestGetBodyRefusesABadURL(t *testing.T) {
	f := newFixture(t)
	if _, err := f.app.getBody(context.Background(), "http://127.0.0.1:1/\x00"); err == nil || !strings.Contains(err.Error(), "bad request") {
		t.Errorf("a URL with a control character must be refused before any request: %v", err)
	}
}
