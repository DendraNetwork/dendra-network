// Tests for the dendra-vrf command. main() exits the process on every error path, so each case
// re-executes the test binary as a child (TestHelperProcess, at the bottom of this file): the
// arguments after "--" become os.Args in the child, main() runs there, and the parent captures
// stdout, stderr and the exit code of the real command.
package main

import (
	"bytes"
	"encoding/hex"
	"os"
	"os/exec"
	"strings"
	"testing"

	"github.com/DendraNetwork/dendra-network/chain/x/jobs/vrf"
)

// usageLine is the first of the three lines usage() prints on stderr.
const usageLine = "usage: dendra-vrf keygen | prove <sk_hex> <alpha> | pubkey <sk_hex> | verify <pk_hex> <alpha> <proof_hex>"

// childEnv builds the environment of the child: the parent's without DENDRA_VRF_SK or
// GO_WANT_HELPER_PROCESS (an ambient value must not reach a case that passes the secret in argv),
// then the helper gate, then the variables the case itself sets.
func childEnv(extra map[string]string) []string {
	env := make([]string, 0, len(os.Environ())+len(extra)+1)
	for _, kv := range os.Environ() {
		if strings.HasPrefix(kv, "DENDRA_VRF_SK=") || strings.HasPrefix(kv, "GO_WANT_HELPER_PROCESS=") {
			continue
		}
		env = append(env, kv)
	}
	env = append(env, "GO_WANT_HELPER_PROCESS=1")
	for k, v := range extra {
		env = append(env, k+"="+v)
	}
	return env
}

// runMain runs the command under test in a child process. args becomes os.Args there, argv[0]
// included; env holds extra environment variables for the child. It returns the child's stdout,
// stderr and exit code.
func runMain(t *testing.T, args []string, env map[string]string) (string, string, int) {
	t.Helper()

	cmd := exec.Command(os.Args[0], append([]string{"-test.run=TestHelperProcess", "--"}, args...)...)
	cmd.Env = childEnv(env)
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	code := 0
	if err := cmd.Run(); err != nil {
		exitErr, ok := err.(*exec.ExitError)
		if !ok {
			t.Fatalf("running %q: %v", args, err)
		}
		code = exitErr.ExitCode()
	}
	return stdout.String(), stderr.String(), code
}

// keygenPair runs keygen and returns its two hex fields, the secret and the public key.
func keygenPair(t *testing.T) (skHex, pkHex string) {
	t.Helper()
	stdout, stderr, code := runMain(t, []string{"dendra-vrf", "keygen"}, nil)
	if code != 0 {
		t.Fatalf("keygen exit code = %d, want 0 (stderr: %q)", code, stderr)
	}
	fields := strings.Split(strings.TrimSuffix(stdout, "\n"), "\t")
	if len(fields) != 2 {
		t.Fatalf("keygen output got %q, want two tab-separated fields", stdout)
	}
	return fields[0], fields[1]
}

// proveOk runs prove and returns its single stdout line, the proof in hex.
func proveOk(t *testing.T, skHex, alpha string) string {
	t.Helper()
	stdout, stderr, code := runMain(t, []string{"dendra-vrf", "prove", skHex, alpha}, nil)
	if code != 0 {
		t.Fatalf("prove exit code = %d, want 0 (stderr: %q)", code, stderr)
	}
	return strings.TrimSuffix(stdout, "\n")
}

// flipFirstByte returns the hex of the same bytes with the low bit of the first byte flipped:
// still valid hex of the same length, and a different proof.
func flipFirstByte(t *testing.T, hexStr string) string {
	t.Helper()
	b, err := hex.DecodeString(hexStr)
	if err != nil {
		t.Fatalf("decode %q: %v", hexStr, err)
	}
	b[0] ^= 0x01
	return hex.EncodeToString(b)
}

// keygen prints one line, "<sk_hex>\t<pk_hex>", with fields of the lengths the vrf package uses,
// and exits 0.
func TestKeygen(t *testing.T) {
	stdout, stderr, code := runMain(t, []string{"dendra-vrf", "keygen"}, nil)
	if code != 0 {
		t.Fatalf("keygen exit code = %d, want 0 (stderr: %q)", code, stderr)
	}
	if stderr != "" {
		t.Fatalf("keygen stderr got %q, want %q", stderr, "")
	}
	if !strings.HasSuffix(stdout, "\n") || strings.Count(stdout, "\n") != 1 {
		t.Fatalf("keygen stdout got %q, want a single line ending in a newline", stdout)
	}
	fields := strings.Split(strings.TrimSuffix(stdout, "\n"), "\t")
	if len(fields) != 2 {
		t.Fatalf("keygen output got %q, want exactly two tab-separated fields", stdout)
	}
	sk, err := hex.DecodeString(fields[0])
	if err != nil {
		t.Fatalf("keygen secret field: %v", err)
	}
	pk, err := hex.DecodeString(fields[1])
	if err != nil {
		t.Fatalf("keygen public key field: %v", err)
	}
	if len(sk) != vrf.PrivateKeySize {
		t.Fatalf("secret length got %d bytes, want %d", len(sk), vrf.PrivateKeySize)
	}
	if len(pk) != vrf.PublicKeySize {
		t.Fatalf("public key length got %d bytes, want %d", len(pk), vrf.PublicKeySize)
	}
}

// Round trip: a proof made with the keygen secret verifies under the keygen public key, the beta
// has the length the vrf package uses, and pubkey of the secret returns the keygen public key.
func TestRoundTrip(t *testing.T) {
	skHex, pkHex := keygenPair(t)
	alpha := "dendra:vrf:test:height=7"

	proofHex := proveOk(t, skHex, alpha)
	if len(proofHex) != 2*vrf.ProofSize {
		t.Fatalf("proof length got %d hex chars, want %d", len(proofHex), 2*vrf.ProofSize)
	}
	if _, err := hex.DecodeString(proofHex); err != nil {
		t.Fatalf("prove output got %q, want hex: %v", proofHex, err)
	}

	stdout, stderr, code := runMain(t, []string{"dendra-vrf", "verify", pkHex, alpha, proofHex}, nil)
	if code != 0 {
		t.Fatalf("verify exit code = %d, want 0 (stderr: %q)", code, stderr)
	}
	if stderr != "" {
		t.Fatalf("verify stderr got %q, want %q", stderr, "")
	}
	line := strings.TrimSuffix(stdout, "\n")
	if !strings.HasPrefix(line, "VALID ") {
		t.Fatalf("verify stdout got %q, want it to start with %q", stdout, "VALID ")
	}
	betaHex := strings.TrimPrefix(line, "VALID ") // the beta follows the word and one space
	beta, err := hex.DecodeString(betaHex)
	if err != nil {
		t.Fatalf("verify beta field: %v", err)
	}
	if len(beta) != vrf.OutputSize {
		t.Fatalf("beta length got %d bytes, want %d", len(beta), vrf.OutputSize)
	}

	stdout, stderr, code = runMain(t, []string{"dendra-vrf", "pubkey", skHex}, nil)
	if code != 0 {
		t.Fatalf("pubkey exit code = %d, want 0 (stderr: %q)", code, stderr)
	}
	if got, want := strings.TrimSuffix(stdout, "\n"), pkHex; got != want {
		t.Fatalf("pubkey got %q, want the keygen public key %q", got, want)
	}
}

// DENDRA_VRF_SK supplies the secret for prove and pubkey; the outputs equal those of the argument
// form.
func TestSecretFromEnv(t *testing.T) {
	skHex, pkHex := keygenPair(t)
	alpha := "dendra:vrf:test:env=1"

	wantProof := proveOk(t, skHex, alpha) + "\n"
	stdout, stderr, code := runMain(t, []string{"dendra-vrf", "prove", alpha}, map[string]string{"DENDRA_VRF_SK": skHex})
	if code != 0 {
		t.Fatalf("prove with DENDRA_VRF_SK exit code = %d, want 0 (stderr: %q)", code, stderr)
	}
	if stderr != "" {
		t.Fatalf("prove with DENDRA_VRF_SK stderr got %q, want %q", stderr, "")
	}
	if stdout != wantProof {
		t.Fatalf("prove via DENDRA_VRF_SK got %q, want the argument form %q", stdout, wantProof)
	}

	stdout, stderr, code = runMain(t, []string{"dendra-vrf", "pubkey"}, map[string]string{"DENDRA_VRF_SK": skHex})
	if code != 0 {
		t.Fatalf("pubkey with DENDRA_VRF_SK exit code = %d, want 0 (stderr: %q)", code, stderr)
	}
	if got, want := strings.TrimSuffix(stdout, "\n"), pkHex; got != want {
		t.Fatalf("pubkey via DENDRA_VRF_SK got %q, want %q", got, want)
	}
}

// verify answers "INVALID" on stdout and exits 1 (main prints "INVALID", then calls os.Exit(1))
// for a proof made on another alpha, a proof changed in one byte, and a proof under another public
// key.
func TestVerifyInvalid(t *testing.T) {
	skHex, pkHex := keygenPair(t)
	_, otherPkHex := keygenPair(t)
	alpha := "dendra:vrf:test:alpha=A"
	otherAlpha := "dendra:vrf:test:alpha=B"
	proofHex := proveOk(t, skHex, alpha)

	tests := []struct {
		name string
		args []string
	}{
		{"proof made on another alpha", []string{"dendra-vrf", "verify", pkHex, otherAlpha, proofHex}},
		{"proof changed in one byte", []string{"dendra-vrf", "verify", pkHex, alpha, flipFirstByte(t, proofHex)}},
		{"another public key", []string{"dendra-vrf", "verify", otherPkHex, alpha, proofHex}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			stdout, stderr, code := runMain(t, tt.args, nil)
			if code != 1 {
				t.Fatalf("exit code got %d, want 1 (stderr: %q)", code, stderr)
			}
			if got, want := stdout, "INVALID\n"; got != want {
				t.Fatalf("stdout got %q, want %q", got, want)
			}
			if stderr != "" {
				t.Fatalf("stderr got %q, want %q", stderr, "")
			}
		})
	}
}

// Usage errors exit 2 and print the usage line on stderr, with nothing on stdout.
func TestUsageErrors(t *testing.T) {
	skHex := strings.Repeat("00", vrf.PrivateKeySize)
	pkHex := strings.Repeat("00", vrf.PublicKeySize)
	// The values above are valid hex on purpose: arity is checked before the key is decoded, and
	// these rows stay about the missing argument alone.
	tests := []struct {
		name string
		args []string
		env  map[string]string
	}{
		{"no command", []string{"dendra-vrf"}, nil},
		{"unknown command", []string{"dendra-vrf", "frobnicate"}, nil},
		{"prove without alpha", []string{"dendra-vrf", "prove", skHex}, nil},
		{"pubkey without secret", []string{"dendra-vrf", "pubkey"}, nil},
		{"verify without proof", []string{"dendra-vrf", "verify", pkHex, "alpha"}, nil},
		{"prove via DENDRA_VRF_SK without alpha", []string{"dendra-vrf", "prove"}, map[string]string{"DENDRA_VRF_SK": skHex}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			stdout, stderr, code := runMain(t, tt.args, tt.env)
			if code != 2 {
				t.Fatalf("exit code got %d, want 2 (stderr: %q)", code, stderr)
			}
			if !strings.Contains(stderr, usageLine) {
				t.Fatalf("stderr got %q, want it to contain the usage line %q", stderr, usageLine)
			}
			if stdout != "" {
				t.Fatalf("stdout got %q, want %q", stdout, "")
			}
		})
	}
}

// Invalid hex in the secret, the public key or the proof exits 1 and reports it on stderr.
func TestInvalidHex(t *testing.T) {
	skHex, pkHex := keygenPair(t)
	proofHex := proveOk(t, skHex, "dendra:vrf:test:hex=1")
	tests := []struct {
		name string
		args []string
		env  map[string]string
	}{
		{"prove: bad secret", []string{"dendra-vrf", "prove", "zz", "alpha"}, nil},
		{"prove: secret of odd length", []string{"dendra-vrf", "prove", "abc", "alpha"}, nil},
		{"prove: bad secret in DENDRA_VRF_SK", []string{"dendra-vrf", "prove", "alpha"}, map[string]string{"DENDRA_VRF_SK": "zz"}},
		{"pubkey: bad secret", []string{"dendra-vrf", "pubkey", "zz"}, nil},
		{"verify: bad public key", []string{"dendra-vrf", "verify", "zz", "alpha", proofHex}, nil},
		{"verify: bad proof", []string{"dendra-vrf", "verify", pkHex, "alpha", "q"}, nil},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			stdout, stderr, code := runMain(t, tt.args, tt.env)
			if code != 1 {
				t.Fatalf("exit code got %d, want 1 (stderr: %q)", code, stderr)
			}
			if !strings.HasPrefix(stderr, "dendra-vrf: invalid hex") {
				t.Fatalf("stderr got %q, want it to start with %q", stderr, "dendra-vrf: invalid hex")
			}
			if stdout != "" {
				t.Fatalf("stdout got %q, want %q", stdout, "")
			}
		})
	}
}

// The secret never appears on stdout or stderr of prove (either form of passing it) or verify.
// The seed half is watched as well: an Ed25519 secret is seed || public key, and only the public
// half may show.
func TestSecretNotPrinted(t *testing.T) {
	skHex, pkHex := keygenPair(t)
	alpha := "dendra:vrf:test:quiet=1"
	seedHex := skHex[:2*(vrf.PrivateKeySize-vrf.PublicKeySize)]

	runs := []struct {
		name string
		args []string
		env  map[string]string
	}{
		{"prove with the secret in argv", []string{"dendra-vrf", "prove", skHex, alpha}, nil},
		{"prove with the secret in DENDRA_VRF_SK", []string{"dendra-vrf", "prove", alpha}, map[string]string{"DENDRA_VRF_SK": skHex}},
	}
	for _, tt := range runs {
		t.Run(tt.name, func(t *testing.T) {
			stdout, stderr, _ := runMain(t, tt.args, tt.env)
			for _, secret := range []string{skHex, seedHex} {
				if strings.Contains(stdout, secret) || strings.Contains(stderr, secret) {
					t.Fatalf("the secret %q appears in the output of prove: stdout %q, stderr %q", secret, stdout, stderr)
				}
			}
		})
	}

	stdout, stderr, _ := runMain(t, []string{"dendra-vrf", "verify", pkHex, alpha, proveOk(t, skHex, alpha)}, nil)
	for _, secret := range []string{skHex, seedHex} {
		if strings.Contains(stdout, secret) || strings.Contains(stderr, secret) {
			t.Fatalf("the secret %q appears in the output of verify: stdout %q, stderr %q", secret, stdout, stderr)
		}
	}
}

// The vrf package's own tests hold no fixed vector: every one of them generates its key pair at
// run time. The expected values are therefore computed with the package itself, and the command
// must print the same proof and the same beta.
func TestMatchesVrfPackage(t *testing.T) {
	pk, sk, err := vrf.GenerateKey(nil)
	if err != nil {
		t.Fatalf("vrf.GenerateKey: %v", err)
	}
	alpha := "dendra:vrf:test:package=1"
	pi, err := vrf.Prove(sk, []byte(alpha))
	if err != nil {
		t.Fatalf("vrf.Prove: %v", err)
	}
	ok, beta := vrf.Verify(pk, []byte(alpha), pi)
	if !ok {
		t.Fatal("vrf.Verify rejected the proof the package itself produced")
	}

	stdout, stderr, code := runMain(t, []string{"dendra-vrf", "prove", hex.EncodeToString(sk), alpha}, nil)
	if code != 0 {
		t.Fatalf("prove exit code = %d, want 0 (stderr: %q)", code, stderr)
	}
	if got, want := stdout, hex.EncodeToString(pi)+"\n"; got != want {
		t.Fatalf("prove got %q, want the package proof %q", got, want)
	}

	stdout, stderr, code = runMain(t, []string{"dendra-vrf", "verify", hex.EncodeToString(pk), alpha, hex.EncodeToString(pi)}, nil)
	if code != 0 {
		t.Fatalf("verify exit code = %d, want 0 (stderr: %q)", code, stderr)
	}
	if got, want := stdout, "VALID "+hex.EncodeToString(beta)+"\n"; got != want {
		t.Fatalf("verify got %q, want %q", got, want)
	}
}

// TestHelperProcess is the child side of the re-exec pattern. When GO_WANT_HELPER_PROCESS is 1,
// the arguments after "--" become os.Args and main() runs; without the variable (the parent's own
// run of the suite) it returns at once. main() exits the process on every error path, so reaching
// os.Exit(0) means the command succeeded.
func TestHelperProcess(t *testing.T) {
	if os.Getenv("GO_WANT_HELPER_PROCESS") != "1" {
		return
	}
	args := os.Args
	for i, a := range args {
		if a == "--" {
			args = append([]string(nil), args[i+1:]...)
			break
		}
	}
	os.Args = args
	main()
	os.Exit(0)
}
