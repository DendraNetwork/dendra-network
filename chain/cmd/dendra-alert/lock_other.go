//go:build !unix

package main

import "errors"

var errLocked = errors.New("locked")

// lockDir is not available off Unix: the tool runs on the chain host only.
func lockDir(dir string) (func(), error) {
	return nil, errors.New("dendra-alert runs on a Unix host only")
}
