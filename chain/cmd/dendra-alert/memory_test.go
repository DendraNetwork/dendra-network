package main

import (
	"context"
	"errors"
	"strings"
	"testing"
)

// The /proc memory line reader, the bounded command read, and the names in a
// chain-down finding.

func TestVmKiBBoundaries(t *testing.T) {
	cases := []struct {
		status  string
		want    int64
		wantErr bool
	}{
		{"VmRSS:\t 0 kB\n", 0, false},
		{"VmRSS:\t 1 kB\n", 1, false},
		{"VmRSS:\t 123456789 kB\n", 123456789, false},
		{"VmRSS:\t5 kB", 5, false},
		{"x\nVmRSS:\t 9 kB\n", 9, false},
		{"VmRSS:\t 1 kB\nVmRSS:\t 2 kB\n", 1, false},
		{"VmRSS:\t -1 kB\n", 0, true},
		{"VmRSS:\t 12 mB\n", 0, true},
		{"VmRSS:\t 12\n", 0, true},
		{"VmRSS:\t 12 kB extra\n", 0, true},
		{"VmRSS:\t twelve kB\n", 0, true},
		{"VmSwap:\t 5 kB\n", 0, true},
		{"VmRSSX:\t 5 kB\n", 0, true},
	}
	for _, c := range cases {
		got, err := vmKiB(c.status, "VmRSS")
		if c.wantErr {
			if err == nil {
				t.Errorf("vmKiB(%q) was accepted", c.status)
			}
			continue
		}
		if err != nil || got != c.want {
			t.Errorf("vmKiB(%q) = %d, %v; want %d", c.status, got, err, c.want)
		}
	}
}

func TestReadCmdWrapsAndTimesOut(t *testing.T) {
	f := newFixture(t)
	f.setConfig(map[string]string{setReadS: "1"})
	f.app.runCmd = func(ctx context.Context, name string, args ...string) ([]byte, error) {
		<-ctx.Done()
		return nil, ctx.Err()
	}
	_, err := f.app.readCmd(context.Background(), "docker", "ps", "-q")
	if err == nil || !strings.Contains(err.Error(), "docker ps") {
		t.Errorf("the error must name the command: %v", err)
	}
	f.app.runCmd = func(ctx context.Context, name string, args ...string) ([]byte, error) {
		return nil, errors.New("boom " + strings.Repeat("x", 300))
	}
	_, err = f.app.readCmd(context.Background(), "docker", "inspect", "abc")
	if err == nil || !strings.Contains(err.Error(), "docker inspect") || !strings.Contains(err.Error(), "boom") {
		t.Errorf("the inner error must survive the wrap: %v", err)
	}
	if len(err.Error()) > 250 {
		t.Errorf("the inner error must be cut down: %d characters", len(err.Error()))
	}
}

func TestMemoryChainDownNamesTheService(t *testing.T) {
	f := newFixture(t)
	dockerFor(f, "", "")
	r := f.app.checkMemory(context.Background())
	if len(r.Findings) != 1 || r.Findings[0].Key != "chain-down" {
		t.Fatalf("no running container must fire chain-down: %+v", r)
	}
	contains(t, "text", r.Findings[0].Text, "service chain")
	contains(t, "text", r.Findings[0].Text, "project dendra")
	contains(t, "read", r.Findings[0].Read, "docker ps -a")
}
