package main

import (
	"os"
	"path/filepath"
	"testing"
)

// The reader of the compose .env file, away from the kit check that calls it.

func TestReadEnvKeysParsing(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, ".env")
	if _, err := readEnvKeys(filepath.Join(dir, "absent"), "K"); err == nil {
		t.Error("a missing file must be an error")
	}
	write := func(text string) map[string]string {
		t.Helper()
		if err := os.WriteFile(p, []byte(text), 0o600); err != nil {
			t.Fatal(err)
		}
		m, err := readEnvKeys(p, "K")
		if err != nil {
			t.Fatalf("readEnvKeys(%q): %v", text, err)
		}
		return m
	}
	if m := write("K=1\nK=2\n"); m["K"] != "2" {
		t.Errorf("the last assignment must win: %q", m["K"])
	}
	if m := write("# K=9\n\nK=3\r\n"); m["K"] != "3" {
		t.Errorf("comments and blank lines must be skipped and a carriage return dropped: %q", m["K"])
	}
	if m := write("K = spaced \n"); m["K"] != "spaced" {
		t.Errorf("the name and the value must be trimmed: %q", m["K"])
	}
	if m := write("K='q v'\n"); m["K"] != "q v" {
		t.Errorf("single quotes must go: %q", m["K"])
	}
	if m := write("K=\"d v\" # c\n"); m["K"] != "d v" {
		t.Errorf("double quotes must go before an inline comment: %q", m["K"])
	}
	if m := write("K=v # c\n"); m["K"] != "v" {
		t.Errorf("an inline comment on a bare value must go: %q", m["K"])
	}
	if m := write("K=v# c\n"); m["K"] != "v# c" {
		t.Errorf("a hash with no space before it belongs to the value: %q", m["K"])
	}
	if m := write("K=\"unclosed\n"); m["K"] != "\"unclosed" {
		t.Errorf("a quote with no closing one belongs to the value: %q", m["K"])
	}
	m := write("OTHER=1\nK=\n")
	if v, ok := m["K"]; !ok || v != "" {
		t.Errorf("a key set to an empty value must be present and empty: %q %v", v, ok)
	}
	if _, ok := m["OTHER"]; ok {
		t.Error("a key nobody asked for must stay out")
	}
	if m := write("K\n"); len(m) != 0 {
		t.Errorf("a line without an equals sign must be skipped: %q", m["K"])
	}
	if m := write("K=a b\n"); m["K"] != "a b" {
		t.Errorf("an inner space belongs to the value: %q", m["K"])
	}
}
