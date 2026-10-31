package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"testing"
	"time"
)

// Everything here runs against local test servers and temporary directories. A webhook in these tests
// is a loopback URL; the real host names are only used as strings for validateWebhook.

type fixture struct {
	t        *testing.T
	home     string
	dir      string
	app      *app
	srv      *httptest.Server
	hook     *httptest.Server
	mu       sync.Mutex
	clock    time.Time
	rpc      func() (int, string)
	capacity func() (int, string)
	status   func() (int, string)
	hookCode int
	posted   []string
	reached  int
	docker   map[string]string
}

func rpcJSON(height, ts string, catching bool) string {
	return fmt.Sprintf(`{"result":{"sync_info":{"latest_block_height":%q,"latest_block_time":%q,"catching_up":%v}}}`, height, ts, catching)
}

func newFixture(t *testing.T) *fixture {
	t.Helper()
	f := &fixture{t: t, hookCode: 204, docker: map[string]string{}}
	f.home = t.TempDir()
	f.dir = filepath.Join(f.home, ".dendra-alert")
	f.clock = time.Date(2026, 10, 24, 6, 0, 0, 0, time.UTC)
	f.rpc = func() (int, string) { return 200, rpcJSON("100", f.clock.Format(time.RFC3339Nano), false) }
	f.capacity = func() (int, string) { return 200, `{"verified":{"live_nodes":4,"kit_versions":{"3":4}}}` }
	f.status = func() (int, string) { return 200, `{"ungraded_ranked_days":[]}` }
	f.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		f.mu.Lock()
		defer f.mu.Unlock()
		var code int
		var body string
		switch r.URL.Path {
		case "/rpc/status":
			code, body = f.rpc()
		case "/capacity":
			code, body = f.capacity()
		case "/programme/status":
			code, body = f.status()
		default:
			code, body = 404, "{}"
		}
		w.WriteHeader(code)
		io.WriteString(w, body)
	}))
	t.Cleanup(f.srv.Close)
	f.hook = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		f.mu.Lock()
		defer f.mu.Unlock()
		f.reached++
		b, _ := io.ReadAll(r.Body)
		if f.hookCode == 204 {
			var m struct {
				Content string `json:"content"`
				Allowed struct {
					Parse []string `json:"parse"`
				} `json:"allowed_mentions"`
			}
			if err := json.Unmarshal(b, &m); err != nil || m.Allowed.Parse == nil || len(m.Allowed.Parse) != 0 {
				f.t.Errorf("message body does not carry an empty mention list: %s", b)
			}
			f.posted = append(f.posted, m.Content)
		}
		w.WriteHeader(f.hookCode)
	}))
	t.Cleanup(f.hook.Close)
	env := map[string]string{
		"HOME":                   f.home,
		setRPC:                   f.srv.URL + "/rpc",
		setCapacity:              f.srv.URL + "/capacity",
		setProgramme:             f.srv.URL + "/programme",
		setPayoutDir:             filepath.Join(f.home, "payout"),
		setEnvFile:               filepath.Join(f.home, "env"),
		"DENDRA_ALERT_TEST_NONE": "",
	}
	cfg, err := loadConfig(func(k string) string { return env[k] })
	if err != nil {
		t.Fatal(err)
	}
	f.app = newApp(cfg, io.Discard)
	f.app.allowLoopback = true
	f.app.procRoot = filepath.Join(f.home, "proc")
	f.app.now = func() time.Time { return f.clock }
	f.app.runCmd = func(ctx context.Context, name string, args ...string) ([]byte, error) {
		key := name + " " + strings.Join(args, " ")
		for prefix, out := range f.docker {
			if strings.HasPrefix(key, prefix) {
				if strings.HasPrefix(out, "ERR:") {
					return nil, errors.New(out[4:])
				}
				return []byte(out), nil
			}
		}
		return nil, fmt.Errorf("unexpected command %q", key)
	}
	if err := os.MkdirAll(f.dir, 0o700); err != nil {
		t.Fatal(err)
	}
	os.WriteFile(filepath.Join(f.dir, "webhook"), []byte(f.hook.URL+"/hook\n"), 0o600)
	return f
}

func (f *fixture) setConfig(m map[string]string) {
	f.t.Helper()
	env := map[string]string{
		"HOME": f.home, setRPC: f.srv.URL + "/rpc", setCapacity: f.srv.URL + "/capacity",
		setProgramme: f.srv.URL + "/programme", setPayoutDir: filepath.Join(f.home, "payout"),
		setEnvFile: filepath.Join(f.home, "env"),
	}
	for k, v := range m {
		env[k] = v
	}
	cfg, err := loadConfig(func(k string) string { return env[k] })
	if err != nil {
		f.t.Fatal(err)
	}
	f.app.cfg = cfg
}

func (f *fixture) messages() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]string(nil), f.posted...)
}

func (f *fixture) advance(d time.Duration) { f.clock = f.clock.Add(d) }

func (f *fixture) writeEnv(text string) {
	f.t.Helper()
	if err := os.WriteFile(filepath.Join(f.home, "env"), []byte(text), 0o600); err != nil {
		f.t.Fatal(err)
	}
}

func contains(t *testing.T, what, got, want string) {
	t.Helper()
	if !strings.Contains(got, want) {
		t.Errorf("%s: %q does not contain %q", what, got, want)
	}
}

func TestConfigDefaultsAndPrecedence(t *testing.T) {
	home := t.TempDir()
	env := map[string]string{"HOME": home}
	c, err := loadConfig(func(k string) string { return env[k] })
	if err != nil {
		t.Fatal(err)
	}
	if c.dir != filepath.Join(home, ".dendra-alert") || c.stallS != 900 || c.memMiB != 2400 || c.unknownN != 3 || c.repeatH != 12 {
		t.Errorf("defaults are not the documented ones: %+v", c)
	}
	os.MkdirAll(c.dir, 0o700)
	os.WriteFile(filepath.Join(c.dir, "settings"), []byte("# comment\nDENDRA_ALERT_STALL_S=120\nDENDRA_ALERT_ACCEPTED_UNGRADED_DAYS=\"0, 1 6\"\nDENDRA_ALERT_SKIP_CHECKS=payout\n"), 0o600)
	env[setMemMiB] = "3000"
	c, err = loadConfig(func(k string) string { return env[k] })
	if err != nil {
		t.Fatal(err)
	}
	if c.stallS != 120 || c.memMiB != 3000 || !c.accepted[6] || !c.accepted[0] || !c.skip["payout"] {
		t.Errorf("file and environment were not read in order: %+v", c)
	}
}

func TestConfigRefusals(t *testing.T) {
	bad := []struct{ name, val string }{
		{setStallS, "0"}, {setStallS, "ten"}, {setMemMiB, "-5"}, {setRepeatH, "0"}, {setUnknownN, "0"},
		{setKitBelowMax, "-1"}, {setAccepted, "3,x"}, {setSkip, "nothing"}, {setRPC, "ftp://x"}, {setEnvFile, "relative/.env"},
	}
	for _, b := range bad {
		env := map[string]string{"HOME": t.TempDir(), b.name: b.val}
		if _, err := loadConfig(func(k string) string { return env[k] }); err == nil {
			t.Errorf("%s=%q was accepted", b.name, b.val)
		}
	}
	for _, home := range []string{"", "relative"} {
		if _, err := loadConfig(func(k string) string {
			if k == "HOME" {
				return home
			}
			return ""
		}); err == nil {
			t.Errorf("HOME=%q was accepted", home)
		}
	}
	if _, err := parseSettingsFile("DENDRA_ALERT_STALL_S=5\nDENDRA_ALERT_STALL=5\n"); err == nil {
		t.Error("a misspelled setting name was accepted")
	}
	if _, err := parseSettingsFile(setDir + "=/x\n"); err == nil {
		t.Error("the state directory was accepted inside the settings file")
	}
}

func TestValidateWebhook(t *testing.T) {
	root := "https://" + "discord.com" + webhookRoot
	good := []string{
		root + "123456789/abcDEF_-ghi",
		"https://canary." + "discord.com" + webhookRoot + "1/t",
		"https://" + "discordapp.com" + webhookRoot + "1/t?thread_id=9",
	}
	for _, g := range good {
		if err := validateWebhook(g, false); err != nil {
			t.Errorf("%q refused: %v", g, err)
		}
	}
	bad := []string{
		"", "not a url", "http://" + "discord.com" + webhookRoot + "1/t",
		"https://example.com" + webhookRoot + "1/t",
		"https://" + "discord.com.evil.example" + webhookRoot + "1/t",
		"https://user@" + "discord.com" + webhookRoot + "1/t",
		"https://" + "discord.com:8443" + webhookRoot + "1/t",
		root + "1", root + "1/t/extra", "https://" + "discord.com" + "/api/other/1/t",
		root + "1/t#frag",
	}
	for _, b := range bad {
		if err := validateWebhook(b, false); err == nil {
			t.Errorf("%q accepted", b)
		}
	}
	if err := validateWebhook("http://127.0.0.1:1/x", false); err == nil {
		t.Error("loopback accepted without the test switch")
	}
	if err := validateWebhook("http://127.0.0.1:1/x", true); err != nil {
		t.Errorf("loopback refused with the test switch: %v", err)
	}
	if err := validateWebhook("http://example.com/x", true); err == nil {
		t.Error("a non-loopback http URL accepted with the test switch")
	}
}

func TestReadWebhookRefusals(t *testing.T) {
	dir := t.TempDir()
	secret := "https://" + "discord.com" + webhookRoot + "424242/SECRETTOKEN"
	if _, err := readWebhook(dir, false); err == nil || !strings.Contains(err.Error(), "does not exist") {
		t.Errorf("an absent file must refuse and say so: %v", err)
	}
	p := webhookPath(dir)
	os.WriteFile(p, []byte(secret+"\n"), 0o644)
	os.Chmod(p, 0o644)
	if _, err := readWebhook(dir, false); err == nil || !strings.Contains(err.Error(), "600") || strings.Contains(err.Error(), "SECRETTOKEN") {
		t.Errorf("a group-readable file must refuse without the value: %v", err)
	}
	os.Chmod(p, 0o600)
	if got, err := readWebhook(dir, false); err != nil || got != secret {
		t.Errorf("a 600 file must be read: %v", err)
	}
	os.WriteFile(p, []byte(secret+"\nsecond line\n"), 0o600)
	if _, err := readWebhook(dir, false); err == nil || strings.Contains(err.Error(), "SECRETTOKEN") {
		t.Errorf("two lines must refuse without the value: %v", err)
	}
	os.WriteFile(p, []byte("https://example.com/SECRETTOKEN\n"), 0o600)
	if _, err := readWebhook(dir, false); err == nil || strings.Contains(err.Error(), "SECRETTOKEN") {
		t.Errorf("a foreign host must refuse without the value: %v", err)
	}
	os.Remove(p)
	target := filepath.Join(dir, "elsewhere")
	os.WriteFile(target, []byte(secret), 0o600)
	if err := os.Symlink(target, p); err == nil {
		if _, err := readWebhook(dir, false); err == nil {
			t.Error("a symbolic link was followed")
		}
	}
}

func TestSendErrorsCarryNoURL(t *testing.T) {
	f := newFixture(t)
	hook := "http://127.0.0.1:1/SECRETTOKEN"
	err := f.app.send(context.Background(), hook, "x")
	if err == nil {
		t.Fatal("a refused connection was not an error")
	}
	if strings.Contains(err.Error(), "SECRETTOKEN") {
		t.Errorf("the error carries the URL: %v", err)
	}
	f.hookCode = 500
	err = f.app.send(context.Background(), f.hook.URL+"/SECRETTOKEN", "x")
	if err == nil || strings.Contains(err.Error(), "SECRETTOKEN") {
		t.Errorf("an HTTP 500 must fail without the URL: %v", err)
	}
}

func TestSendDoesNotFollowRedirects(t *testing.T) {
	f := newFixture(t)
	other := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Error("the redirect target was reached")
	}))
	defer other.Close()
	redir := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, other.URL, http.StatusTemporaryRedirect)
	}))
	defer redir.Close()
	if err := f.app.send(context.Background(), redir.URL+"/x", "x"); err == nil {
		t.Error("a redirect counted as a delivery")
	}
}

func TestBlocksStall(t *testing.T) {
	f := newFixture(t)
	st := newState()
	ctx := context.Background()
	if r := f.app.checkBlocks(ctx, &st); r.Err != nil || len(r.Findings) != 0 {
		t.Fatalf("first pass must only record: %+v", r)
	}
	f.advance(899 * time.Second)
	if r := f.app.checkBlocks(ctx, &st); len(r.Findings) != 0 {
		t.Errorf("899 s still: must not fire (limit 900): %+v", r)
	}
	f.advance(1 * time.Second)
	r := f.app.checkBlocks(ctx, &st)
	if len(r.Findings) != 1 || r.Findings[0].Key != "blocks-stalled" {
		t.Fatalf("900 s still: must fire: %+v", r)
	}
	contains(t, "text", r.Findings[0].Text, "height has stayed at 100")
	contains(t, "read", r.Findings[0].Read, "/rpc/status")
	f.rpc = func() (int, string) { return 200, rpcJSON("101", f.clock.Format(time.RFC3339Nano), false) }
	if r := f.app.checkBlocks(ctx, &st); len(r.Findings) != 0 {
		t.Errorf("the height moved: must clear: %+v", r)
	}
	// A height that goes down (a reset chain) counts as movement too.
	f.advance(2000 * time.Second)
	f.rpc = func() (int, string) { return 200, rpcJSON("5", f.clock.Format(time.RFC3339Nano), false) }
	if r := f.app.checkBlocks(ctx, &st); len(r.Findings) != 0 {
		t.Errorf("a lower height is a change: %+v", r)
	}
}

func TestBlocksUnreadable(t *testing.T) {
	f := newFixture(t)
	st := newState()
	cases := map[string]func() (int, string){
		"http 500":     func() (int, string) { return 500, "{}" },
		"not json":     func() (int, string) { return 200, "<html>" },
		"no sync_info": func() (int, string) { return 200, `{"result":{}}` },
		"no catching_up": func() (int, string) {
			return 200, `{"result":{"sync_info":{"latest_block_height":"5","latest_block_time":"2026-10-24T06:00:00Z"}}}`
		},
		"height zero":    func() (int, string) { return 200, rpcJSON("0", "2026-10-24T06:00:00Z", false) },
		"height not int": func() (int, string) { return 200, rpcJSON("abc", "2026-10-24T06:00:00Z", false) },
		"no block time": func() (int, string) {
			return 200, `{"result":{"sync_info":{"latest_block_height":"5","catching_up":false}}}`
		},
	}
	for name, h := range cases {
		f.rpc = h
		if r := f.app.checkBlocks(context.Background(), &st); r.Err == nil || len(r.Findings) != 0 {
			t.Errorf("%s: must be unreadable, not ok and not firing: %+v", name, r)
		}
	}
	if st.Blocks.Seen {
		t.Error("an unreadable answer was recorded as a height")
	}
}

func writeProc(t *testing.T, root string, pid int, status string) {
	t.Helper()
	d := filepath.Join(root, fmt.Sprint(pid))
	os.MkdirAll(d, 0o755)
	os.WriteFile(filepath.Join(d, "status"), []byte(status), 0o644)
}

func dockerFor(f *fixture, ps, inspect string) {
	f.docker = map[string]string{
		"docker ps -q":    ps,
		"docker inspect ": inspect,
	}
}

func TestMemory(t *testing.T) {
	f := newFixture(t)
	ctx := context.Background()
	inspect := `[{"State":{"Pid":4242,"Status":"running"}}]`
	dockerFor(f, "abc123\n", inspect)
	writeProc(t, f.app.procRoot, 4242, "Name:\tdendrad\nVmRSS:\t 1048576 kB\nVmSwap:\t 204800 kB\n")
	r := f.app.checkMemory(ctx)
	if r.Err != nil || len(r.Findings) != 0 {
		t.Fatalf("1024 + 200 MiB is under the limit: %+v", r)
	}
	contains(t, "note", r.Note, "1024 MiB resident + 200 MiB in swap = 1224 MiB")
	writeProc(t, f.app.procRoot, 4242, "VmRSS:\t 2097152 kB\nVmSwap:\t 358400 kB\n") // 2048 + 350 = 2398
	if r := f.app.checkMemory(ctx); len(r.Findings) != 0 {
		t.Errorf("2398 MiB is under 2400: %+v", r)
	}
	writeProc(t, f.app.procRoot, 4242, "VmRSS:\t 2097152 kB\nVmSwap:\t 360448 kB\n") // 2048 + 352 = 2400
	r = f.app.checkMemory(ctx)
	if len(r.Findings) != 1 || r.Findings[0].Key != "chain-memory" {
		t.Fatalf("2400 MiB must fire: %+v", r)
	}
	contains(t, "read", r.Findings[0].Read, "docker stats --no-stream")
	contains(t, "read", r.Findings[0].Read, "com.docker.compose.service=chain")
}

func TestMemoryStatesAndUnreadable(t *testing.T) {
	f := newFixture(t)
	ctx := context.Background()
	dockerFor(f, "", "")
	if r := f.app.checkMemory(ctx); len(r.Findings) != 1 || r.Findings[0].Key != "chain-down" {
		t.Errorf("no running container must fire chain-down: %+v", r)
	}
	dockerFor(f, "ERR:Cannot connect to the Docker daemon", "")
	if r := f.app.checkMemory(ctx); r.Err == nil || len(r.Findings) != 0 {
		t.Errorf("a dead docker daemon is unreadable, not a stopped chain: %+v", r)
	}
	dockerFor(f, "a\nb\n", "")
	if r := f.app.checkMemory(ctx); r.Err == nil {
		t.Errorf("two containers must be unreadable: %+v", r)
	}
	for name, inspect := range map[string]string{
		"no Pid":    `[{"State":{"Status":"running"}}]`,
		"no Status": `[{"State":{"Pid":4242}}]`,
		"not json":  `nope`,
		"empty":     `[]`,
	} {
		dockerFor(f, "abc\n", inspect)
		if r := f.app.checkMemory(ctx); r.Err == nil {
			t.Errorf("%s must be unreadable: %+v", name, r)
		}
	}
	dockerFor(f, "abc\n", `[{"State":{"Pid":0,"Status":"restarting"}}]`)
	if r := f.app.checkMemory(ctx); len(r.Findings) != 1 || r.Findings[0].Key != "chain-down" {
		t.Errorf("a restarting container is down: %+v", r)
	}
	dockerFor(f, "abc\n", `[{"State":{"Pid":4242,"Status":"running"}}]`)
	writeProc(t, f.app.procRoot, 4242, "VmRSS:\t 1000 kB\n")
	if r := f.app.checkMemory(ctx); r.Err == nil || len(r.Findings) != 0 {
		t.Errorf("a missing VmSwap line is unreadable, not zero swap: %+v", r)
	}
	writeProc(t, f.app.procRoot, 4242, "VmRSS:\t lots kB\nVmSwap:\t 0 kB\n")
	if r := f.app.checkMemory(ctx); r.Err == nil {
		t.Errorf("a malformed VmRSS must be unreadable: %+v", r)
	}
	os.RemoveAll(filepath.Join(f.app.procRoot, "4242"))
	if r := f.app.checkMemory(ctx); r.Err == nil {
		t.Errorf("a vanished process directory must be unreadable: %+v", r)
	}
}

func TestKit(t *testing.T) {
	f := newFixture(t)
	ctx := context.Background()
	// An absent file is unreadable, not dormant.
	if r := f.app.checkKit(ctx); r.Err == nil {
		t.Errorf("a missing .env must be unreadable: %+v", r)
	}
	f.writeEnv("OTHER=1\nDENDRA_KIT_MIN=\n")
	if r := f.app.checkKit(ctx); r.Err != nil || len(r.Findings) != 0 || !strings.HasPrefix(r.Note, "dormant") {
		t.Errorf("both unset is dormant: %+v", r)
	}
	f.writeEnv("DENDRA_KIT_MIN=3\n")
	if r := f.app.checkKit(ctx); len(r.Findings) != 1 || r.Findings[0].Key != "kit-config" {
		t.Errorf("one without the other must fire kit-config: %+v", r)
	}
	f.writeEnv("DENDRA_KIT_MIN=0\nDENDRA_KIT_DEADLINE=2026-10-25T00:00:00Z\n")
	if r := f.app.checkKit(ctx); len(r.Findings) != 1 || r.Findings[0].Key != "kit-config" {
		t.Errorf("a floor of 0 must fire kit-config: %+v", r)
	}
	f.writeEnv("DENDRA_KIT_MIN=3\nDENDRA_KIT_DEADLINE=tomorrow\n")
	if r := f.app.checkKit(ctx); len(r.Findings) != 1 || r.Findings[0].Key != "kit-config" {
		t.Errorf("a bad deadline must fire kit-config: %+v", r)
	}
	// Outside the lead window the capacity registry is not read.
	f.capacity = func() (int, string) { t.Error("capacity read outside the window"); return 500, "" }
	f.writeEnv("DENDRA_KIT_MIN=3\nDENDRA_KIT_DEADLINE=2026-10-28T06:00:00Z\n")
	if r := f.app.checkKit(ctx); r.Err != nil || len(r.Findings) != 0 {
		t.Errorf("four days out: %+v", r)
	}
	f.writeEnv("DENDRA_KIT_MIN=3\nDENDRA_KIT_DEADLINE=2026-10-23T06:00:00Z\n")
	if r := f.app.checkKit(ctx); r.Err != nil || len(r.Findings) != 0 || !strings.Contains(r.Note, "has passed") {
		t.Errorf("a passed deadline: %+v", r)
	}
	// Inside the window: 72 h before, to the second.
	f.capacity = func() (int, string) {
		return 200, `{"verified":{"live_nodes":9,"kit_versions":{"0":1,"2":1,"3":5,"4":2}}}`
	}
	f.writeEnv("export DENDRA_KIT_MIN=\"3\"  # floor\nDENDRA_KIT_DEADLINE=2026-10-27T06:00:00Z\n")
	r := f.app.checkKit(ctx)
	if len(r.Findings) != 1 || r.Findings[0].Key != "kit-adoption" || r.Findings[0].Fingerprint != "below=2" {
		t.Fatalf("kits 0 and 2 are below 3: want below=2: %+v", r)
	}
	contains(t, "text", r.Findings[0].Text, "2 of 9 proven live nodes")
	f.setConfig(map[string]string{setKitBelowMax: "2"})
	if r := f.app.checkKit(ctx); len(r.Findings) != 0 {
		t.Errorf("two below with two tolerated: %+v", r)
	}
	f.setConfig(nil)
	f.writeEnv("DENDRA_KIT_MIN=2\nDENDRA_KIT_DEADLINE=2026-10-27T06:00:00Z\n")
	if r := f.app.checkKit(ctx); len(r.Findings) != 1 || r.Findings[0].Fingerprint != "below=1" {
		t.Errorf("only kit 0 is below 2: %+v", r)
	}
	f.writeEnv("DENDRA_KIT_MIN=1\nDENDRA_KIT_DEADLINE=2026-10-27T06:00:00Z\n")
	if r := f.app.checkKit(ctx); len(r.Findings) != 1 || r.Findings[0].Fingerprint != "below=1" {
		t.Errorf("a signed report with no kit field counts as below 1: %+v", r)
	}
	for name, body := range map[string]string{
		"no verified":     `{}`,
		"no kit_versions": `{"verified":{"live_nodes":1}}`,
		"no live_nodes":   `{"verified":{"kit_versions":{}}}`,
		"non-numeric key": `{"verified":{"live_nodes":1,"kit_versions":{"x":1}}}`,
		"negative count":  `{"verified":{"live_nodes":1,"kit_versions":{"3":-1}}}`,
	} {
		body := body
		f.capacity = func() (int, string) { return 200, body }
		if r := f.app.checkKit(ctx); r.Err == nil || len(r.Findings) != 0 {
			t.Errorf("%s must be unreadable: %+v", name, r)
		}
	}
	f.capacity = func() (int, string) { return 200, `{"verified":{"live_nodes":0,"kit_versions":{}}}` }
	if r := f.app.checkKit(ctx); r.Err != nil || len(r.Findings) != 0 {
		t.Errorf("an empty but present table is zero nodes below: %+v", r)
	}
}

func TestGrading(t *testing.T) {
	f := newFixture(t)
	ctx := context.Background()
	f.status = func() (int, string) { return 200, `{"ungraded_ranked_days":[7,0,1,6]}` }
	f.setConfig(map[string]string{setAccepted: "0,1,6"})
	r := f.app.checkGrading(ctx)
	if len(r.Findings) != 1 || r.Findings[0].Fingerprint != "days=7" {
		t.Fatalf("day 7 is the only one not accepted: %+v", r)
	}
	f.status = func() (int, string) { return 200, `{"ungraded_ranked_days":[0,1,6]}` }
	if r := f.app.checkGrading(ctx); r.Err != nil || len(r.Findings) != 0 {
		t.Errorf("only accepted days: %+v", r)
	}
	f.status = func() (int, string) { return 200, `{"day":14}` }
	if r := f.app.checkGrading(ctx); r.Err == nil || len(r.Findings) != 0 {
		t.Errorf("a status without the field is unreadable, not empty: %+v", r)
	}
	f.status = func() (int, string) { return 200, `{"ungraded_ranked_days":["x"]}` }
	if r := f.app.checkGrading(ctx); r.Err == nil {
		t.Errorf("a non-integer day is unreadable: %+v", r)
	}
	f.status = func() (int, string) { return 200, `{"ungraded_ranked_days":[]}` }
	f.setConfig(nil)
	if r := f.app.checkGrading(ctx); r.Err != nil || len(r.Findings) != 0 {
		t.Errorf("an empty list is fine: %+v", r)
	}
}

// TestBaseline: the list install stores is what the public status holds, sorted and without repeats; a status
// that cannot be read, or that lacks the field, gives no list (an absent list is not an empty one).
func TestBaseline(t *testing.T) {
	f := newFixture(t)
	var out bytes.Buffer
	f.app.out = &out
	cases := []struct {
		name string
		code int
		body string
		want string
	}{
		{"old days in any order, with a repeat", 200, `{"ungraded_ranked_days":[6,0,1,6]}`, "ungraded_ranked_days=0,1,6\n"},
		{"one day", 200, `{"ungraded_ranked_days":[6]}`, "ungraded_ranked_days=6\n"},
		{"no day", 200, `{"ungraded_ranked_days":[]}`, "ungraded_ranked_days=\n"},
		{"field absent", 200, `{"day":14}`, ""},
		{"field null", 200, `{"ungraded_ranked_days":null}`, ""},
		{"day is a string", 200, `{"ungraded_ranked_days":["x"]}`, ""},
		{"negative day", 200, `{"ungraded_ranked_days":[0,-1]}`, ""},
		{"not JSON", 200, `<html>`, ""},
		{"HTTP error", 503, `{"ungraded_ranked_days":[0]}`, ""},
	}
	for _, c := range cases {
		c := c
		f.status = func() (int, string) { return c.code, c.body }
		out.Reset()
		code := f.app.cmdBaseline()
		if c.want == "" {
			if code == 0 || out.Len() != 0 {
				t.Errorf("%s: want a refusal with no list, got code %d and %q", c.name, code, out.String())
			}
			continue
		}
		if code != 0 || out.String() != c.want {
			t.Errorf("%s: want %q, got code %d and %q", c.name, c.want, code, out.String())
		}
	}
}

// TestBaselineSilencesOnlyKnownDays: the list read at install, written as the accepted days, keeps the first
// pass quiet, and a day that appears afterwards raises the alert.
func TestBaselineSilencesOnlyKnownDays(t *testing.T) {
	f := newFixture(t)
	ctx := context.Background()
	var out bytes.Buffer
	f.app.out = &out
	f.status = func() (int, string) { return 200, `{"ungraded_ranked_days":[1,0,6]}` }
	if f.app.cmdBaseline() != 0 {
		t.Fatal("the baseline was not read")
	}
	line := strings.TrimSpace(out.String())
	list := strings.TrimPrefix(line, "ungraded_ranked_days=")
	settings := "# written by install\n" + setAccepted + "=" + list + "\n"
	if err := os.WriteFile(filepath.Join(f.dir, "settings"), []byte(settings), 0o600); err != nil {
		t.Fatal(err)
	}
	f.setConfig(nil)
	if r := f.app.checkGrading(ctx); r.Err != nil || len(r.Findings) != 0 {
		t.Errorf("the days of the baseline must stay quiet: %+v", r)
	}
	f.status = func() (int, string) { return 200, `{"ungraded_ranked_days":[0,1,6,7]}` }
	r := f.app.checkGrading(ctx)
	if len(r.Findings) != 1 || r.Findings[0].Fingerprint != "days=7" {
		t.Errorf("a day outside the baseline must alert alone: %+v", r)
	}
	// An empty baseline is a written zero days: every day alerts.
	f.status = func() (int, string) { return 200, `{"ungraded_ranked_days":[]}` }
	out.Reset()
	f.app.cmdBaseline()
	if strings.TrimSpace(out.String()) != "ungraded_ranked_days=" {
		t.Fatalf("an empty list is printed as an empty value: %q", out.String())
	}
	os.WriteFile(filepath.Join(f.dir, "settings"), []byte(setAccepted+"=\n"), 0o600)
	f.setConfig(nil)
	f.status = func() (int, string) { return 200, `{"ungraded_ranked_days":[2]}` }
	if r := f.app.checkGrading(ctx); len(r.Findings) != 1 {
		t.Errorf("with an empty baseline day 2 must alert: %+v", r)
	}
}

// TestStatusShowsBaseline: the status command prints the accepted days in effect.
func TestStatusShowsBaseline(t *testing.T) {
	f := newFixture(t)
	var out bytes.Buffer
	f.app.out = &out
	f.setConfig(map[string]string{setAccepted: "6,0 1"})
	f.app.cmdStatus()
	contains(t, "status with a baseline", out.String(), "baseline  accepted ranked days with no grade: 0,1,6\n")
	out.Reset()
	f.setConfig(nil)
	f.app.cmdStatus()
	contains(t, "status without a baseline", out.String(), "baseline  accepted ranked days with no grade: none")
}

func TestPayout(t *testing.T) {
	f := newFixture(t)
	dir := filepath.Join(f.home, "payout")
	os.MkdirAll(dir, 0o700)
	logp := filepath.Join(dir, "season-payout.log")
	alertp := filepath.Join(dir, "season-payout.alert")
	if r := f.app.checkPayout(); len(r.Findings) != 1 || r.Findings[0].Key != "payout-silent" || r.Findings[0].Fingerprint != "no log" {
		t.Errorf("no log at all: %+v", r)
	}
	os.WriteFile(logp, []byte("x\n"), 0o600)
	os.Chtimes(logp, f.clock.Add(-6*time.Hour), f.clock.Add(-6*time.Hour))
	if r := f.app.checkPayout(); r.Err != nil || len(r.Findings) != 0 {
		t.Errorf("a log 6 h old is fine (limit 7 h): %+v", r)
	}
	os.Chtimes(logp, f.clock.Add(-7*time.Hour), f.clock.Add(-7*time.Hour))
	if r := f.app.checkPayout(); len(r.Findings) != 1 || r.Findings[0].Key != "payout-silent" {
		t.Errorf("a log 7 h old is silent: %+v", r)
	}
	os.Chtimes(logp, f.clock, f.clock)
	os.WriteFile(alertp, []byte("2026-10-24T03:00:05Z season payout: exit 3: run finished: the payout exited 3, the lines above say why\nlog: x\n"), 0o600)
	r := f.app.checkPayout()
	if len(r.Findings) != 1 || r.Findings[0].Key != "payout-alert" {
		t.Fatalf("the alert file must fire: %+v", r)
	}
	if strings.Contains(r.Findings[0].Fingerprint, "2026-10-24") {
		t.Errorf("the fingerprint carries the timestamp, so every new alert line would count as a change: %q", r.Findings[0].Fingerprint)
	}
	contains(t, "text", r.Findings[0].Text, "exit 3")
	contains(t, "read", r.Findings[0].Read, "dendra_season_payout_cron.sh status")
	os.WriteFile(alertp, []byte("free text without the shape\n"), 0o600)
	if r := f.app.checkPayout(); len(r.Findings) != 1 || r.Findings[0].Fingerprint != "unparsed" {
		t.Errorf("an unparsed alert still fires: %+v", r)
	}
}

// ---- passes

func (f *fixture) run() int {
	f.t.Helper()
	return f.app.cmdRun()
}

func (f *fixture) healthy() {
	f.docker = map[string]string{
		"docker ps -q":    "abc\n",
		"docker inspect ": `[{"State":{"Pid":4242,"Status":"running"}}]`,
	}
	writeProc(f.t, f.app.procRoot, 4242, "VmRSS:\t 1024000 kB\nVmSwap:\t 0 kB\n")
	f.writeEnv("DENDRA_KIT_MIN=\nDENDRA_KIT_DEADLINE=\n")
	f.rpc = func() (int, string) {
		return 200, rpcJSON(fmt.Sprint(100+f.clock.Unix()/5), f.clock.Format(time.RFC3339Nano), false)
	}
	dir := filepath.Join(f.home, "payout")
	os.MkdirAll(dir, 0o700)
	os.WriteFile(filepath.Join(dir, "season-payout.log"), []byte("ok\n"), 0o600)
	os.Chtimes(filepath.Join(dir, "season-payout.log"), f.clock, f.clock)
}

func TestRunQuietWhenHealthy(t *testing.T) {
	f := newFixture(t)
	f.healthy()
	for i := 0; i < 3; i++ {
		if c := f.run(); c != 0 {
			t.Fatalf("pass %d exit %d", i, c)
		}
		f.advance(5 * time.Minute)
		f.rpc = func() (int, string) {
			return 200, rpcJSON(fmt.Sprint(100+f.clock.Unix()%1000), f.clock.Format(time.RFC3339Nano), false)
		}
		os.Chtimes(filepath.Join(f.home, "payout", "season-payout.log"), f.clock, f.clock)
	}
	if m := f.messages(); len(m) != 0 {
		t.Errorf("a healthy host posted: %q", m)
	}
}

func TestRunAlertLifecycle(t *testing.T) {
	f := newFixture(t)
	f.healthy()
	moving := func() {
		f.rpc = func() (int, string) {
			return 200, rpcJSON(fmt.Sprint(100+f.clock.Unix()/5), f.clock.Format(time.RFC3339Nano), false)
		}
	}
	moving()
	f.run()
	// A grading alert starts.
	f.status = func() (int, string) { return 200, `{"ungraded_ranked_days":[3]}` }
	f.advance(5 * time.Minute)
	moving()
	f.run()
	m := f.messages()
	if len(m) != 1 || !strings.Contains(m[0], "firing grading-ungraded") || !strings.Contains(m[0], "Read: curl -s") {
		t.Fatalf("one firing message with its read command: %q", m)
	}
	// Same condition on the next passes: nothing new.
	for i := 0; i < 5; i++ {
		f.advance(5 * time.Minute)
		moving()
		os.Chtimes(filepath.Join(f.home, "payout", "season-payout.log"), f.clock, f.clock)
		f.run()
	}
	if len(f.messages()) != 1 {
		t.Fatalf("an alert repeated on every pass: %q", f.messages())
	}
	// The content changes: a new day.
	f.status = func() (int, string) { return 200, `{"ungraded_ranked_days":[3,4]}` }
	f.advance(5 * time.Minute)
	moving()
	f.run()
	if m := f.messages(); len(m) != 2 || !strings.Contains(m[1], "changed grading-ungraded") || !strings.Contains(m[1], "3,4") {
		t.Fatalf("a changed fingerprint must post changed: %q", m)
	}
	// 12 h of reminders.
	f.advance(11*time.Hour + 50*time.Minute)
	moving()
	os.Chtimes(filepath.Join(f.home, "payout", "season-payout.log"), f.clock, f.clock)
	f.run()
	if len(f.messages()) != 2 {
		t.Fatalf("under the reminder delay: %q", f.messages())
	}
	f.advance(10 * time.Minute)
	moving()
	os.Chtimes(filepath.Join(f.home, "payout", "season-payout.log"), f.clock, f.clock)
	f.run()
	if m := f.messages(); len(m) != 3 || !strings.Contains(m[2], "still firing") {
		t.Fatalf("the reminder: %q", m)
	}
	// It ends.
	f.status = func() (int, string) { return 200, `{"ungraded_ranked_days":[]}` }
	f.advance(5 * time.Minute)
	moving()
	f.run()
	if m := f.messages(); len(m) != 4 || !strings.Contains(m[3], "resolved grading-ungraded") {
		t.Fatalf("resolved: %q", m)
	}
	f.advance(5 * time.Minute)
	moving()
	f.run()
	if len(f.messages()) != 4 {
		t.Fatalf("a resolved alert posted again: %q", f.messages())
	}
}

func TestRunBlocksAndMemoryTogether(t *testing.T) {
	f := newFixture(t)
	f.healthy()
	f.rpc = func() (int, string) { return 200, rpcJSON("1", f.clock.Format(time.RFC3339Nano), false) }
	f.run()
	writeProc(t, f.app.procRoot, 4242, "VmRSS:\t 3000000 kB\nVmSwap:\t 0 kB\n")
	f.advance(16 * time.Minute) // the height never moved
	os.Chtimes(filepath.Join(f.home, "payout", "season-payout.log"), f.clock, f.clock)
	f.run()
	m := strings.Join(f.messages(), "\n")
	contains(t, "messages", m, "firing blocks-stalled")
	contains(t, "messages", m, "firing chain-memory")
}

func TestUnreadableNeedsSeveralPassesAndKeepsOldAlerts(t *testing.T) {
	f := newFixture(t)
	f.healthy()
	f.status = func() (int, string) { return 200, `{"ungraded_ranked_days":[3]}` }
	f.run()
	if len(f.messages()) != 1 {
		t.Fatalf("setup: %q", f.messages())
	}
	f.status = func() (int, string) { return 500, "" }
	for i := 1; i <= 2; i++ {
		f.advance(5 * time.Minute)
		f.run()
		if len(f.messages()) != 1 {
			t.Fatalf("pass %d unreadable: nothing may be posted yet and the old alert must not resolve: %q", i, f.messages())
		}
	}
	f.advance(5 * time.Minute)
	f.run()
	m := f.messages()
	if len(m) != 2 || !strings.Contains(m[1], "firing grading-unreadable") {
		t.Fatalf("the third failed pass raises the unreadable alert: %q", m)
	}
	// The source answers again: the unreadable alert ends, and the day-3 alert is still there.
	f.status = func() (int, string) { return 200, `{"ungraded_ranked_days":[3]}` }
	f.advance(5 * time.Minute)
	f.run()
	m = f.messages()
	if len(m) != 3 || !strings.Contains(m[2], "resolved grading-unreadable") {
		t.Fatalf("recovery: %q", m)
	}
}

func TestFailedDeliveryIsRetried(t *testing.T) {
	f := newFixture(t)
	f.healthy()
	f.status = func() (int, string) { return 200, `{"ungraded_ranked_days":[3]}` }
	f.hookCode = 500
	if c := f.run(); c != 1 {
		t.Fatalf("a failed delivery must exit 1, got %d", c)
	}
	f.hookCode = 204
	f.advance(5 * time.Minute)
	if c := f.run(); c != 0 {
		t.Fatalf("exit %d", c)
	}
	if m := f.messages(); len(m) != 1 || !strings.Contains(m[0], "firing grading-ungraded") {
		t.Fatalf("the message must go out on the next pass: %q", m)
	}
}

func TestSkippedCheckDropsItsRecordsQuietly(t *testing.T) {
	f := newFixture(t)
	f.healthy()
	f.status = func() (int, string) { return 200, `{"ungraded_ranked_days":[3]}` }
	f.run()
	f.setConfig(map[string]string{setSkip: "grading"})
	f.advance(5 * time.Minute)
	f.run()
	if len(f.messages()) != 1 {
		t.Errorf("a skipped check posted: %q", f.messages())
	}
	st, _, _ := loadState(f.dir, f.clock)
	if len(st.Alerts) != 0 {
		t.Errorf("records of a skipped check stayed: %+v", st.Alerts)
	}
}

func TestStatusPostsNothingAndWritesNothing(t *testing.T) {
	f := newFixture(t)
	f.healthy()
	f.status = func() (int, string) { return 200, `{"ungraded_ranked_days":[3]}` }
	var out bytes.Buffer
	f.app.out = &out
	if c := f.app.cmdStatus(); c != 1 {
		t.Errorf("a firing condition must exit 1: %d", c)
	}
	contains(t, "status output", out.String(), "firing")
	contains(t, "status output", out.String(), "webhook: ready")
	if f.reached != 0 {
		t.Error("status reached the webhook")
	}
	if _, err := os.Stat(statePath(f.dir)); err == nil {
		t.Error("status wrote state.json")
	}
	if strings.Contains(out.String(), f.hook.URL) {
		t.Error("status printed the webhook")
	}
}

func TestRunRefusesWithoutWebhook(t *testing.T) {
	f := newFixture(t)
	f.healthy()
	os.Remove(webhookPath(f.dir))
	if c := f.run(); c != 2 {
		t.Errorf("no webhook: want exit 2, got %d", c)
	}
	if _, err := os.Stat(statePath(f.dir)); err == nil {
		t.Error("a refused run wrote state")
	}
	if f.reached != 0 {
		t.Error("a refused run reached the network")
	}
}

func TestCorruptStateIsSetAside(t *testing.T) {
	f := newFixture(t)
	os.WriteFile(statePath(f.dir), []byte("{not json"), 0o600)
	st, note, err := loadState(f.dir, f.clock)
	if err != nil || note == "" || len(st.Alerts) != 0 {
		t.Fatalf("a damaged state must be set aside: %v %q", err, note)
	}
	if _, err := os.Stat(statePath(f.dir)); err == nil {
		t.Error("the damaged file was left in place")
	}
	m, _ := filepath.Glob(statePath(f.dir) + ".bad-*")
	if len(m) != 1 {
		t.Errorf("the damaged file must be kept aside: %v", m)
	}
}

func TestLockIsExclusive(t *testing.T) {
	dir := t.TempDir()
	un, err := lockDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := lockDir(dir); err != errLocked {
		t.Errorf("a second lock must report errLocked, got %v", err)
	}
	un()
	un2, err := lockDir(dir)
	if err != nil {
		t.Fatalf("the lock must free on release: %v", err)
	}
	un2()
}

func TestTestCommand(t *testing.T) {
	f := newFixture(t)
	if c := f.app.cmdTest(); c != 0 {
		t.Fatalf("exit %d", c)
	}
	if m := f.messages(); len(m) != 1 || !strings.Contains(m[0], "test message") {
		t.Errorf("%q", m)
	}
}

func TestMessagesAreCut(t *testing.T) {
	f := newFixture(t)
	if err := f.app.send(context.Background(), f.hook.URL+"/h", strings.Repeat("a", 5000)); err != nil {
		t.Fatal(err)
	}
	if m := f.messages(); len(m) != 1 || len(m[0]) > 2000 {
		t.Errorf("a message over the chat limit: %d", len(m[0]))
	}
}

// No file of this directory spells a whole webhook address, and none holds a local path.
func TestSourceNamesNoWebhookAndNoPath(t *testing.T) {
	whole := regexp.MustCompile(`(?i)discord(app)?\.com/api/webhooks/`)
	local := regexp.MustCompile("/mnt/" + "c/|[A-Z]:" + "[/" + string(rune(92)) + string(rune(92)) + "]Users|/home/[a-z]+/")
	files, _ := filepath.Glob("*.go")
	for _, name := range files {
		b, err := os.ReadFile(name)
		if err != nil {
			t.Fatal(err)
		}
		if whole.Match(b) {
			t.Errorf("%s spells a whole webhook address", name)
		}
		if local.Match(b) {
			t.Errorf("%s holds a local path", name)
		}
	}
}

// The mirror build refuses any published command that runs an internal script. This is the same
// shape, with the directory split so that this file does not match it.
func TestSourceRunsNoInternalScript(t *testing.T) {
	dir := "dendra/onch" + "ain-staging/"
	runs := regexp.MustCompile(dir + `[^ ]*[.](sh|py) *[|] *(bash|sh)|(bash|sh) +` + dir + `|< *` + dir + `[^ ]*[.](sh|py)`)
	files, _ := filepath.Glob("*.go")
	for _, name := range files {
		b, err := os.ReadFile(name)
		if err != nil {
			t.Fatal(err)
		}
		if runs.Match(b) {
			t.Errorf("%s holds a command that runs an internal script", name)
		}
	}
}

func TestHelpListsEverySettingWithItsDefault(t *testing.T) {
	var b bytes.Buffer
	usage(&b)
	for _, s := range settingDefs {
		contains(t, "help", b.String(), s.name)
	}
	contains(t, "help", b.String(), "2400")
}
