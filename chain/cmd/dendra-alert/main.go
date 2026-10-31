// Command dendra-alert sends the owner's alerts to a private chat channel through a webhook. It runs on
// the chain host from cron, one pass every few minutes, reads the node and the services on that host and
// posts a message when something needs the owner: the block height stops, the node process grows past a
// memory limit, nodes are still below the kit floor shortly before its deadline, a ranked day was left
// without a grade, or the season payout cron raised its alert or went silent.
//
// An alert is posted once when it starts, again when its content changes or after the reminder delay,
// and once more when it ends. What the last pass saw lives in state.json under the state directory
// (default $HOME/.dendra-alert), beside the webhook file, which is read without a default and never
// printed. Every threshold is a named setting; see "dendra-alert help".
//
// usage:
//
//	dendra-alert run      one pass: read, compare with the last pass, post what changed
//	dendra-alert status   read everything and print it; posts nothing and writes no state
//	dendra-alert baseline print the ranked days with no grade that the public status lists now
//	dendra-alert test     post one test message
//	dendra-alert help
package main

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

// app holds everything a pass touches from outside, so that the tests can replace each piece.
type app struct {
	cfg           config
	now           func() time.Time
	client        *http.Client
	runCmd        func(ctx context.Context, name string, args ...string) ([]byte, error)
	procRoot      string
	allowLoopback bool
	out           io.Writer
}

func newApp(cfg config, out io.Writer) *app {
	return &app{
		cfg: cfg,
		now: time.Now,
		client: &http.Client{
			Timeout: time.Duration(cfg.readS) * time.Second,
			// A redirect would carry the request to a host nobody checked.
			CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
		},
		runCmd: func(ctx context.Context, name string, args ...string) ([]byte, error) {
			return exec.CommandContext(ctx, name, args...).Output()
		},
		procRoot: "/proc",
		out:      out,
	}
}

func usage(w io.Writer) {
	fmt.Fprint(w, `usage: dendra-alert run | status | baseline | test | help

  run      one pass: read the node and the services, post the alerts that started, changed or ended
  status   read everything and print it; posts nothing and writes no state
  baseline print the ranked days with no grade that the public status lists now, as
           ungraded_ranked_days=0,1,6 (nothing is written; install stores the list as the accepted days)
  test     post one test message to the webhook

The webhook is read from the file "webhook" of the state directory (mode 600) and from nowhere else.
Settings come from the environment first, then from the file "settings" of the state directory
(NAME=VALUE lines), then from the defaults below.

`)
	fmt.Fprint(w, settingsHelp())
}

func main() {
	if len(os.Args) != 2 {
		usage(os.Stderr)
		os.Exit(2)
	}
	if os.Args[1] == "help" || os.Args[1] == "-h" || os.Args[1] == "--help" {
		usage(os.Stdout)
		return
	}
	cfg, err := loadConfig(os.Getenv)
	if err != nil {
		fmt.Fprintln(os.Stderr, "dendra-alert: not run:", err)
		os.Exit(2)
	}
	a := newApp(cfg, os.Stdout)
	var code int
	switch os.Args[1] {
	case "run":
		code = a.cmdRun()
	case "status":
		code = a.cmdStatus()
	case "baseline":
		code = a.cmdBaseline()
	case "test":
		code = a.cmdTest()
	default:
		usage(os.Stderr)
		code = 2
	}
	os.Exit(code)
}

// logf writes a line to stdout and to alert.log (rotated at 1 MiB) with a UTC timestamp.
func (a *app) logf(format string, args ...any) {
	line := a.now().UTC().Format("2006-01-02T15:04:05Z") + " " + fmt.Sprintf(format, args...)
	fmt.Fprintln(a.out, line)
	p := filepath.Join(a.cfg.dir, "alert.log")
	if fi, err := os.Stat(p); err == nil && fi.Size() > 1<<20 {
		os.Rename(p, p+".1")
	}
	f, err := os.OpenFile(p, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o600)
	if err != nil {
		return
	}
	defer f.Close()
	fmt.Fprintln(f, line)
}

type namedResult struct {
	name string
	res  checkResult
}

// evaluate runs the checks that are not skipped, in a fixed order. st.Blocks is updated.
func (a *app) evaluate(ctx context.Context, st *alertState) []namedResult {
	var out []namedResult
	for _, name := range checkNames {
		if a.cfg.skip[name] {
			continue
		}
		var r checkResult
		switch name {
		case "blocks":
			r = a.checkBlocks(ctx, st)
		case "memory":
			r = a.checkMemory(ctx)
		case "kit":
			r = a.checkKit(ctx)
		case "grading":
			r = a.checkGrading(ctx)
		case "payout":
			r = a.checkPayout()
		}
		out = append(out, namedResult{name, r})
	}
	return out
}

func (a *app) cmdTest() int {
	if err := os.MkdirAll(a.cfg.dir, 0o700); err != nil {
		fmt.Fprintln(os.Stderr, "dendra-alert: not run:", err)
		return 2
	}
	hook, err := readWebhook(a.cfg.dir, a.allowLoopback)
	if err != nil {
		fmt.Fprintln(os.Stderr, "dendra-alert: not run:", err)
		return 2
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(a.cfg.readS)*time.Second)
	defer cancel()
	if err := a.send(ctx, hook, "[dendra-alert] test message sent at "+a.now().UTC().Format("2006-01-02 15:04:05")+" UTC. If you read this, the webhook works."); err != nil {
		a.logf("test: %v", err)
		return 1
	}
	a.logf("test: message delivered")
	return 0
}

// cmdBaseline prints the ranked days with no grade that the public status lists now. install stores them as the
// accepted days, so that only a day that appears later raises the grading alert. It writes nothing.
func (a *app) cmdBaseline() int {
	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(a.cfg.readS)*time.Second)
	defer cancel()
	days, err := a.readUngradedDays(ctx)
	if err != nil {
		fmt.Fprintln(os.Stderr, "dendra-alert: baseline not read:", err)
		return 1
	}
	fmt.Fprintln(a.out, "ungraded_ranked_days="+joinDays(days))
	return 0
}

// acceptedLine shows the accepted days in effect, for the status command.
func (a *app) acceptedLine() string {
	days := make([]int, 0, len(a.cfg.accepted))
	for d := range a.cfg.accepted {
		days = append(days, d)
	}
	sort.Ints(days)
	if len(days) == 0 {
		return "none (every ranked day with no grade raises the grading alert)"
	}
	return joinDays(days)
}

func (a *app) cmdStatus() int {
	bad := false
	if _, err := readWebhook(a.cfg.dir, a.allowLoopback); err != nil {
		fmt.Fprintln(a.out, "webhook: not ready:", err)
		bad = true
	} else {
		fmt.Fprintln(a.out, "webhook: ready")
	}
	st, note, err := peekState(a.cfg.dir)
	if err != nil {
		fmt.Fprintln(a.out, "state: not readable:", err)
		bad = true
	}
	if note != "" {
		fmt.Fprintln(a.out, "state:", note)
		bad = true
	}
	fmt.Fprintf(a.out, "baseline  accepted ranked days with no grade: %s\n", a.acceptedLine())
	ctx := context.Background()
	for _, nr := range a.evaluate(ctx, &st) {
		switch {
		case nr.res.Err != nil:
			fmt.Fprintf(a.out, "%-8s unreadable  %v\n", nr.name, nr.res.Err)
			bad = true
		case len(nr.res.Findings) > 0:
			for _, f := range nr.res.Findings {
				fmt.Fprintf(a.out, "%-8s firing      %s\n", nr.name, f.Text)
				fmt.Fprintf(a.out, "%-8s read        %s\n", "", f.Read)
			}
			bad = true
		default:
			fmt.Fprintf(a.out, "%-8s ok          %s\n", nr.name, nr.res.Note)
		}
	}
	keys := make([]string, 0, len(st.Alerts))
	for k := range st.Alerts {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		r := st.Alerts[k]
		fmt.Fprintf(a.out, "posted   %s since %s, last message %s\n", k,
			time.Unix(r.Since, 0).UTC().Format("2006-01-02 15:04"), time.Unix(r.LastSent, 0).UTC().Format("2006-01-02 15:04"))
	}
	if bad {
		return 1
	}
	return 0
}

// peekState reads the state without setting a damaged file aside: status changes nothing.
func peekState(dir string) (alertState, string, error) {
	b, err := os.ReadFile(statePath(dir))
	if os.IsNotExist(err) {
		return newState(), "", nil
	}
	if err != nil {
		return newState(), "", err
	}
	st := newState()
	if err := decodeState(b, &st); err != nil {
		return newState(), "state.json does not parse (" + err.Error() + "); the next run sets it aside", nil
	}
	return st, "", nil
}

func (a *app) cmdRun() int {
	if err := os.MkdirAll(a.cfg.dir, 0o700); err != nil {
		fmt.Fprintln(os.Stderr, "dendra-alert: not run:", err)
		return 2
	}
	hook, err := readWebhook(a.cfg.dir, a.allowLoopback)
	if err != nil {
		a.logf("not run: %v", err)
		return 2
	}
	unlock, err := lockDir(a.cfg.dir)
	if err != nil {
		if err == errLocked {
			a.logf("another pass holds the lock: this one stops")
			return 0
		}
		a.logf("not run: %v", err)
		return 2
	}
	defer unlock()
	st, note, err := loadState(a.cfg.dir, a.now())
	if err != nil {
		a.logf("not run: %v", err)
		return 2
	}
	if note != "" {
		a.logf("%s", note)
	}
	ctx := context.Background()
	results := a.evaluate(ctx, &st)
	failed := a.reconcile(ctx, hook, &st, results)
	if err := saveState(a.cfg.dir, st); err != nil {
		a.logf("cannot save the state: %v", err)
		return 2
	}
	if failed {
		return 1
	}
	return 0
}

// reconcile compares what the checks found with the alerts on record, posts the differences and
// updates st. A message that could not be delivered leaves its record as it was, so that the next
// pass tries again. It reports whether any delivery failed.
func (a *app) reconcile(ctx context.Context, hook string, st *alertState, results []namedResult) bool {
	failed := false
	now := a.now().Unix()
	repeat := int64(a.cfg.repeatH) * 3600
	post := func(text string) bool {
		c, cancel := context.WithTimeout(ctx, time.Duration(a.cfg.readS)*time.Second)
		defer cancel()
		if err := a.send(c, hook, text); err != nil {
			a.logf("%v", err)
			failed = true
			return false
		}
		return true
	}
	ran := map[string]bool{}
	for _, nr := range results {
		ran[nr.name] = true
		r := nr.res
		firing := map[string]finding{}
		if r.Err != nil {
			st.Unknown[nr.name]++
			a.logf("%s: cannot read: %v (%d of %d passes)", nr.name, r.Err, st.Unknown[nr.name], a.cfg.unknownN)
			if st.Unknown[nr.name] >= a.cfg.unknownN {
				k := nr.name + "-unreadable"
				firing[k] = finding{Key: k, Fingerprint: "unreadable",
					Text: fmt.Sprintf("The %s check could not read its source for %d passes in a row: %s", nr.name, st.Unknown[nr.name], clean(r.Err.Error(), 300)),
					Read: "dendra-alert status"}
			}
		} else {
			st.Unknown[nr.name] = 0
			a.logf("%s: %s", nr.name, firstNonEmpty(r.Note, "ok"))
			for _, f := range r.Findings {
				firing[f.Key] = f
			}
		}
		keys := make([]string, 0, len(firing))
		for k := range firing {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		for _, k := range keys {
			f := firing[k]
			rec, known := st.Alerts[k]
			var prefix string
			switch {
			case !known:
				prefix = "firing"
			case rec.Fingerprint != f.Fingerprint:
				prefix = "changed"
			case now-rec.LastSent >= repeat:
				prefix = "still firing"
			default:
				continue
			}
			if post(fmt.Sprintf("[dendra-alert] %s %s\n%s\nRead: %s", prefix, k, f.Text, f.Read)) {
				if !known {
					rec = alertRecord{Check: nr.name, Since: now}
				}
				rec.Fingerprint, rec.LastSent = f.Fingerprint, now
				st.Alerts[k] = rec
				a.logf("%s %s", strings.ToLower(prefix), k)
			}
		}
		if r.Err != nil {
			continue // an unreadable check leaves the alerts it raised before as they are
		}
		var ended []string
		for k, rec := range st.Alerts {
			if rec.Check == nr.name {
				if _, still := firing[k]; !still {
					ended = append(ended, k)
				}
			}
		}
		sort.Strings(ended)
		for _, k := range ended {
			if post(fmt.Sprintf("[dendra-alert] resolved %s\nThe condition no longer holds (%s).", k, firstNonEmpty(r.Note, "read ok"))) {
				delete(st.Alerts, k)
				a.logf("resolved %s", k)
			}
		}
	}
	// A check the owner left out no longer speaks: its records go without a message.
	for k, rec := range st.Alerts {
		if !ran[rec.Check] {
			delete(st.Alerts, k)
			a.logf("dropped %s: the %s check is left out", k, rec.Check)
		}
	}
	return failed
}

func firstNonEmpty(a, b string) string {
	if a != "" {
		return a
	}
	return b
}
