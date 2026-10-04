package main

import (
	"crypto/rand"
	"encoding/hex"
	"errors"
	"strconv"
	"strings"
)

// maxCountedValue bounds every integer the API accepts. Nothing in the domain needs
// a larger party or a longer interval, and a bound keeps arithmetic far from
// overflow instead of relying on it never happening.
const maxCountedValue = 1_000_000_000

// maxIDLength is §3.4's limit on every id the service accepts or emits.
const maxIDLength = 64

var errOutOfRange = errors.New("value out of range")

func parseBoundedInt(text string) (int, error) {
	value, err := strconv.Atoi(text)
	if err != nil {
		return 0, errOutOfRange
	}
	if value > maxCountedValue || value < -maxCountedValue {
		return 0, errOutOfRange
	}
	return value, nil
}

func randomHex(bytesLen int) string {
	buf := make([]byte, bytesLen)
	if _, err := rand.Read(buf); err != nil {
		panic("the system random source failed: " + err.Error())
	}
	return hex.EncodeToString(buf)
}

const referenceAlphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

// newReference is 6 characters of A-Z0-9, the short end of §8's 6..12 range.
func newReference() string {
	buf := make([]byte, 6)
	if _, err := rand.Read(buf); err != nil {
		panic("the system random source failed: " + err.Error())
	}
	out := make([]byte, 6)
	for i, b := range buf {
		out[i] = referenceAlphabet[int(b)%len(referenceAlphabet)]
	}
	return string(out)
}

func isValidReference(reference string) bool {
	if len(reference) < 6 || len(reference) > 12 {
		return false
	}
	for _, c := range reference {
		if !strings.ContainsRune(referenceAlphabet, c) {
			return false
		}
	}
	return true
}

// isValidID applies §3.4: ids are opaque, non-empty and at most 64 characters.
func isValidID(id string) bool {
	return id != "" && len(id) <= maxIDLength
}
