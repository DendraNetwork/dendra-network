package main

import (
	"bufio"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

// setting is one named parameter. A value comes from the process environment first, then from the
// settings file in the state directory, then from def. "{home}" in def stands for $HOME.
type setting struct {
	name string
	def  string
	doc  string
}

const (
	setDir         = "DENDRA_ALERT_DIR"
	setRPC         = "DENDRA_ALERT_RPC_URL"
	setCapacity    = "DENDRA_ALERT_CAPACITY_URL"
	setProgramme   = "DENDRA_ALERT_PROGRAMME_URL"
	setEnvFile     = "DENDRA_ALERT_ENV_FILE"
	setPayoutDir   = "DENDRA_ALERT_PAYOUT_DIR"
	setProject     = "DENDRA_ALERT_COMPOSE_PROJECT"
	setService     = "DENDRA_ALERT_CHAIN_SERVICE"
	setStallS      = "DENDRA_ALERT_STALL_S"
	setMemMiB      = "DENDRA_ALERT_CHAIN_MEM_MIB"
	setKitLeadH    = "DENDRA_ALERT_KIT_LEAD_H"
	setKitBelowMax = "DENDRA_ALERT_KIT_BELOW_MAX"
	setSilentH     = "DENDRA_ALERT_PAYOUT_SILENT_H"
	setAccepted    = "DENDRA_ALERT_ACCEPTED_UNGRADED_DAYS"
	setUnknownN    = "DENDRA_ALERT_UNKNOWN_PASSES"
	setRepeatH     = "DENDRA_ALERT_REPEAT_H"
	setSkip        = "DENDRA_ALERT_SKIP_CHECKS"
	setReadS       = "DENDRA_ALERT_READ_TIMEOUT_S"
)

var settingDefs = []setting{
	{setDir, "{home}/.dendra-alert", "state directory: webhook, settings, state.json, alert.log, run.lock"},
	{setRPC, "http://127.0.0.1:26657", "RPC of the chain node on this host"},
	{setCapacity, "http://127.0.0.1:8092/capacity", "capacity registry on this host (verified.kit_versions)"},
	{setProgramme, "https://testnet-api.dendranetwork.com/final-season/v1",
		"season programme API, as the grading cron reads it (status.ungraded_ranked_days)"},
	{setEnvFile, "{home}/dendra/.env", "compose .env that carries DENDRA_KIT_MIN and DENDRA_KIT_DEADLINE"},
	{setPayoutDir, "{home}/.dendra-season-payout", "directory of the season payout cron (season-payout.alert, season-payout.log)"},
	{setProject, "dendra", "compose project name of the chain host"},
	{setService, "chain", "compose service name of the node"},
	{setStallS, "900", "seconds the block height may stay still before the blocks alert fires"},
	{setMemMiB, "2400", "MiB of resident memory plus swap of dendrad above which the memory alert fires"},
	{setKitLeadH, "72", "hours before DENDRA_KIT_DEADLINE from which the adoption check runs"},
	{setKitBelowMax, "0", "verified live nodes below DENDRA_KIT_MIN tolerated before the adoption alert fires"},
	{setSilentH, "7", "hours without a line in season-payout.log before the payout cron counts as silent"},
	{setAccepted, "", "ranked days with no grade that the owner accepted (install writes the days the public status lists then), separated by commas or spaces"},
	{setUnknownN, "3", "consecutive passes a check may fail to read before an unreadable alert fires"},
	{setRepeatH, "12", "hours between two reminders of an alert that stays firing"},
	{setSkip, "", "checks to leave out, separated by commas: blocks, memory, kit, grading, payout"},
	{setReadS, "20", "seconds a read of the node, a service or docker may take"},
}

var checkNames = []string{"blocks", "memory", "kit", "grading", "payout"}

type config struct {
	home      string
	dir       string
	rpcURL    string
	capacity  string
	programme string
	envFile   string
	payoutDir string
	project   string
	service   string
	stallS    int
	memMiB    int
	kitLeadH  int
	kitBelow  int
	silentH   int
	accepted  map[int]bool
	unknownN  int
	repeatH   int
	skip      map[string]bool
	readS     int
	fromFile  map[string]string
}

// resolveHome returns $HOME. A missing or relative home is an error: the state directory has no
// sensible place to default to without it.
func resolveHome(getenv func(string) string) (string, error) {
	h := getenv("HOME")
	if h == "" || !filepath.IsAbs(h) {
		return "", fmt.Errorf("HOME is not set to an absolute path (read %q)", h)
	}
	return filepath.Clean(h), nil
}

// parseSettingsFile reads NAME=VALUE lines. A name that is not a setting is an error, so a typo is
// never taken for a value.
func parseSettingsFile(text string) (map[string]string, error) {
	known := map[string]bool{}
	for _, s := range settingDefs {
		known[s.name] = true
	}
	out := map[string]string{}
	sc := bufio.NewScanner(strings.NewReader(text))
	n := 0
	for sc.Scan() {
		n++
		line := strings.TrimSpace(sc.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		name, val, ok := strings.Cut(line, "=")
		name = strings.TrimSpace(name)
		if !ok || !known[name] {
			return nil, fmt.Errorf("settings line %d: %q is not NAME=VALUE with a known name", n, name)
		}
		if name == setDir {
			return nil, fmt.Errorf("settings line %d: %s cannot be set in the settings file it lives in", n, name)
		}
		val = strings.TrimSpace(val)
		if len(val) >= 2 && (val[0] == '"' || val[0] == '\'') && val[len(val)-1] == val[0] {
			val = val[1 : len(val)-1]
		}
		out[name] = val
	}
	return out, sc.Err()
}

func loadConfig(getenv func(string) string) (config, error) {
	var c config
	home, err := resolveHome(getenv)
	if err != nil {
		return c, err
	}
	c.home = home
	dir := getenv(setDir)
	if dir == "" {
		dir = filepath.Join(home, ".dendra-alert")
	}
	if !filepath.IsAbs(dir) {
		return c, fmt.Errorf("%s must be an absolute path (read %q)", setDir, dir)
	}
	c.dir = filepath.Clean(dir)
	c.fromFile = map[string]string{}
	if b, err := os.ReadFile(filepath.Join(c.dir, "settings")); err == nil {
		m, perr := parseSettingsFile(string(b))
		if perr != nil {
			return c, fmt.Errorf("%s: %w", filepath.Join(c.dir, "settings"), perr)
		}
		c.fromFile = m
	} else if !os.IsNotExist(err) {
		return c, fmt.Errorf("cannot read settings: %w", err)
	}
	get := func(name string) string {
		if v := getenv(name); v != "" {
			return v
		}
		if v, ok := c.fromFile[name]; ok {
			return v
		}
		for _, s := range settingDefs {
			if s.name == name {
				return strings.ReplaceAll(s.def, "{home}", home)
			}
		}
		return ""
	}
	atLeast := func(name string, min int) (int, error) {
		raw := get(name)
		v, err := strconv.Atoi(raw)
		if err != nil || v < min {
			return 0, fmt.Errorf("%s must be a whole number of at least %d (read %q)", name, min, raw)
		}
		return v, nil
	}
	c.rpcURL = strings.TrimRight(get(setRPC), "/")
	c.capacity = get(setCapacity)
	c.programme = strings.TrimRight(get(setProgramme), "/")
	c.envFile = get(setEnvFile)
	c.payoutDir = get(setPayoutDir)
	c.project = get(setProject)
	c.service = get(setService)
	for _, p := range []struct{ n, v string }{{setRPC, c.rpcURL}, {setCapacity, c.capacity}, {setProgramme, c.programme}} {
		if !strings.HasPrefix(p.v, "http://") && !strings.HasPrefix(p.v, "https://") {
			return c, fmt.Errorf("%s must be an http or https URL (read %q)", p.n, p.v)
		}
	}
	for _, p := range []struct{ n, v string }{{setEnvFile, c.envFile}, {setPayoutDir, c.payoutDir}} {
		if !filepath.IsAbs(p.v) {
			return c, fmt.Errorf("%s must be an absolute path (read %q)", p.n, p.v)
		}
	}
	if c.project == "" || c.service == "" {
		return c, fmt.Errorf("%s and %s must not be empty", setProject, setService)
	}
	if c.stallS, err = atLeast(setStallS, 1); err != nil {
		return c, err
	}
	if c.memMiB, err = atLeast(setMemMiB, 1); err != nil {
		return c, err
	}
	if c.kitLeadH, err = atLeast(setKitLeadH, 1); err != nil {
		return c, err
	}
	if c.kitBelow, err = atLeast(setKitBelowMax, 0); err != nil {
		return c, err
	}
	if c.silentH, err = atLeast(setSilentH, 1); err != nil {
		return c, err
	}
	if c.unknownN, err = atLeast(setUnknownN, 1); err != nil {
		return c, err
	}
	if c.repeatH, err = atLeast(setRepeatH, 1); err != nil {
		return c, err
	}
	if c.readS, err = atLeast(setReadS, 1); err != nil {
		return c, err
	}
	c.accepted = map[int]bool{}
	for _, f := range strings.FieldsFunc(get(setAccepted), func(r rune) bool { return r == ',' || r == ' ' }) {
		d, err := strconv.Atoi(f)
		if err != nil || d < 0 {
			return c, fmt.Errorf("%s holds %q, which is not a day number", setAccepted, f)
		}
		c.accepted[d] = true
	}
	c.skip = map[string]bool{}
	valid := map[string]bool{}
	for _, n := range checkNames {
		valid[n] = true
	}
	for _, f := range strings.FieldsFunc(get(setSkip), func(r rune) bool { return r == ',' || r == ' ' }) {
		if !valid[f] {
			return c, fmt.Errorf("%s names %q, which is not a check (%s)", setSkip, f, strings.Join(checkNames, ", "))
		}
		c.skip[f] = true
	}
	return c, nil
}

// settingsHelp lists every parameter with its default, in the order of settingDefs.
func settingsHelp() string {
	var b strings.Builder
	for _, s := range settingDefs {
		def := strings.ReplaceAll(s.def, "{home}", "$HOME")
		if def == "" {
			def = "(empty)"
		}
		fmt.Fprintf(&b, "  %-38s %s\n      %s\n", s.name, def, s.doc)
	}
	return b.String()
}
