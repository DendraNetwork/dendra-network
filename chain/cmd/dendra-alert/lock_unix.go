//go:build unix

package main

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"syscall"
)

var errLocked = errors.New("locked")

// lockDir takes an exclusive lock on run.lock in the state directory without waiting. The lock goes
// when the process ends, however it ends.
func lockDir(dir string) (func(), error) {
	f, err := os.OpenFile(filepath.Join(dir, "run.lock"), os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return nil, fmt.Errorf("cannot open the lock file: %w", err)
	}
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		f.Close()
		if errors.Is(err, syscall.EWOULDBLOCK) {
			return nil, errLocked
		}
		return nil, fmt.Errorf("cannot take the lock: %w", err)
	}
	return func() { f.Close() }, nil
}
