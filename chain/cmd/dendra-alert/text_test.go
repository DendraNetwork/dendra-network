package main

import (
	"errors"
	"fmt"
	neturl "net/url"
	"strings"
	"testing"
)

// Boundaries of the one-line cleaner and of the error unwrapping.

func TestCleanBoundaries(t *testing.T) {
	if got := clean("\ta\x00b", 10); got != "a b" {
		t.Errorf("control characters must become spaces and the ends must be trimmed: %q", got)
	}
	if got := clean("\x7f", 5); got != "" {
		t.Errorf("a lone control character must trim away to nothing: %q", got)
	}
	if got := clean("", 5); got != "" {
		t.Errorf("an empty value must stay empty: %q", got)
	}
	if got := clean("abcdef", 6); got != "abcdef" {
		t.Errorf("a value of exactly the limit must stay whole: %q", got)
	}
	if got := clean("abcdefg", 6); got != "abcdef..." {
		t.Errorf("one character over the limit must cut to the limit plus the ellipsis: %q", got)
	}
}

func TestPlainErrUnwrapsURLError(t *testing.T) {
	inner := errors.New("connection refused")
	wrapped := &neturl.Error{Op: "Get", URL: "http://127.0.0.1:1/x", Err: inner}
	got := plainErr(wrapped)
	if got != inner {
		t.Errorf("the transport wrapper must go: %v", got)
	}
	if strings.Contains(got.Error(), "127.0.0.1") {
		t.Errorf("the error still carries the address: %v", got)
	}
	if got := plainErr(fmt.Errorf("send: %w", wrapped)); got != inner {
		t.Errorf("the wrapper must be found through other layers too: %v", got)
	}
	if got := plainErr(inner); got != inner {
		t.Errorf("a plain error must pass through unchanged: %v", got)
	}
}

func TestFirstNonEmptyChoosesTheFirstSet(t *testing.T) {
	if got := firstNonEmpty("a", "b"); got != "a" {
		t.Errorf("the first value must win: %q", got)
	}
	if got := firstNonEmpty("", "b"); got != "b" {
		t.Errorf("an empty first value must fall through: %q", got)
	}
	if got := firstNonEmpty("", ""); got != "" {
		t.Errorf("two empty values must stay empty: %q", got)
	}
}
