package main

import (
	"bytes"
	"errors"
	"flag"
	"fmt"
	"go/parser"
	"go/token"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
)

// The tests read documents as constants and files under a temporary directory; nothing here talks
// to the network beyond a test server on the loopback address.

// oneSlotJSON is a sample report of one slot, its fields at the top. twoSlotsJSON is the form that
// miner_health.sh writes with --json for a machine of two identities: a slots array with one envelope
// {slot, project, rc, doc} per slot, doc being the document of that slot. bareTwoSlotsJSON holds the
// same documents without their envelope: the script never writes that form, and the exporter reads it
// all the same.
const oneSlotJSON = `{"schema":1,"tool":"miner_health","generated_at":"2026-10-25T06:00:01Z","generated_epoch":1792908001,"schedule_period_s":3600,"rc":1,"verdict":"a problem is standing","kit":"/srv/dendra-kit","slot":0,"project":"dendra-miner","quick":false,"no_write":false,"note":"","summary":{"ok":5,"ko":1,"unmeasured":1,"checks":7},"host":[{"id":"H1","name":"containers","state":"ok","measured":"miner and node running","reason":"","fix":""},{"id":"H2","name":"image","state":"ok","measured":"pinned image of the release","reason":"","fix":""},{"id":"H3","name":"chain sync","state":"ko","measured":"node 312 blocks behind","reason":"the node is catching up","fix":"wait, then run the check again"},{"id":"H4","name":"presence","state":"ok","measured":"last proof 2 windows ago","reason":"","fix":""},{"id":"H5","name":"schedule","state":"ok","measured":"hourly","reason":"","fix":""},{"id":"H6","name":"GPU pinning","state":"unmeasured","measured":"no GPU tool in reach","reason":"nvidia-smi not found","fix":""},{"id":"C0","name":"self-test (C1-C10)","state":"ok","measured":"10 of 10","reason":"","fix":""}],"selftest_rc":0,"selftest":{"rc":0},"judge_role":{"word":"active","why":"the judge worker runs and holds the pinned model"},"advice":[]}`

const twoSlotsJSON = `{"schema":1,"tool":"miner_health","generated_at":"2026-10-25T06:00:03Z","generated_epoch":1792908003,"schedule_period_s":3600,"rc":2,"verdict":"not everything could be measured","kit":"/srv/dendra-kit","quick":false,"no_write":false,"note":"2 identities, one per card: one document per slot","summary":{"ok":11,"ko":0,"unmeasured":1,"checks":12},"host":[],"selftest_rc":null,"selftest":null,"slots":[{"slot":0,"project":"dendra-miner","rc":0,"doc":{"schema":1,"tool":"miner_health","generated_at":"2026-10-25T06:00:02Z","generated_epoch":1792908002,"schedule_period_s":3600,"rc":0,"verdict":"OK","kit":"/srv/dendra-kit","slot":0,"project":"dendra-miner","quick":false,"no_write":false,"note":"","summary":{"ok":6,"ko":0,"unmeasured":0,"checks":6},"host":[{"id":"H1","name":"containers","state":"ok","measured":"miner and node running","reason":"","fix":""},{"id":"H2","name":"image","state":"ok","measured":"pinned image of the release","reason":"","fix":""},{"id":"H4","name":"presence","state":"ok","measured":"last proof this window","reason":"","fix":""},{"id":"H5","name":"schedule","state":"ok","measured":"hourly","reason":"","fix":""},{"id":"H6","name":"GPU pinning","state":"ok","measured":"card 0","reason":"","fix":""},{"id":"C0","name":"self-test (C1-C10)","state":"ok","measured":"10 of 10","reason":"","fix":""}],"selftest_rc":0,"selftest":{"rc":0},"judge_role":{"word":"off","why":"the judge role was not requested"},"advice":[]}},{"slot":1,"project":"dendra-miner-g1","rc":2,"doc":{"schema":1,"tool":"miner_health","generated_at":"2026-10-25T06:00:03Z","generated_epoch":1792908003,"schedule_period_s":3600,"rc":2,"verdict":"not everything could be measured","kit":"/srv/dendra-kit","slot":1,"project":"dendra-miner-g1","quick":false,"no_write":false,"note":"","summary":{"ok":5,"ko":0,"unmeasured":1,"checks":6},"host":[{"id":"H1","name":"containers","state":"ok","measured":"miner running","reason":"","fix":""},{"id":"H2","name":"image","state":"ok","measured":"pinned image of the release","reason":"","fix":""},{"id":"H4","name":"presence","state":"ok","measured":"last proof this window","reason":"","fix":""},{"id":"H5","name":"schedule","state":"ok","measured":"hourly","reason":"","fix":""},{"id":"H6","name":"GPU pinning","state":"ok","measured":"card 1","reason":"","fix":""},{"id":"C0","name":"self-test \"quick\" (C1-C10)","state":"unmeasured","measured":"skipped","reason":"no running miner answered","fix":""}],"selftest_rc":null,"selftest":null,"judge_role":null,"advice":[]}}]}`

const bareTwoSlotsJSON = `{"schema":1,"tool":"miner_health","generated_at":"2026-10-25T06:00:03Z","generated_epoch":1792908003,"schedule_period_s":3600,"rc":2,"verdict":"not everything could be measured","kit":"/srv/dendra-kit","quick":false,"no_write":false,"note":"2 identities, one per card: one document per slot","summary":{"ok":11,"ko":0,"unmeasured":1,"checks":12},"host":[],"selftest_rc":null,"selftest":null,"slots":[{"schema":1,"tool":"miner_health","generated_at":"2026-10-25T06:00:02Z","generated_epoch":1792908002,"schedule_period_s":3600,"rc":0,"verdict":"OK","kit":"/srv/dendra-kit","slot":0,"project":"dendra-miner","quick":false,"no_write":false,"note":"","summary":{"ok":6,"ko":0,"unmeasured":0,"checks":6},"host":[{"id":"H1","name":"containers","state":"ok","measured":"miner and node running","reason":"","fix":""},{"id":"H2","name":"image","state":"ok","measured":"pinned image of the release","reason":"","fix":""},{"id":"H4","name":"presence","state":"ok","measured":"last proof this window","reason":"","fix":""},{"id":"H5","name":"schedule","state":"ok","measured":"hourly","reason":"","fix":""},{"id":"H6","name":"GPU pinning","state":"ok","measured":"card 0","reason":"","fix":""},{"id":"C0","name":"self-test (C1-C10)","state":"ok","measured":"10 of 10","reason":"","fix":""}],"selftest_rc":0,"selftest":{"rc":0},"judge_role":{"word":"off","why":"the judge role was not requested"},"advice":[]},{"schema":1,"tool":"miner_health","generated_at":"2026-10-25T06:00:03Z","generated_epoch":1792908003,"schedule_period_s":3600,"rc":2,"verdict":"not everything could be measured","kit":"/srv/dendra-kit","slot":1,"project":"dendra-miner-g1","quick":false,"no_write":false,"note":"","summary":{"ok":5,"ko":0,"unmeasured":1,"checks":6},"host":[{"id":"H1","name":"containers","state":"ok","measured":"miner running","reason":"","fix":""},{"id":"H2","name":"image","state":"ok","measured":"pinned image of the release","reason":"","fix":""},{"id":"H4","name":"presence","state":"ok","measured":"last proof this window","reason":"","fix":""},{"id":"H5","name":"schedule","state":"ok","measured":"hourly","reason":"","fix":""},{"id":"H6","name":"GPU pinning","state":"ok","measured":"card 1","reason":"","fix":""},{"id":"C0","name":"self-test \"quick\" (C1-C10)","state":"unmeasured","measured":"skipped","reason":"no running miner answered","fix":""}],"selftest_rc":null,"selftest":null,"judge_role":null,"advice":[]}]}`

// checkExposition walks an exposition text: every sample line must carry a name, an optional
// label set and one number at the end, and must have a type line of its own.
func checkExposition(t *testing.T, text string) {
	t.Helper()
	types := map[string]string{}
	for _, line := range strings.Split(text, "\n") {
		if line == "" || strings.HasPrefix(line, "# ") {
			if strings.HasPrefix(line, "# TYPE ") {
				f := strings.Fields(strings.TrimPrefix(line, "# TYPE "))
				if len(f) != 2 || (f[1] != "gauge" && f[1] != "counter") {
					t.Errorf("bad type line: %s", line)
					continue
				}
				types[f[0]] = f[1]
			}
			continue
		}
		name := line
		if i := strings.IndexAny(line, "{ "); i >= 0 {
			name = line[:i]
		}
		if types[name] == "" {
			t.Errorf("sample without a type line: %s", line)
			continue
		}
		if _, err := strconv.ParseFloat(line[strings.LastIndexByte(line, ' ')+1:], 64); err != nil {
			t.Errorf("sample without a number at the end: %s", line)
		}
	}
}

type docCase struct {
	name string
	doc  string
	want []string
	omit []string
	// none lists metric names that must have no sample at all in the text.
	none []string
}

// TestDocuments renders each document and checks the text line by line: every want line appears,
// in the order of the text, no omit line appears, and no none metric has a sample.
func TestDocuments(t *testing.T) {
	cases := []docCase{
		{
			name: "one slot, the sample report",
			doc:  oneSlotJSON,
			want: []string{
				`dendra_miner_health_up{slot="0"} 1`,
				`dendra_miner_health_rc{slot="0"} 1`,
				`dendra_miner_health_checks{slot="0",state="ok"} 5`,
				`dendra_miner_health_checks{slot="0",state="ko"} 1`,
				`dendra_miner_health_checks{slot="0",state="unmeasured"} 1`,
				`dendra_miner_health_check{slot="0",id="C0",name="self-test (C1-C10)"} 0`,
				`dendra_miner_health_check{slot="0",id="H1",name="containers"} 0`,
				`dendra_miner_health_check{slot="0",id="H2",name="image"} 0`,
				`dendra_miner_health_check{slot="0",id="H3",name="chain sync"} 1`,
				`dendra_miner_health_check{slot="0",id="H4",name="presence"} 0`,
				`dendra_miner_health_check{slot="0",id="H5",name="schedule"} 0`,
				`dendra_miner_health_check{slot="0",id="H6",name="GPU pinning"} 2`,
				`dendra_miner_health_generated_timestamp_seconds{slot="0"} 1792908001`,
				`dendra_miner_health_schedule_period_seconds{slot="0"} 3600`,
				`dendra_miner_health_selftest_rc{slot="0"} 0`,
				`dendra_miner_health_judge_role{slot="0",word="active"} 1`,
			},
		},
		{
			name: "two slots, the sample report",
			doc:  twoSlotsJSON,
			want: []string{
				`dendra_miner_health_up{slot="0"} 1`,
				`dendra_miner_health_up{slot="1"} 1`,
				`dendra_miner_health_rc{slot="0"} 0`,
				`dendra_miner_health_rc{slot="1"} 2`,
				`dendra_miner_health_checks{slot="0",state="ok"} 6`,
				`dendra_miner_health_checks{slot="1",state="ok"} 5`,
				`dendra_miner_health_checks{slot="1",state="unmeasured"} 1`,
				`dendra_miner_health_check{slot="0",id="H6",name="GPU pinning"} 0`,
				`dendra_miner_health_check{slot="1",id="C0",name="self-test \"quick\" (C1-C10)"} 2`,
				`dendra_miner_health_generated_timestamp_seconds{slot="0"} 1792908002`,
				`dendra_miner_health_generated_timestamp_seconds{slot="1"} 1792908003`,
				`dendra_miner_health_schedule_period_seconds{slot="0"} 3600`,
				`dendra_miner_health_selftest_rc{slot="0"} 0`,
				`dendra_miner_health_judge_role{slot="0",word="off"} 1`,
			},
			omit: []string{
				// the machine summary and the machine rc belong to no slot
				`dendra_miner_health_checks{slot="0",state="ok"} 11`,
				`dendra_miner_health_rc{slot="0"} 2`,
				// slot 1 writes selftest_rc null and judge_role null
				`dendra_miner_health_selftest_rc{slot="1"} 0`,
				`dendra_miner_health_judge_role{slot="1",word="off"} 1`,
			},
		},
		{
			name: "a report with nothing but the rc is slot 0",
			doc:  `{"rc":0}`,
			want: []string{
				`dendra_miner_health_up{slot="0"} 1`,
				`dendra_miner_health_rc{slot="0"} 0`,
			},
			omit: []string{
				`dendra_miner_health_generated_timestamp_seconds{slot="0"} 0`,
				`dendra_miner_health_schedule_period_seconds{slot="0"} 0`,
				`dendra_miner_health_selftest_rc{slot="0"} 0`,
				`dendra_miner_health_checks{slot="0",state="ok"} 0`,
			},
		},
		{
			name: "a field of the wrong type is treated as absent",
			doc:  `{"rc":"1","slot":"0","generated_epoch":"1792908001","schedule_period_s":"3600","selftest_rc":"0","judge_role":"active","summary":{"ok":"5","ko":1},"host":[{"id":"H1","name":"containers","state":"ok"},{"id":1,"name":"a wrong id"},{"id":"H2"}]}`,
			want: []string{
				`dendra_miner_health_up{slot="0"} 1`,
				`dendra_miner_health_checks{slot="0",state="ko"} 1`,
				`dendra_miner_health_check{slot="0",id="H1",name="containers"} 0`,
			},
			omit: []string{
				`dendra_miner_health_rc{slot="0"} 1`,
				`dendra_miner_health_generated_timestamp_seconds{slot="0"} 1792908001`,
				`dendra_miner_health_schedule_period_seconds{slot="0"} 3600`,
				`dendra_miner_health_selftest_rc{slot="0"} 0`,
				`dendra_miner_health_checks{slot="0",state="ok"} 5`,
				// a check needs both its labels as strings; one without them has no series
				`dendra_miner_health_check{slot="0",id="H2",name=""} 2`,
				`dendra_miner_health_check{slot="0",id="1",name="a wrong id"} 2`,
			},
		},
		{
			name: "null where a number is expected",
			doc:  `{"slot":0,"rc":null,"generated_epoch":null,"schedule_period_s":null,"selftest_rc":null,"judge_role":null,"summary":null,"host":null}`,
			want: []string{
				`dendra_miner_health_up{slot="0"} 1`,
			},
			omit: []string{
				`dendra_miner_health_rc{slot="0"} 0`,
				`dendra_miner_health_generated_timestamp_seconds{slot="0"} 0`,
				`dendra_miner_health_schedule_period_seconds{slot="0"} 0`,
				`dendra_miner_health_selftest_rc{slot="0"} 0`,
				`dendra_miner_health_checks{slot="0",state="ok"} 0`,
			},
		},
		{
			name: "a name with a double quote, a backslash and a line feed is escaped",
			doc:  `{"slot":0,"host":[{"id":"Q1","name":"a \"quoted\" \\ name","state":"ko"},{"id":"Q2","name":"two\nlines","state":"ok"}]}`,
			want: []string{
				`dendra_miner_health_up{slot="0"} 1`,
				`dendra_miner_health_check{slot="0",id="Q1",name="a \"quoted\" \\ name"} 1`,
				`dendra_miner_health_check{slot="0",id="Q2",name="two\nlines"} 0`,
			},
		},
		{
			name: "a slot of the array is broken while the other one is fine",
			doc:  `{"slots":[{"slot":0,"rc":2,"doc":null},{"slot":1,"rc":0,"summary":{"ok":1,"ko":0,"unmeasured":0,"checks":1}}]}`,
			want: []string{
				`dendra_miner_health_up{slot="0"} 0`,
				`dendra_miner_health_up{slot="1"} 1`,
				`dendra_miner_health_rc{slot="1"} 0`,
				`dendra_miner_health_checks{slot="1",state="ok"} 1`,
			},
			omit: []string{
				`dendra_miner_health_rc{slot="0"} 2`,
				`dendra_miner_health_checks{slot="0",state="ko"} 1`,
			},
		},
		{
			name: "the wrapper of the machine run, with its document",
			doc:  `{"slots":[{"slot":2,"project":"dendra-miner-2","rc":1,"doc":{"slot":2,"rc":0,"summary":{"ok":1,"ko":0,"unmeasured":0,"checks":1},"host":[{"id":"H1","name":"containers","state":"ok"}]}},{"slot":3,"rc":0,"doc":{"rc":0}}]}`,
			want: []string{
				`dendra_miner_health_up{slot="2"} 1`,
				`dendra_miner_health_up{slot="3"} 1`,
				`dendra_miner_health_rc{slot="2"} 0`,
				`dendra_miner_health_rc{slot="3"} 0`,
				`dendra_miner_health_checks{slot="2",state="ok"} 1`,
				`dendra_miner_health_check{slot="2",id="H1",name="containers"} 0`,
			},
			omit: []string{
				// the rc of the wrapper loses against the rc of the document
				`dendra_miner_health_rc{slot="2"} 1`,
			},
		},
		{
			name: "the slot of the document wins over the slot of its wrapper, which is the fallback",
			doc:  `{"slots":[{"slot":9,"doc":{"slot":4,"rc":0}},{"slot":6,"doc":{"rc":1}}]}`,
			want: []string{
				`dendra_miner_health_up{slot="4"} 1`,
				`dendra_miner_health_up{slot="6"} 1`,
				`dendra_miner_health_rc{slot="4"} 0`,
				`dendra_miner_health_rc{slot="6"} 1`,
			},
			omit: []string{`dendra_miner_health_up{slot="9"} 1`},
		},
		{
			name: "a slots element with no slot number anywhere is left out",
			doc:  `{"slots":["broken",{"rc":0},{"slot":5,"rc":0}]}`,
			want: []string{
				`dendra_miner_health_up{slot="5"} 1`,
				`dendra_miner_health_rc{slot="5"} 0`,
			},
			omit: []string{
				`dendra_miner_health_up{slot="0"} 1`,
				`dendra_miner_health_up{slot="0"} 0`,
			},
		},
		{
			name: "a slot that is not a whole number reads as absent, so the slot is 0",
			doc:  `{"slot":1.5,"rc":0}`,
			want: []string{
				`dendra_miner_health_up{slot="0"} 1`,
				`dendra_miner_health_rc{slot="0"} 0`,
			},
			omit: []string{
				`dendra_miner_health_up{slot="1"} 1`,
			},
		},
		{
			name: "a slots field of the wrong type falls back to one slot at the top",
			doc:  `{"slots":"x","rc":0}`,
			want: []string{
				`dendra_miner_health_up{slot="0"} 1`,
				`dendra_miner_health_rc{slot="0"} 0`,
			},
		},
		{
			name: "an empty slots array serves no slot",
			doc:  `{"slots":[],"rc":0}`,
			omit: []string{
				`dendra_miner_health_up{slot="0"} 1`,
				`dendra_miner_health_up{slot="0"} 0`,
			},
		},
		{
			name: "a judge role with a null word, no word, or a word of the wrong type has no series",
			doc:  `{"slots":[{"slot":0,"judge_role":{"word":null}},{"slot":1,"judge_role":{}},{"slot":2,"judge_role":{"word":3}},{"slot":3,"judge_role":[]}]}`,
			want: []string{
				`dendra_miner_health_up{slot="0"} 1`,
				`dendra_miner_health_up{slot="3"} 1`,
			},
			none: []string{"dendra_miner_health_judge_role"},
		},
		{
			name: "a check state that is absent, unknown or of the wrong type counts as unmeasured",
			doc:  `{"slot":0,"host":[{"id":"A","name":"no state"},{"id":"B","name":"odd","state":"weird"},{"id":"C","name":"upper","state":"OK"},{"id":"D","name":"number","state":1},{"id":"E","name":"null","state":null},{"id":"F","name":"named","state":"unmeasured"}]}`,
			want: []string{
				`dendra_miner_health_check{slot="0",id="A",name="no state"} 2`,
				`dendra_miner_health_check{slot="0",id="B",name="odd"} 2`,
				`dendra_miner_health_check{slot="0",id="C",name="upper"} 2`,
				`dendra_miner_health_check{slot="0",id="D",name="number"} 2`,
				`dendra_miner_health_check{slot="0",id="E",name="null"} 2`,
				`dendra_miner_health_check{slot="0",id="F",name="named"} 2`,
			},
		},
		{
			name: "a number that does not fit a float64 is treated as absent",
			doc:  `{"slot":0,"rc":1e999,"generated_epoch":-1e999,"summary":{"ok":1e999,"ko":2}}`,
			want: []string{
				`dendra_miner_health_up{slot="0"} 1`,
				`dendra_miner_health_checks{slot="0",state="ko"} 2`,
			},
			none: []string{
				"dendra_miner_health_rc",
				"dendra_miner_health_generated_timestamp_seconds",
			},
			omit: []string{`dendra_miner_health_checks{slot="0",state="ok"} 0`},
		},
		{
			name: "a check listed twice serves one series, the first entry",
			doc:  `{"slot":0,"host":[{"id":"H1","name":"containers","state":"ko"},{"id":"H1","name":"containers","state":"ok"}]}`,
			want: []string{
				`dendra_miner_health_check{slot="0",id="H1",name="containers"} 1`,
			},
			omit: []string{`dendra_miner_health_check{slot="0",id="H1",name="containers"} 0`},
		},
	}
	// The documents without their envelope give the same series: the exporter reads that form too.
	for _, c := range cases {
		if c.name == "two slots, the sample report" {
			bare := c
			bare.name = "two slots, the documents without their envelope"
			bare.doc = bareTwoSlotsJSON
			cases = append(cases, bare)
		}
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			slots, err := parseFile([]byte(c.doc))
			if err != nil {
				t.Fatalf("parseFile: %v", err)
			}
			text := render(slots, map[string]int64{})
			checkExposition(t, text)
			lines := strings.Split(text, "\n")
			pos := 0
			for _, w := range c.want {
				found := false
				for i := pos; i < len(lines); i++ {
					if lines[i] == w {
						pos, found = i+1, true
						break
					}
				}
				if !found {
					t.Errorf("line missing or out of order: %s", w)
				}
			}
			for _, o := range c.omit {
				if strings.Contains(text, "\n"+o+"\n") {
					t.Errorf("line must not appear: %s", o)
				}
			}
			for _, name := range c.none {
				for _, line := range lines {
					if strings.HasPrefix(line, name+"{") || strings.HasPrefix(line, name+" ") {
						t.Errorf("%s must have no sample, got: %s", name, line)
					}
				}
			}
		})
	}
}

func TestGatherFiles(t *testing.T) {
	dir := t.TempDir()
	good := filepath.Join(dir, "good.json")
	if err := os.WriteFile(good, []byte(oneSlotJSON), 0o600); err != nil {
		t.Fatal(err)
	}
	big := filepath.Join(dir, "big.json")
	if err := os.WriteFile(big, []byte(strings.Repeat("a", maxReportBytes+1)), 0o600); err != nil {
		t.Fatal(err)
	}
	bad := filepath.Join(dir, "bad.json")
	if err := os.WriteFile(bad, []byte("not json"), 0o600); err != nil {
		t.Fatal(err)
	}
	missing := filepath.Join(dir, "missing.json")
	var logs []string
	logf := func(format string, args ...any) {
		logs = append(logs, fmt.Sprintf(format, args...))
	}
	paths := []string{good, missing, big, bad}
	readErrs := map[string]int64{}
	slots := gather(paths, readErrs, logf)
	if len(slots) != 1 || slots[0].slot != 0 || !slots[0].up {
		t.Fatalf("the good file must give slot 0 up, got %+v", slots)
	}
	for _, p := range paths[1:] {
		if readErrs[p] != 1 {
			t.Errorf("%s counted %d read errors, want 1", p, readErrs[p])
		}
	}
	if _, ok := readErrs[good]; ok {
		t.Error("the good file must not count a read error")
	}
	text := render(slots, readErrs)
	// The label value escapes the backslashes a Windows path carries.
	if !strings.Contains(text, `dendra_miner_health_read_errors_total{file="`+escapeLabel(big)+`"} 1`) {
		t.Errorf("the counter of the large file is missing from:\n%s", text)
	}
	// A second scrape of the same files grows the counter of the failing reads.
	gather(paths, readErrs, logf)
	if readErrs[missing] != 2 {
		t.Errorf("the missing file counted %d read errors after two scrapes, want 2", readErrs[missing])
	}
	if !strings.Contains(strings.Join(logs, "\n"), missing) {
		t.Errorf("the log lines do not name the missing file:\n%s", strings.Join(logs, "\n"))
	}
}

func TestGatherSameSlotTwice(t *testing.T) {
	dir := t.TempDir()
	a := filepath.Join(dir, "a.json")
	b := filepath.Join(dir, "b.json")
	for _, p := range []string{a, b} {
		if err := os.WriteFile(p, []byte(oneSlotJSON), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	slots := gather([]string{a, b}, map[string]int64{}, func(string, ...any) {})
	if len(slots) != 1 {
		t.Fatalf("two files of slot 0 gave %d slots, want 1", len(slots))
	}
}

func TestExpandPaths(t *testing.T) {
	dir := t.TempDir()
	a := filepath.Join(dir, "slot0.json")
	b := filepath.Join(dir, "slot1.json")
	for _, p := range []string{a, b} {
		if err := os.WriteFile(p, []byte(oneSlotJSON), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	e := &exporter{
		files: []string{a},
		globs: []string{filepath.Join(dir, "*.json")},
		logf:  func(string, ...any) {},
	}
	got := e.expandPaths()
	want := []string{a, b}
	if len(got) != len(want) {
		t.Fatalf("expandPaths gave %q, want %q", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("expandPaths[%d] = %q, want %q", i, got[i], want[i])
		}
	}
}

func TestHandler(t *testing.T) {
	dir := t.TempDir()
	good := filepath.Join(dir, "miner-health.last.json")
	if err := os.WriteFile(good, []byte(oneSlotJSON), 0o600); err != nil {
		t.Fatal(err)
	}
	e := &exporter{
		files:    []string{good},
		readErrs: map[string]int64{},
		logf:     func(string, ...any) {},
	}
	// The mux main serves: /metrics and nothing else.
	srv := httptest.NewServer(newMux(e))
	defer srv.Close()
	// A client without a proxy: the test server lives on the loopback address.
	client := &http.Client{Transport: &http.Transport{Proxy: nil}}
	resp, err := client.Get(srv.URL + "/metrics")
	if err != nil {
		t.Fatal(err)
	}
	body, err := io.ReadAll(resp.Body)
	resp.Body.Close()
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != 200 {
		t.Fatalf("/metrics answered %d, want 200", resp.StatusCode)
	}
	if ct := resp.Header.Get("Content-Type"); ct != "text/plain; version=0.0.4; charset=utf-8" {
		t.Errorf("Content-Type = %q", ct)
	}
	if !strings.Contains(string(body), `dendra_miner_health_up{slot="0"} 1`) {
		t.Errorf("the body holds no up sample:\n%s", body)
	}
	checkExposition(t, string(body))

	for _, path := range []string{"/", "/anything-else", "/metrics/", "/metrics/x", "/metric", "/healthz"} {
		other, err := client.Get(srv.URL + path)
		if err != nil {
			t.Fatal(err)
		}
		other.Body.Close()
		if other.StatusCode != 404 {
			t.Errorf("%s answered %d, want 404", path, other.StatusCode)
		}
	}
}

// writeReport writes a report under a directory and returns its path.
func writeReport(t *testing.T, dir, name, doc string) string {
	t.Helper()
	p := filepath.Join(dir, name)
	if err := os.WriteFile(p, []byte(doc), 0o600); err != nil {
		t.Fatal(err)
	}
	return p
}

// scrape runs one request through the handler, without a network.
func scrape(t *testing.T, e *exporter) string {
	t.Helper()
	rec := httptest.NewRecorder()
	newMux(e).ServeHTTP(rec, httptest.NewRequest("GET", "/metrics", nil))
	if rec.Code != 200 {
		t.Fatalf("/metrics answered %d, want 200", rec.Code)
	}
	return rec.Body.String()
}

// TestReadAtEachScrape checks that nothing is cached: a report that changes, a file that appears
// under a glob and a file that disappears all show at the next scrape.
func TestReadAtEachScrape(t *testing.T) {
	dir := t.TempDir()
	first := writeReport(t, dir, "slot0.json", `{"slot":0,"rc":0}`)
	e := &exporter{
		files:    []string{first},
		globs:    []string{filepath.Join(dir, "gpu-*.json")},
		readErrs: map[string]int64{},
		logf:     func(string, ...any) {},
	}
	text := scrape(t, e)
	if !strings.Contains(text, "\ndendra_miner_health_rc{slot=\"0\"} 0\n") {
		t.Fatalf("first scrape misses rc 0:\n%s", text)
	}
	writeReport(t, dir, "slot0.json", `{"slot":0,"rc":2}`)
	writeReport(t, dir, "gpu-1.json", `{"slot":1,"rc":1}`)
	text = scrape(t, e)
	for _, want := range []string{
		"\ndendra_miner_health_rc{slot=\"0\"} 2\n",
		"\ndendra_miner_health_rc{slot=\"1\"} 1\n",
	} {
		if !strings.Contains(text, want) {
			t.Errorf("second scrape misses %q in:\n%s", strings.TrimSpace(want), text)
		}
	}
	if err := os.Remove(first); err != nil {
		t.Fatal(err)
	}
	text = scrape(t, e)
	if strings.Contains(text, "dendra_miner_health_up{slot=\"0\"}") {
		t.Errorf("a removed report still shows:\n%s", text)
	}
	if !strings.Contains(text, "\ndendra_miner_health_read_errors_total{file=\""+escapeLabel(first)+"\"} 1\n") {
		t.Errorf("a removed report must count one read error:\n%s", text)
	}
	// The counter belongs to the process: a second failing scrape adds one.
	text = scrape(t, e)
	if !strings.Contains(text, "\ndendra_miner_health_read_errors_total{file=\""+escapeLabel(first)+"\"} 2\n") {
		t.Errorf("the read-error counter must grow across scrapes:\n%s", text)
	}
}

// paddedReport returns a valid report document of exactly size bytes, so that only its size
// can make a reader refuse it.
func paddedReport(size int) []byte {
	head, tail := `{"slot":0,"rc":0,"note":"`, `"}`
	pad := size - len(head) - len(tail)
	return []byte(head + strings.Repeat("x", pad) + tail)
}

// TestSizeLimit pins the limit at 1 MiB: a valid report of exactly 1 MiB is read, one byte more
// is refused and counted as a read error. The sizes are written out, not taken from the constant.
func TestSizeLimit(t *testing.T) {
	const oneMiB = 1048576
	dir := t.TempDir()
	atLimit := writeReport(t, dir, "at.json", string(paddedReport(oneMiB)))
	pastLimit := writeReport(t, dir, "past.json", string(paddedReport(oneMiB+1)))
	var logs []string
	logf := func(format string, args ...any) { logs = append(logs, fmt.Sprintf(format, args...)) }
	readErrs := map[string]int64{}
	slots := gather([]string{atLimit}, readErrs, logf)
	if len(slots) != 1 || !slots[0].up || len(readErrs) != 0 {
		t.Errorf("a report of exactly 1 MiB must be read: slots=%+v errors=%v", slots, readErrs)
	}
	slots = gather([]string{pastLimit}, readErrs, logf)
	if len(slots) != 0 || readErrs[pastLimit] != 1 {
		t.Errorf("a report of 1 MiB + 1 must be refused and counted: slots=%+v errors=%v", slots, readErrs)
	}
	if !strings.Contains(strings.Join(logs, "\n"), "larger than") {
		t.Errorf("the refusal must say the file is too large, got: %v", logs)
	}
}

func TestParseFileErrors(t *testing.T) {
	for _, doc := range []string{
		"",
		"not json",
		`{"rc":`,
		`[1,2]`,
		`null`,
		`"text"`,
		`42`,
		`{"rc":0} {"rc":1}`,
		`{"rc":0} trailing`,
	} {
		if slots, err := parseFile([]byte(doc)); err == nil {
			t.Errorf("parseFile(%q) must fail, got %+v", doc, slots)
		}
	}
}

func TestParseArgs(t *testing.T) {
	var errs bytes.Buffer
	o, err := parseArgs([]string{"-file", "a.json"}, &errs)
	if err != nil {
		t.Fatalf("one -file: %v", err)
	}
	// The default is written out here: a change of it must fail this test.
	if o.listen != "127.0.0.1:9478" {
		t.Errorf("default listen = %q, want 127.0.0.1:9478", o.listen)
	}
	host, port, err := net.SplitHostPort(o.listen)
	if err != nil || port != "9478" || !net.ParseIP(host).IsLoopback() {
		t.Errorf("default listen %q must be a loopback address on port 9478", o.listen)
	}

	o, err = parseArgs([]string{"-file", "a.json", "-file", "b.json", "-glob", "g/*/r.json", "-glob", "h/*.json", "-listen", "127.0.0.1:1"}, &errs)
	if err != nil {
		t.Fatalf("repeated flags: %v", err)
	}
	if strings.Join(o.files, ",") != "a.json,b.json" || strings.Join(o.globs, ",") != "g/*/r.json,h/*.json" || o.listen != "127.0.0.1:1" {
		t.Errorf("repeated flags read as %+v", o)
	}

	o, err = parseArgs([]string{"-glob", "g/*.json"}, &errs)
	if err != nil || len(o.globs) != 1 {
		t.Errorf("a -glob alone must be enough: %+v %v", o, err)
	}

	errs.Reset()
	if _, err = parseArgs(nil, &errs); !errors.Is(err, errNoSource) {
		t.Errorf("no -file and no -glob must fail with errNoSource, got %v", err)
	}
	if !strings.Contains(errs.String(), "usage: dendra-exporter") {
		t.Errorf("no source must print the usage, got %q", errs.String())
	}
	if _, err = parseArgs([]string{"-listen", "127.0.0.1:1"}, &errs); err == nil {
		t.Error("-listen alone must fail: a report source is required")
	}
	if _, err = parseArgs([]string{"-glob", "["}, &errs); err == nil {
		t.Error("a malformed -glob must refuse to start")
	}
	if _, err = parseArgs([]string{"-file", "a.json", "-nope"}, &errs); err == nil {
		t.Error("an unknown flag must fail")
	}
	if _, err = parseArgs([]string{"-h"}, &errs); !errors.Is(err, flag.ErrHelp) {
		t.Errorf("-h must return flag.ErrHelp, got %v", err)
	}
}

// TestFamilyTypes pins the type and the help of every family, in the order of the text.
func TestFamilyTypes(t *testing.T) {
	text := render(nil, map[string]int64{})
	want := []string{
		"dendra_miner_health_up gauge",
		"dendra_miner_health_rc gauge",
		"dendra_miner_health_checks gauge",
		"dendra_miner_health_check gauge",
		"dendra_miner_health_generated_timestamp_seconds gauge",
		"dendra_miner_health_schedule_period_seconds gauge",
		"dendra_miner_health_selftest_rc gauge",
		"dendra_miner_health_judge_role gauge",
		"dendra_miner_health_read_errors_total counter",
	}
	var got []string
	helps := map[string]bool{}
	for _, line := range strings.Split(text, "\n") {
		if strings.HasPrefix(line, "# TYPE ") {
			got = append(got, strings.TrimPrefix(line, "# TYPE "))
		}
		if strings.HasPrefix(line, "# HELP ") {
			helps[strings.Fields(line)[2]] = true
		}
	}
	if strings.Join(got, "|") != strings.Join(want, "|") {
		t.Errorf("type lines:\n got %q\nwant %q", got, want)
	}
	for _, w := range want {
		if name := strings.Fields(w)[0]; !helps[name] {
			t.Errorf("%s has no help line", name)
		}
	}
}

// TestNoFreeText checks that the free texts of a report (the kit path, the project name, the
// measured, reason and fix texts) reach no series: they would name a user or make series
// without bound.
func TestNoFreeText(t *testing.T) {
	for _, doc := range []string{oneSlotJSON, twoSlotsJSON, bareTwoSlotsJSON} {
		slots, err := parseFile([]byte(doc))
		if err != nil {
			t.Fatal(err)
		}
		text := render(slots, map[string]int64{})
		for _, s := range []string{
			"/srv/dendra-kit", "dendra-miner", "kit=", "project=",
			"node 312 blocks behind", "the node is catching up", "wait, then run the check again",
			"nvidia-smi not found", "no running miner answered", "pinned image of the release",
		} {
			if strings.Contains(text, s) {
				t.Errorf("the text of a report reached the output: %q", s)
			}
		}
	}
}

// TestStandardLibraryOnly reads the imports of the package: the exporter runs nothing and pulls
// no module, so an import outside the standard library, os/exec and the plugin loader are refused.
func TestStandardLibraryOnly(t *testing.T) {
	files, err := filepath.Glob("*.go")
	if err != nil || len(files) == 0 {
		t.Fatalf("no source files found: %v", err)
	}
	fset := token.NewFileSet()
	for _, name := range files {
		f, err := parser.ParseFile(fset, name, nil, parser.ImportsOnly)
		if err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		for _, imp := range f.Imports {
			path, err := strconv.Unquote(imp.Path.Value)
			if err != nil {
				t.Fatalf("%s: import %s: %v", name, imp.Path.Value, err)
			}
			if first, _, _ := strings.Cut(path, "/"); strings.Contains(first, ".") {
				t.Errorf("%s imports %s: the standard library only", name, path)
			}
			if path == "os/exec" || path == "plugin" {
				t.Errorf("%s imports %s: the exporter runs nothing", name, path)
			}
		}
	}
}
