package main

// This file reads the miner-health reports and turns them into samples; main.go serves them.

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"os"
	"sort"
	"strconv"
	"strings"
)

// maxReportBytes is the largest report file read. A file past it is refused, so a stray file
// cannot hold a scrape back.
const maxReportBytes = 1 << 20

// slotSeries holds the samples one slot contributes. A nil pointer or a nil map means the field
// was absent or of the wrong type: that series is left out, nothing is invented.
type slotSeries struct {
	slot      int
	up        bool
	rc        *float64
	counts    map[string]float64
	checks    []checkSample
	generated *float64
	schedule  *float64
	selftest  *float64
	judgeWord *string
}

// checkSample is one entry of the host list: 0 ok, 1 ko, 2 unmeasured.
type checkSample struct {
	id    string
	name  string
	value float64
}

// Every field of a report may be absent or of the wrong type, so each read below is a type check
// that can fail: asObject, asArray, asString and asNumber answer false for anything else.
func asObject(v any) (map[string]any, bool) {
	m, ok := v.(map[string]any)
	return m, ok
}

func asArray(v any) ([]any, bool) {
	a, ok := v.([]any)
	return a, ok
}

func asString(v any) (string, bool) {
	s, ok := v.(string)
	return s, ok
}

func asNumber(v any) (float64, bool) {
	n, ok := v.(json.Number)
	if !ok {
		return 0, false
	}
	f, err := n.Float64()
	if err != nil {
		return 0, false
	}
	return f, true
}

// slotOf reads the slot label: a whole number, as the script writes it. Anything else, or a
// number past a billion, is read as absent.
func slotOf(m map[string]any) (int, bool) {
	f, ok := asNumber(m["slot"])
	if !ok || f != math.Trunc(f) || f > 1e9 || f < -1e9 {
		return 0, false
	}
	return int(f), true
}

// parseSlotDoc reads one slot's document (its fields at the top) into its samples. The caller has
// already settled the slot number; every field that is absent or of the wrong type is simply
// left without its series.
func parseSlotDoc(doc map[string]any, slot int) slotSeries {
	s := slotSeries{slot: slot, up: true}
	if v, ok := asNumber(doc["rc"]); ok {
		s.rc = &v
	}
	if sum, ok := asObject(doc["summary"]); ok {
		for _, state := range []string{"ok", "ko", "unmeasured"} {
			if v, ok := asNumber(sum[state]); ok {
				if s.counts == nil {
					s.counts = map[string]float64{}
				}
				s.counts[state] = v
			}
		}
	}
	if host, ok := asArray(doc["host"]); ok {
		seen := map[string]bool{}
		for _, el := range host {
			hm, ok := asObject(el)
			if !ok {
				continue
			}
			id, okID := asString(hm["id"])
			name, okName := asString(hm["name"])
			// Without its two labels a check has no series. A name twice would serve the same
			// series twice, so the first entry wins.
			if !okID || !okName || seen[id+"\x00"+name] {
				continue
			}
			seen[id+"\x00"+name] = true
			var v float64
			switch st, _ := asString(hm["state"]); st {
			case "ok":
				v = 0
			case "ko":
				v = 1
			default:
				v = 2 // any other state counts as unmeasured, as the script does
			}
			s.checks = append(s.checks, checkSample{id: id, name: name, value: v})
		}
	}
	if v, ok := asNumber(doc["generated_epoch"]); ok {
		s.generated = &v
	}
	if v, ok := asNumber(doc["schedule_period_s"]); ok {
		s.schedule = &v
	}
	if v, ok := asNumber(doc["selftest_rc"]); ok {
		s.selftest = &v
	}
	if jr, ok := asObject(doc["judge_role"]); ok {
		if w, ok := asString(jr["word"]); ok {
			s.judgeWord = &w
		}
	}
	return s
}

// parseFile reads one report file. A report is either one slot (fields at the top) or a document
// with a slots array, one document per slot; an element of the array may wrap its document in a
// doc field, as the machine run of the script writes it. A slot whose document is missing or
// broken keeps its slot number and shows as down; the others are unaffected.
func parseFile(b []byte) ([]slotSeries, error) {
	dec := json.NewDecoder(bytes.NewReader(b))
	dec.UseNumber()
	var top any
	if err := dec.Decode(&top); err != nil {
		return nil, fmt.Errorf("not JSON: %v", err)
	}
	if dec.More() {
		return nil, fmt.Errorf("not one JSON document")
	}
	root, ok := asObject(top)
	if !ok {
		return nil, fmt.Errorf("not a JSON object")
	}
	if arr, ok := asArray(root["slots"]); ok {
		var out []slotSeries
		for _, el := range arr {
			m, ok := asObject(el)
			if !ok {
				continue // no slot number to read: nothing can be labelled
			}
			doc, wrapped := m, false
			if d, ok := asObject(m["doc"]); ok {
				doc, wrapped = d, true
			}
			slot, ok := slotOf(doc)
			if !ok {
				slot, ok = slotOf(m)
			}
			if !ok {
				continue // no slot number anywhere: nothing can be labelled
			}
			if wrapped {
				out = append(out, parseSlotDoc(doc, slot))
			} else if _, hasDoc := m["doc"]; hasDoc {
				// The wrapper names a slot whose own document is absent or broken.
				out = append(out, slotSeries{slot: slot, up: false})
			} else {
				out = append(out, parseSlotDoc(doc, slot))
			}
		}
		return out, nil
	}
	slot := 0 // a top-level report without a slot field is slot 0
	if n, ok := slotOf(root); ok {
		slot = n
	}
	return []slotSeries{parseSlotDoc(root, slot)}, nil
}

// gather reads every file, in the order given, and returns the slots it found. Two files that
// name the same slot: the first one read wins and the later one is logged and left out. A read
// that fails (missing file, a file past maxReportBytes, bad JSON) counts one error per path in
// readErrs; the map belongs to the caller and grows across scrapes.
func gather(paths []string, readErrs map[string]int64, logf func(string, ...any)) []slotSeries {
	bySlot := map[int]bool{}
	var out []slotSeries
	for _, p := range paths {
		b, err := readReport(p)
		if err == nil {
			var slots []slotSeries
			if slots, err = parseFile(b); err == nil {
				for _, s := range slots {
					if bySlot[s.slot] {
						logf("slot %d already has a report; the one in %s is left out", s.slot, p)
						continue
					}
					bySlot[s.slot] = true
					out = append(out, s)
				}
				continue
			}
		}
		readErrs[p]++
		logf("%s: %v", p, err)
	}
	return out
}

// readReport reads one report file, refusing a file past maxReportBytes.
func readReport(path string) ([]byte, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	b, err := io.ReadAll(io.LimitReader(f, maxReportBytes+1))
	if err != nil {
		return nil, err
	}
	if len(b) > maxReportBytes {
		return nil, fmt.Errorf("larger than %d bytes", maxReportBytes)
	}
	return b, nil
}

// escapeLabel escapes a label value as the exposition format requires: the backslash, the double
// quote and the line feed.
func escapeLabel(v string) string {
	return strings.NewReplacer(`\`, `\\`, `"`, `\"`, "\n", `\n`).Replace(v)
}

// labels builds the label set of a sample: {a="x",b="y"}, or nothing when there is no pair.
func labels(pairs ...string) string {
	if len(pairs) == 0 {
		return ""
	}
	parts := make([]string, 0, len(pairs)/2)
	for i := 0; i+1 < len(pairs); i += 2 {
		parts = append(parts, pairs[i]+"=\""+escapeLabel(pairs[i+1])+"\"")
	}
	return "{" + strings.Join(parts, ",") + "}"
}

// render writes the exposition text, version 0.0.4 of the Prometheus text format: every family
// in a fixed order with its help and type lines, then one sample per line, sorted so that two
// scrapes of the same reports give the same text.
func render(slots []slotSeries, readErrs map[string]int64) string {
	ordered := append([]slotSeries(nil), slots...)
	sort.Slice(ordered, func(i, j int) bool { return ordered[i].slot < ordered[j].slot })
	slotLabel := func(i int) string { return strconv.Itoa(i) }

	var b strings.Builder
	header := func(keyword, name, text string) {
		fmt.Fprintf(&b, "# %s %s %s\n", keyword, name, text)
	}
	family := func(name, help, typ string) {
		header("HELP", name, help)
		header("TYPE", name, typ)
	}
	sample := func(name, set string, v float64) {
		fmt.Fprintf(&b, "%s%s %s\n", name, set, strconv.FormatFloat(v, 'f', -1, 64))
	}

	family("dendra_miner_health_up",
		"1 when the slot's report was read and parsed, 0 otherwise", "gauge")
	for _, s := range ordered {
		v := 0.0
		if s.up {
			v = 1
		}
		sample("dendra_miner_health_up", labels("slot", slotLabel(s.slot)), v)
	}

	family("dendra_miner_health_rc",
		"the rc of the report: 0 ok, 1 a problem is standing, 2 not everything measured", "gauge")
	for _, s := range ordered {
		if s.rc != nil {
			sample("dendra_miner_health_rc", labels("slot", slotLabel(s.slot)), *s.rc)
		}
	}

	family("dendra_miner_health_checks",
		"the counts of the report summary, by state", "gauge")
	for _, s := range ordered {
		for _, state := range []string{"ok", "ko", "unmeasured"} {
			if v, ok := s.counts[state]; ok {
				sample("dendra_miner_health_checks",
					labels("slot", slotLabel(s.slot), "state", state), v)
			}
		}
	}

	family("dendra_miner_health_check",
		"one check of the host list: 0 ok, 1 ko, 2 unmeasured", "gauge")
	for _, s := range ordered {
		checks := append([]checkSample(nil), s.checks...)
		sort.Slice(checks, func(i, j int) bool {
			if checks[i].id != checks[j].id {
				return checks[i].id < checks[j].id
			}
			return checks[i].name < checks[j].name
		})
		for _, c := range checks {
			sample("dendra_miner_health_check",
				labels("slot", slotLabel(s.slot), "id", c.id, "name", c.name), c.value)
		}
	}

	family("dendra_miner_health_generated_timestamp_seconds",
		"when the report was generated, in seconds since the epoch", "gauge")
	for _, s := range ordered {
		if s.generated != nil {
			sample("dendra_miner_health_generated_timestamp_seconds",
				labels("slot", slotLabel(s.slot)), *s.generated)
		}
	}

	family("dendra_miner_health_schedule_period_seconds",
		"the period of the kit schedule, in seconds", "gauge")
	for _, s := range ordered {
		if s.schedule != nil {
			sample("dendra_miner_health_schedule_period_seconds",
				labels("slot", slotLabel(s.slot)), *s.schedule)
		}
	}

	family("dendra_miner_health_selftest_rc",
		"the rc of the self-test: 0 ok, 1 a problem is standing, 2 not everything measured", "gauge")
	for _, s := range ordered {
		if s.selftest != nil {
			sample("dendra_miner_health_selftest_rc",
				labels("slot", slotLabel(s.slot)), *s.selftest)
		}
	}

	family("dendra_miner_health_judge_role",
		"1 for the word of judge_role, the role the judge of this miner holds", "gauge")
	for _, s := range ordered {
		if s.judgeWord != nil {
			sample("dendra_miner_health_judge_role",
				labels("slot", slotLabel(s.slot), "word", *s.judgeWord), 1)
		}
	}

	files := make([]string, 0, len(readErrs))
	for f := range readErrs {
		files = append(files, f)
	}
	sort.Strings(files)
	family("dendra_miner_health_read_errors_total",
		"reads of a report file that failed since the start", "counter")
	for _, f := range files {
		sample("dendra_miner_health_read_errors_total", labels("file", f), float64(readErrs[f]))
	}

	return b.String()
}
