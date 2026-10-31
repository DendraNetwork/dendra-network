// Command dendra-exporter turns the miner-health reports of a miner kit into metrics for
// Prometheus, so a miner operator watches the Dendra miner on the same screens as the rest of
// the machine.
//
// It reads report files and runs nothing: the files are given by a repeatable -file flag and by
// -glob (one file per slot of a machine with several cards), and are read again at each scrape,
// so a new report shows at the next scrape. It serves them at /metrics, in the Prometheus text
// format, on the loopback address by default; every other path answers 404.
//
// A field that a report leaves out, writes null, or writes with the wrong type is not a zero:
// its series is left out. A read that fails (a missing file, a file past 1 MiB, bad JSON) raises
// dendra_miner_health_read_errors_total for that file.
package main

import (
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

// defaultListen is the loopback address: the read-error counter carries file paths, so the
// metrics reach the network only when the operator asks for it with -listen.
const defaultListen = "127.0.0.1:9478"

func usage(w io.Writer) {
	fmt.Fprint(w, `usage: dendra-exporter -file <path> [-file <path> ...] [-glob <pattern> ...] [-listen <host:port>]

  -file   one report file, miner-health.last.json (repeatable)
  -glob   a pattern matching one report file per slot, such as
          <kit folder>/gpu/*/miner-health.last.json (repeatable, matched again at each scrape)
  -listen where /metrics is served (default 127.0.0.1:9478)

At least one -file or -glob is required. The files are read at each scrape, never cached.
`)
}

// pathList collects the values of a repeatable flag.
type pathList struct {
	vals *[]string
}

func (p pathList) String() string { return strings.Join(*p.vals, ", ") }

func (p pathList) Set(v string) error {
	*p.vals = append(*p.vals, v)
	return nil
}

// options holds what the command line asked for.
type options struct {
	files  []string
	globs  []string
	listen string
}

// errNoSource is returned when neither -file nor -glob was given.
var errNoSource = errors.New("at least one -file or -glob is required")

// parseArgs reads the command line. A start with no report source, or with a glob pattern that
// is malformed, is refused here, so a typo shows at start-up and not at the first scrape. What
// the user must read is printed to stderr before the error comes back.
func parseArgs(args []string, stderr io.Writer) (options, error) {
	var o options
	fs := flag.NewFlagSet("dendra-exporter", flag.ContinueOnError)
	fs.SetOutput(stderr)
	fs.Usage = func() { usage(stderr) }
	fs.StringVar(&o.listen, "listen", defaultListen, "address where /metrics is served")
	fs.Var(pathList{vals: &o.files}, "file", "one report file, miner-health.last.json (repeatable)")
	fs.Var(pathList{vals: &o.globs}, "glob", "a pattern matching one report file per slot (repeatable)")
	if err := fs.Parse(args); err != nil {
		return options{}, err
	}
	if len(o.files) == 0 && len(o.globs) == 0 {
		usage(stderr)
		return options{}, errNoSource
	}
	for _, g := range o.globs {
		if _, err := filepath.Glob(g); err != nil {
			fmt.Fprintf(stderr, "dendra-exporter: -glob %q: %v\n", g, err)
			return options{}, err
		}
	}
	return o, nil
}

// exporter serves the metrics of one set of report files.
type exporter struct {
	files    []string
	globs    []string
	readErrs map[string]int64
	logf     func(string, ...any)
	mu       sync.Mutex
}

// expandPaths lists the files of this scrape: the -file paths first, then the matches of each
// -glob in order, a path seen twice only once.
func (e *exporter) expandPaths() []string {
	seen := map[string]bool{}
	var out []string
	add := func(p string) {
		if p == "" || seen[p] {
			return
		}
		seen[p] = true
		out = append(out, p)
	}
	for _, p := range e.files {
		add(p)
	}
	for _, g := range e.globs {
		ms, err := filepath.Glob(g)
		if err != nil {
			// The pattern was checked at start-up; a file that appears or leaves is not an error.
			e.logf("glob %s: %v", g, err)
			continue
		}
		for _, p := range ms {
			add(p)
		}
	}
	return out
}

// handleMetrics reads the report files and writes the exposition text. One scrape at a time: the
// read-error counter belongs to this process and grows across scrapes.
func (e *exporter) handleMetrics(w http.ResponseWriter, r *http.Request) {
	e.mu.Lock()
	defer e.mu.Unlock()
	slots := gather(e.expandPaths(), e.readErrs, e.logf)
	w.Header().Set("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
	io.WriteString(w, render(slots, e.readErrs))
}

// newMux serves /metrics and nothing else: every other path answers 404.
func newMux(e *exporter) *http.ServeMux {
	mux := http.NewServeMux()
	mux.HandleFunc("/metrics", e.handleMetrics)
	return mux
}

func main() {
	o, err := parseArgs(os.Args[1:], os.Stderr)
	if err != nil {
		if errors.Is(err, flag.ErrHelp) {
			os.Exit(0)
		}
		os.Exit(2)
	}
	logf := func(format string, args ...any) {
		fmt.Fprintf(os.Stderr, "%s dendra-exporter: %s\n",
			time.Now().UTC().Format("2006-01-02T15:04:05Z"), fmt.Sprintf(format, args...))
	}
	e := &exporter{files: o.files, globs: o.globs, readErrs: map[string]int64{}, logf: logf}
	logf("serving /metrics on %s (%d file flag(s), %d glob flag(s))", o.listen, len(o.files), len(o.globs))
	// The header timeout keeps a client that never finishes its request from holding a connection.
	srv := &http.Server{Addr: o.listen, Handler: newMux(e), ReadHeaderTimeout: 10 * time.Second}
	if err := srv.ListenAndServe(); err != nil {
		logf("cannot serve on %s: %v", o.listen, err)
		os.Exit(1)
	}
}
