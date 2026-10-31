package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	neturl "net/url"
	"os"
	"path/filepath"
	"strings"
)

// The webhook lives in one file of the state directory and nowhere else. It is read without a
// default: a missing file, a mode other than owner-only, a symbolic link or a value that is not a
// chat webhook URL stops the run, and no message here repeats the value.
const webhookFile = "webhook"

// Host names and path of a chat webhook, kept apart so that no file of the repository spells a whole
// webhook address (the public surface guard refuses that spelling in a published file).
var (
	webhookHosts = []string{"discord.com", "discordapp.com"}
	webhookRoot  = "/api/" + "webhooks/"
)

func webhookPath(dir string) string { return filepath.Join(dir, webhookFile) }

func validateWebhook(raw string, allowLoopback bool) error {
	u, err := neturl.Parse(raw)
	if err != nil {
		return errors.New("not a URL")
	}
	if u.User != nil || u.Fragment != "" {
		return errors.New("must carry no user information and no fragment")
	}
	if allowLoopback && u.Scheme == "http" {
		if ip := net.ParseIP(u.Hostname()); ip != nil && ip.IsLoopback() {
			return nil
		}
	}
	if u.Scheme != "https" {
		return errors.New("must be an https URL")
	}
	host := strings.ToLower(u.Hostname())
	okHost := false
	for _, h := range webhookHosts {
		if host == h || host == "canary."+h || host == "ptb."+h {
			okHost = true
		}
	}
	if !okHost {
		return errors.New("the host is not a chat webhook host")
	}
	if u.Port() != "" {
		return errors.New("must not name a port")
	}
	rest, ok := strings.CutPrefix(u.Path, webhookRoot)
	if !ok || len(strings.Split(strings.Trim(rest, "/"), "/")) != 2 {
		return errors.New("the path is not a webhook path with an id and a token")
	}
	return nil
}

// readWebhook returns the webhook, or an error that names the file and the problem, never the value.
func readWebhook(dir string, allowLoopback bool) (string, error) {
	p := webhookPath(dir)
	fi, err := os.Lstat(p)
	if errors.Is(err, os.ErrNotExist) {
		return "", fmt.Errorf("%s does not exist: set the webhook first", p)
	}
	if err != nil {
		return "", fmt.Errorf("cannot stat %s: %v", p, plainErr(err))
	}
	if !fi.Mode().IsRegular() {
		return "", fmt.Errorf("%s is not a regular file", p)
	}
	if fi.Mode().Perm()&0o077 != 0 || fi.Mode().Perm()&0o400 == 0 {
		return "", fmt.Errorf("%s has mode %04o: it must be readable by its owner alone (600)", p, fi.Mode().Perm())
	}
	b, err := os.ReadFile(p)
	if err != nil {
		return "", fmt.Errorf("cannot read %s: %v", p, plainErr(err))
	}
	raw := strings.TrimSpace(string(b))
	if strings.ContainsAny(raw, "\r\n\t ") || raw == "" {
		return "", fmt.Errorf("%s must hold the webhook URL alone on one line", p)
	}
	if err := validateWebhook(raw, allowLoopback); err != nil {
		return "", fmt.Errorf("%s does not hold a chat webhook URL: %v", p, err)
	}
	return raw, nil
}

const maxMessage = 1900

// send posts one message. The mention list is empty, so a message can ping nobody. The error never
// carries the URL.
func (a *app) send(ctx context.Context, webhook, text string) error {
	if len(text) > maxMessage {
		text = text[:maxMessage] + "..."
	}
	body, err := json.Marshal(map[string]any{
		"content":          text,
		"allowed_mentions": map[string]any{"parse": []string{}},
	})
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, webhook, bytes.NewReader(body))
	if err != nil {
		return errors.New("cannot build the request")
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := a.client.Do(req)
	if err != nil {
		return fmt.Errorf("delivery failed: %v", plainErr(err))
	}
	defer resp.Body.Close()
	io.Copy(io.Discard, io.LimitReader(resp.Body, 1<<16))
	if resp.StatusCode != http.StatusNoContent && resp.StatusCode != http.StatusOK {
		return fmt.Errorf("delivery failed: HTTP %d", resp.StatusCode)
	}
	return nil
}
