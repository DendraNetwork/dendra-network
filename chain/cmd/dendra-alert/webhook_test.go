package main

import (
	"context"
	"os"
	"strings"
	"testing"
)

// Refusals of the webhook file reader, the ptb host form, and the message
// limit of send.

func TestReadWebhookMoreRefusals(t *testing.T) {
	dir := t.TempDir()
	p := webhookPath(dir)
	os.Mkdir(p, 0o700)
	if _, err := readWebhook(dir, false); err == nil || !strings.Contains(err.Error(), "regular file") {
		t.Errorf("a directory must be refused: %v", err)
	}
	os.Remove(p)
	os.WriteFile(p, []byte("http://127.0.0.1:1/x\n"), 0o200)
	if _, err := readWebhook(dir, false); err == nil || !strings.Contains(err.Error(), "600") {
		t.Errorf("a file the owner cannot read must be refused: %v", err)
	}
	os.Chmod(p, 0o600)
	os.WriteFile(p, []byte("http://127.0.0.1:1/x extra\n"), 0o600)
	if _, err := readWebhook(dir, false); err == nil || !strings.Contains(err.Error(), "one line") {
		t.Errorf("a value with an inner space must be refused: %v", err)
	}
	os.WriteFile(p, []byte("   \n"), 0o600)
	if _, err := readWebhook(dir, false); err == nil {
		t.Error("an empty file must be refused")
	}
	os.WriteFile(p, []byte("  http://127.0.0.1:1/x \n"), 0o600)
	got, err := readWebhook(dir, true)
	if err != nil || got != "http://127.0.0.1:1/x" {
		t.Errorf("space around the value must go, space inside must refuse: %q %v", got, err)
	}
}

func TestWebhookPtbHostIsAccepted(t *testing.T) {
	if err := validateWebhook("https://ptb."+"discord.com"+webhookRoot+"12/token", false); err != nil {
		t.Errorf("the ptb host was refused: %v", err)
	}
	if err := validateWebhook("https://ptb."+"example.com"+webhookRoot+"12/token", false); err == nil {
		t.Error("the ptb host was accepted on a foreign domain")
	}
}

func TestSendAcceptsStatusOK(t *testing.T) {
	f := newFixture(t)
	f.hookCode = 200
	if err := f.app.send(context.Background(), f.hook.URL+"/h", "x"); err != nil {
		t.Errorf("HTTP 200 counts as a delivery: %v", err)
	}
}

func TestMessageLimitBoundary(t *testing.T) {
	f := newFixture(t)
	if err := f.app.send(context.Background(), f.hook.URL+"/h", strings.Repeat("a", maxMessage)); err != nil {
		t.Fatal(err)
	}
	m := f.messages()
	if len(m) != 1 || len(m[0]) != maxMessage || strings.HasSuffix(m[0], "...") {
		t.Errorf("a message of exactly the limit must go out whole: %d", len(m[0]))
	}
	if err := f.app.send(context.Background(), f.hook.URL+"/h", strings.Repeat("a", maxMessage+1)); err != nil {
		t.Fatal(err)
	}
	m = f.messages()
	if len(m) != 2 || len(m[1]) != maxMessage+3 || !strings.HasSuffix(m[1], "...") {
		t.Errorf("one character over the limit must cut to the limit plus the ellipsis: %d", len(m[1]))
	}
}
