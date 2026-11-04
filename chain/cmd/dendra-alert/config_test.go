package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// Refusals and small choices of loadConfig and parseSettingsFile that the
// first config tests leave out.

func TestConfigMoreRefusals(t *testing.T) {
	bad := []struct{ name, val string }{
		{setDir, "relative/alert"},
		{setCapacity, "capacity-without-scheme"},
		{setProgramme, "ftp://x"},
		{setPayoutDir, "relative/payout"},
		{setMemMiB, "0"},
		{setKitLeadH, "0"},
		{setSilentH, "0"},
		{setReadS, "0"},
		{setAccepted, "-1"},
		{setAccepted, "1.5"},
		{setSkip, "blocks,payout,more"},
	}
	for _, b := range bad {
		env := map[string]string{"HOME": t.TempDir(), b.name: b.val}
		if _, err := loadConfig(func(k string) string { return env[k] }); err == nil {
			t.Errorf("%s=%q was accepted", b.name, b.val)
		}
	}
	env := map[string]string{
		"HOME": t.TempDir(), setStallS: "1", setMemMiB: "1", setKitLeadH: "1",
		setKitBelowMax: "0", setSilentH: "1", setUnknownN: "1", setRepeatH: "1", setReadS: "1",
	}
	if _, err := loadConfig(func(k string) string { return env[k] }); err != nil {
		t.Errorf("the smallest legal values were refused: %v", err)
	}
}

func TestConfigTrimsTrailingSlashes(t *testing.T) {
	env := map[string]string{
		"HOME":       t.TempDir(),
		setRPC:       "http://127.0.0.1:26657///",
		setProgramme: "http://127.0.0.1:8080/v1/",
	}
	c, err := loadConfig(func(k string) string { return env[k] })
	if err != nil {
		t.Fatal(err)
	}
	if c.rpcURL != "http://127.0.0.1:26657" || c.programme != "http://127.0.0.1:8080/v1" {
		t.Errorf("trailing slashes must go: %q %q", c.rpcURL, c.programme)
	}
}

func TestSettingsFileEmptyValueOverridesDefault(t *testing.T) {
	home := t.TempDir()
	dir := filepath.Join(home, ".dendra-alert")
	os.MkdirAll(dir, 0o700)
	os.WriteFile(filepath.Join(dir, "settings"), []byte(setProject+"=\n"+setService+"=\n"), 0o600)
	env := map[string]string{"HOME": home}
	if _, err := loadConfig(func(k string) string { return env[k] }); err == nil {
		t.Error("an empty project and service in the settings file must be refused")
	}
}

func TestSettingsFileErrors(t *testing.T) {
	home := t.TempDir()
	dir := filepath.Join(home, ".dendra-alert")
	os.MkdirAll(dir, 0o700)
	os.WriteFile(filepath.Join(dir, "settings"), []byte("BOGUS_NAME=1\n"), 0o600)
	env := map[string]string{"HOME": home}
	if _, err := loadConfig(func(k string) string { return env[k] }); err == nil || !strings.Contains(err.Error(), "settings") {
		t.Errorf("an unknown name in the settings file must name the file: %v", err)
	}
	os.Remove(filepath.Join(dir, "settings"))
	os.Mkdir(filepath.Join(dir, "settings"), 0o700)
	if _, err := loadConfig(func(k string) string { return env[k] }); err == nil {
		t.Error("a settings file that cannot be read must stop the load")
	}
}

func TestParseSettingsFileDetails(t *testing.T) {
	m, err := parseSettingsFile("  \n# comment\n  " + setStallS + " = 120 \n")
	if err != nil || m[setStallS] != "120" {
		t.Errorf("names and values must be trimmed: %v %q", err, m)
	}
	m, err = parseSettingsFile(setStallS + "=\"120\n")
	if err != nil || m[setStallS] != "\"120" {
		t.Errorf("a quote with no closing one stays part of the value: %v %q", err, m)
	}
	m, err = parseSettingsFile(setSkip + "=payout,grading\n")
	if err != nil || m[setSkip] != "payout,grading" {
		t.Errorf("the value keeps its commas: %v %q", err, m)
	}
	if _, err := parseSettingsFile("=5\n"); err == nil {
		t.Error("a line with no name was accepted")
	}
}
