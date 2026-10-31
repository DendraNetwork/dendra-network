package main

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	neturl "net/url"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"
)

// finding is one condition that is true now. Key names it across passes; Fingerprint changes when the
// condition changes in a way worth a new message (a new day, a different count).
type finding struct {
	Key         string
	Fingerprint string
	Text        string
	Read        string
}

// checkResult has three states: Err set (the check could not read what it needs), Findings empty
// (everything read is fine) and Findings set (firing). Note is a line for the status command.
type checkResult struct {
	Findings []finding
	Note     string
	Err      error
}

const maxBody = 4 << 20

var errTooLarge = errors.New("response larger than the 4 MiB limit")

// clean makes a value safe to put in one message line.
func clean(s string, max int) string {
	s = strings.Map(func(r rune) rune {
		if r < 0x20 || r == 0x7f {
			return ' '
		}
		return r
	}, s)
	s = strings.TrimSpace(s)
	if len(s) > max {
		s = s[:max] + "..."
	}
	return s
}

// plainErr drops the URL that net/http puts in its errors, so that an error never carries a secret.
func plainErr(err error) error {
	var ue *neturl.Error
	if errors.As(err, &ue) {
		return ue.Err
	}
	return err
}

func (a *app) getBody(ctx context.Context, url string) ([]byte, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, fmt.Errorf("GET %s: bad request", url)
	}
	resp, err := a.client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("GET %s: %v", url, plainErr(err))
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("GET %s: HTTP %d", url, resp.StatusCode)
	}
	b, err := io.ReadAll(io.LimitReader(resp.Body, maxBody+1))
	if err != nil {
		return nil, fmt.Errorf("GET %s: %v", url, plainErr(err))
	}
	if len(b) > maxBody {
		return nil, fmt.Errorf("GET %s: %w", url, errTooLarge)
	}
	return b, nil
}

func (a *app) getJSON(ctx context.Context, url string, v any) error {
	b, err := a.getBody(ctx, url)
	if err != nil {
		return err
	}
	if err := json.Unmarshal(b, v); err != nil {
		return fmt.Errorf("GET %s: not the expected JSON (%v)", url, err)
	}
	return nil
}

// ---------------------------------------------------------------------------------------- blocks

// checkBlocks fires when the height read from the node's /status has not changed for stallS seconds.
// It updates st.Blocks; the caller decides whether to keep it.
func (a *app) checkBlocks(ctx context.Context, st *alertState) checkResult {
	var doc struct {
		Result *struct {
			SyncInfo *struct {
				Height     *string `json:"latest_block_height"`
				Time       *string `json:"latest_block_time"`
				CatchingUp *bool   `json:"catching_up"`
			} `json:"sync_info"`
		} `json:"result"`
	}
	if err := a.getJSON(ctx, a.cfg.rpcURL+"/status", &doc); err != nil {
		return checkResult{Err: err}
	}
	if doc.Result == nil || doc.Result.SyncInfo == nil || doc.Result.SyncInfo.Height == nil ||
		doc.Result.SyncInfo.Time == nil || doc.Result.SyncInfo.CatchingUp == nil {
		return checkResult{Err: errors.New("/status lacks result.sync_info.latest_block_height, latest_block_time or catching_up")}
	}
	si := doc.Result.SyncInfo
	h, err := strconv.ParseInt(*si.Height, 10, 64)
	if err != nil || h < 1 {
		return checkResult{Err: fmt.Errorf("/status latest_block_height is not a positive whole number (read %q)", clean(*si.Height, 40))}
	}
	now := a.now().Unix()
	b := &st.Blocks
	if !b.Seen || b.Height != h {
		b.Seen, b.Height, b.MovedAt = true, h, now
	}
	still := now - b.MovedAt
	age := "unreadable"
	if t, err := time.Parse(time.RFC3339Nano, *si.Time); err == nil {
		age = fmt.Sprintf("%d s old", now-t.Unix())
	}
	note := fmt.Sprintf("height %d, unchanged for %d s (limit %d s), latest block %s, catching_up=%v", h, still, a.cfg.stallS, age, *si.CatchingUp)
	if still < int64(a.cfg.stallS) {
		return checkResult{Note: note}
	}
	return checkResult{Note: note, Findings: []finding{{
		Key:         "blocks-stalled",
		Fingerprint: "stalled",
		Text: fmt.Sprintf("The chain height has stayed at %d for %d min (limit %d s). The latest block is %s; catching_up=%v.",
			h, still/60, a.cfg.stallS, age, *si.CatchingUp),
		Read: fmt.Sprintf("curl -s %s/status", a.cfg.rpcURL),
	}}}
}

// ---------------------------------------------------------------------------------------- memory

// vmKiB reads a "Name:   123 kB" line of /proc/<pid>/status.
func vmKiB(status, name string) (int64, error) {
	sc := bufio.NewScanner(strings.NewReader(status))
	for sc.Scan() {
		rest, ok := strings.CutPrefix(sc.Text(), name+":")
		if !ok {
			continue
		}
		f := strings.Fields(rest)
		if len(f) != 2 || f[1] != "kB" {
			return 0, fmt.Errorf("%s line is not \"<n> kB\" (read %q)", name, clean(rest, 40))
		}
		n, err := strconv.ParseInt(f[0], 10, 64)
		if err != nil || n < 0 {
			return 0, fmt.Errorf("%s is not a whole number (read %q)", name, clean(f[0], 40))
		}
		return n, nil
	}
	return 0, fmt.Errorf("no %s line", name)
}

func (a *app) readCmd(ctx context.Context, name string, args ...string) ([]byte, error) {
	ctx, cancel := context.WithTimeout(ctx, time.Duration(a.cfg.readS)*time.Second)
	defer cancel()
	out, err := a.runCmd(ctx, name, args...)
	if err != nil {
		return nil, fmt.Errorf("%s %s: %v", name, args[0], clean(err.Error(), 200))
	}
	return out, nil
}

// checkMemory fires when no chain container runs, or when dendrad holds stateful memory above memMiB.
func (a *app) checkMemory(ctx context.Context) checkResult {
	readTip := fmt.Sprintf("docker stats --no-stream $(docker ps -q --filter label=com.docker.compose.project=%s --filter label=com.docker.compose.service=%s)",
		a.cfg.project, a.cfg.service)
	out, err := a.readCmd(ctx, "docker", "ps", "-q",
		"--filter", "label=com.docker.compose.project="+a.cfg.project,
		"--filter", "label=com.docker.compose.service="+a.cfg.service)
	if err != nil {
		return checkResult{Err: err}
	}
	ids := strings.Fields(string(out))
	if len(ids) == 0 {
		return checkResult{Findings: []finding{{
			Key: "chain-down", Fingerprint: "down",
			Text: fmt.Sprintf("No running container for service %s of project %s.", a.cfg.service, a.cfg.project),
			Read: "docker ps -a --filter label=com.docker.compose.service=" + a.cfg.service,
		}}}
	}
	if len(ids) != 1 {
		return checkResult{Err: fmt.Errorf("docker ps found %d running containers for the chain service, expected one", len(ids))}
	}
	out, err = a.readCmd(ctx, "docker", "inspect", ids[0])
	if err != nil {
		return checkResult{Err: err}
	}
	var insp []struct {
		State *struct {
			Pid    *int    `json:"Pid"`
			Status *string `json:"Status"`
		} `json:"State"`
	}
	if err := json.Unmarshal(out, &insp); err != nil || len(insp) != 1 || insp[0].State == nil ||
		insp[0].State.Pid == nil || insp[0].State.Status == nil {
		return checkResult{Err: errors.New("docker inspect gave no State.Pid and State.Status for the chain container")}
	}
	if *insp[0].State.Status != "running" || *insp[0].State.Pid < 1 {
		return checkResult{Findings: []finding{{
			Key: "chain-down", Fingerprint: "down",
			Text: fmt.Sprintf("The chain container is %s (pid %d).", clean(*insp[0].State.Status, 30), *insp[0].State.Pid),
			Read: readTip,
		}}}
	}
	raw, err := os.ReadFile(filepath.Join(a.procRoot, strconv.Itoa(*insp[0].State.Pid), "status"))
	if err != nil {
		return checkResult{Err: fmt.Errorf("cannot read the process status of the node: %v", plainErr(err))}
	}
	rss, err := vmKiB(string(raw), "VmRSS")
	if err != nil {
		return checkResult{Err: err}
	}
	swap, err := vmKiB(string(raw), "VmSwap")
	if err != nil {
		return checkResult{Err: err}
	}
	rssM, swapM := rss/1024, swap/1024
	note := fmt.Sprintf("dendrad %d MiB resident + %d MiB in swap = %d MiB (limit %d MiB)", rssM, swapM, rssM+swapM, a.cfg.memMiB)
	if rssM+swapM < int64(a.cfg.memMiB) {
		return checkResult{Note: note}
	}
	return checkResult{Note: note, Findings: []finding{{
		Key: "chain-memory", Fingerprint: "over",
		Text: fmt.Sprintf("The node process holds %d MiB resident plus %d MiB in swap, %d MiB in all (limit %d MiB).",
			rssM, swapM, rssM+swapM, a.cfg.memMiB),
		Read: readTip,
	}}}
}

// ------------------------------------------------------------------------------------------ kit

const kitDeadlineLayout = "2006-01-02T15:04:05Z"

var kitMinRE = regexp.MustCompile(`^[1-9][0-9]{0,8}$`)

// readEnvKeys returns the last assignment of each wanted key in a compose .env file. A key with no
// line is absent from the map, which is different from a key set to an empty value.
func readEnvKeys(path string, keys ...string) (map[string]string, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("cannot read %s: %v", path, plainErr(err))
	}
	want := map[string]bool{}
	for _, k := range keys {
		want[k] = true
	}
	out := map[string]string{}
	for _, line := range strings.Split(string(b), "\n") {
		line = strings.TrimSpace(strings.TrimRight(line, "\r"))
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		line = strings.TrimSpace(strings.TrimPrefix(line, "export "))
		name, val, ok := strings.Cut(line, "=")
		name = strings.TrimSpace(name)
		if !ok || !want[name] {
			continue
		}
		val = strings.TrimSpace(val)
		if val != "" && (val[0] == '"' || val[0] == '\'') {
			if end := strings.IndexByte(val[1:], val[0]); end >= 0 {
				val = val[1 : 1+end]
			}
		} else if i := strings.Index(val, " #"); i >= 0 {
			val = strings.TrimSpace(val[:i])
		}
		out[name] = val
	}
	return out, nil
}

// checkKit reads the floor and the deadline from the host .env, and inside the lead window before the
// deadline counts the proven live nodes whose declared kit is below the floor.
func (a *app) checkKit(ctx context.Context) checkResult {
	env, err := readEnvKeys(a.cfg.envFile, "DENDRA_KIT_MIN", "DENDRA_KIT_DEADLINE")
	if err != nil {
		return checkResult{Err: err}
	}
	minRaw, deadRaw := env["DENDRA_KIT_MIN"], env["DENDRA_KIT_DEADLINE"]
	if minRaw == "" && deadRaw == "" {
		return checkResult{Note: "dormant: DENDRA_KIT_MIN and DENDRA_KIT_DEADLINE are both unset"}
	}
	bad := func(why string) checkResult {
		return checkResult{Findings: []finding{{
			Key: "kit-config", Fingerprint: clean(why, 120),
			Text: "The kit floor is misconfigured in the host .env: " + why + ". The faucet and the relay refuse to start on it.",
			Read: "grep -E '^DENDRA_KIT_(MIN|DEADLINE)=' " + a.cfg.envFile,
		}}}
	}
	if minRaw == "" || deadRaw == "" {
		return bad("DENDRA_KIT_MIN and DENDRA_KIT_DEADLINE are set together or not at all")
	}
	if !kitMinRE.MatchString(minRaw) {
		return bad(fmt.Sprintf("DENDRA_KIT_MIN is not a positive whole number (read %q)", clean(minRaw, 30)))
	}
	floor, _ := strconv.Atoi(minRaw)
	deadline, err := time.Parse(kitDeadlineLayout, deadRaw)
	if err != nil {
		return bad(fmt.Sprintf("DENDRA_KIT_DEADLINE does not read YYYY-MM-DDThh:mm:ssZ (read %q)", clean(deadRaw, 30)))
	}
	left := deadline.Sub(a.now())
	if left <= 0 {
		return checkResult{Note: fmt.Sprintf("floor %d, deadline %s has passed", floor, deadRaw)}
	}
	if left > time.Duration(a.cfg.kitLeadH)*time.Hour {
		return checkResult{Note: fmt.Sprintf("floor %d, deadline %s is %d h away (checks start %d h before)", floor, deadRaw, int(left.Hours()), a.cfg.kitLeadH)}
	}
	var doc struct {
		Verified *struct {
			Kits  *map[string]int `json:"kit_versions"`
			Nodes *int            `json:"live_nodes"`
		} `json:"verified"`
	}
	if err := a.getJSON(ctx, a.cfg.capacity, &doc); err != nil {
		return checkResult{Err: err}
	}
	if doc.Verified == nil || doc.Verified.Kits == nil || doc.Verified.Nodes == nil {
		return checkResult{Err: errors.New("the capacity answer lacks verified.kit_versions or verified.live_nodes")}
	}
	below := 0
	for k, n := range *doc.Verified.Kits {
		v, err := strconv.Atoi(k)
		if err != nil || v < 0 || n < 0 {
			return checkResult{Err: fmt.Errorf("verified.kit_versions holds the entry %q: %d", clean(k, 30), n)}
		}
		if v < floor {
			below += n
		}
	}
	note := fmt.Sprintf("floor %d, deadline in %d h: %d of %d proven live nodes below it (tolerated: %d)", floor, int(left.Hours()), below, *doc.Verified.Nodes, a.cfg.kitBelow)
	if below <= a.cfg.kitBelow {
		return checkResult{Note: note}
	}
	return checkResult{Note: note, Findings: []finding{{
		Key: "kit-adoption", Fingerprint: fmt.Sprintf("below=%d", below),
		Text: fmt.Sprintf("%d of %d proven live nodes still declare a kit below %d. The deadline %s is in %d h; from then the faucet and the relay refuse them.",
			below, *doc.Verified.Nodes, floor, deadRaw, int(left.Hours())),
		Read: fmt.Sprintf("curl -s %s (verified.kit_versions)", a.cfg.capacity),
	}}}
}

// -------------------------------------------------------------------------------------- grading

// readUngradedDays returns the ranked days that the public status lists without a grade, sorted and without
// repeats. A status without the field, or with a day that is not a whole number of at least 0, is an error:
// an absent list is not an empty one.
func (a *app) readUngradedDays(ctx context.Context) ([]int, error) {
	var doc struct {
		Days *[]int `json:"ungraded_ranked_days"`
	}
	if err := a.getJSON(ctx, a.cfg.programme+"/status", &doc); err != nil {
		return nil, err
	}
	if doc.Days == nil {
		return nil, errors.New("the status lacks ungraded_ranked_days")
	}
	seen := map[int]bool{}
	days := []int{}
	for _, d := range *doc.Days {
		if d < 0 {
			return nil, fmt.Errorf("ungraded_ranked_days holds %d, which is not a day number", d)
		}
		if !seen[d] {
			seen[d] = true
			days = append(days, d)
		}
	}
	sort.Ints(days)
	return days, nil
}

// joinDays writes a list of days as the settings file and the messages show it: "0,1,6".
func joinDays(days []int) string {
	parts := make([]string, len(days))
	for i, d := range days {
		parts[i] = strconv.Itoa(d)
	}
	return strings.Join(parts, ",")
}

// checkGrading fires on a ranked day with sampled answers and no grade that the owner did not accept.
func (a *app) checkGrading(ctx context.Context) checkResult {
	days, err := a.readUngradedDays(ctx)
	if err != nil {
		return checkResult{Err: err}
	}
	var open []int
	for _, d := range days {
		if !a.cfg.accepted[d] {
			open = append(open, d)
		}
	}
	note := fmt.Sprintf("%d ranked day(s) with no grade, %d not accepted", len(days), len(open))
	if len(open) == 0 {
		return checkResult{Note: note}
	}
	list := joinDays(open)
	return checkResult{Note: note, Findings: []finding{{
		Key: "grading-ungraded", Fingerprint: "days=" + list,
		Text: fmt.Sprintf("Ranked day(s) %s have sampled answers and no grade. A ranked day is final: nothing can be graded there any more.", list),
		Read: fmt.Sprintf("curl -s %s/status (ungraded_ranked_days); the grading machine runs dendra_grade_daily.sh", a.cfg.programme),
	}}}
}

// --------------------------------------------------------------------------------------- payout

var payoutAlertRE = regexp.MustCompile(`^\S+ season payout: exit ([0-9]+): (.*)$`)

// checkPayout reads what the season payout cron leaves: its alert file, and the age of its log.
func (a *app) checkPayout() checkResult {
	read := "bash " + filepath.Join(a.cfg.home, "dendra/dendra/onchain-staging/dendra_season_payout_cron.sh") + " status"
	var fs []finding
	alertPath := filepath.Join(a.cfg.payoutDir, "season-payout.alert")
	logPath := filepath.Join(a.cfg.payoutDir, "season-payout.log")
	note := ""
	b, err := os.ReadFile(alertPath)
	switch {
	case err == nil:
		first, _, _ := strings.Cut(string(b), "\n")
		fp, shown := "unparsed", clean(first, 200)
		if m := payoutAlertRE.FindStringSubmatch(strings.TrimRight(first, "\r")); m != nil {
			fp, shown = "exit "+m[1]+": "+clean(m[2], 120), "exit "+m[1]+": "+clean(m[2], 160)
		}
		fs = append(fs, finding{Key: "payout-alert", Fingerprint: fp,
			Text: "The season payout cron raised its alert (" + shown + ").", Read: read})
		note = "alert file present"
	case errors.Is(err, os.ErrNotExist):
		note = "no alert file"
	default:
		return checkResult{Err: fmt.Errorf("cannot read %s: %v", alertPath, plainErr(err))}
	}
	fi, err := os.Stat(logPath)
	switch {
	case errors.Is(err, os.ErrNotExist):
		fs = append(fs, finding{Key: "payout-silent", Fingerprint: "no log",
			Text: "The season payout cron has no log: it has not run on this host.", Read: read})
		note += ", no log"
	case err != nil:
		return checkResult{Err: fmt.Errorf("cannot read %s: %v", logPath, plainErr(err))}
	default:
		age := a.now().Sub(fi.ModTime())
		note += fmt.Sprintf(", log %d min old (limit %d h)", int(age.Minutes()), a.cfg.silentH)
		if age >= time.Duration(a.cfg.silentH)*time.Hour {
			fs = append(fs, finding{Key: "payout-silent", Fingerprint: "silent",
				Text: fmt.Sprintf("The season payout cron wrote nothing for %d h (limit %d h): it is not running.", int(age.Hours()), a.cfg.silentH),
				Read: read})
		}
	}
	return checkResult{Findings: fs, Note: note}
}
