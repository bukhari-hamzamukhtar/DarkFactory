package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"net/http"
	"strings"
)

const maxBodyBytes = 8 << 20

// jsonBody is a request body kept as its parsed members plus a canonical digest.
//
// Members stay as raw JSON because §5 distinguishes a field of the wrong JSON type
// (400) from a field of the right type with a bad value (422), which means the
// handlers have to see the types rather than a coerced Go value. The digest is what
// §7 compares when deciding replay from reuse.
type jsonBody struct {
	members map[string]json.RawMessage
	digest  string
}

func readBody(r *http.Request) ([]byte, *apiError) {
	raw, err := io.ReadAll(io.LimitReader(r.Body, maxBodyBytes+1))
	if err != nil {
		return nil, malformed("request body could not be read")
	}
	if len(raw) > maxBodyBytes {
		return nil, malformed("request body is too large")
	}
	return raw, nil
}

// parseJSONObject parses a body that must be a JSON object, per §7's first step.
func parseJSONObject(raw []byte) (*jsonBody, *apiError) {
	members, err := decodeObject(raw)
	if err != nil {
		return nil, malformed("request body must be a JSON object")
	}
	digest, derr := canonicalDigest(raw)
	if derr != nil {
		return nil, malformed("request body must be a JSON object")
	}
	return &jsonBody{members: members, digest: digest}, nil
}

func decodeObject(raw []byte) (map[string]json.RawMessage, error) {
	dec := json.NewDecoder(bytes.NewReader(raw))
	var members map[string]json.RawMessage
	if err := dec.Decode(&members); err != nil {
		return nil, err
	}
	if members == nil {
		return nil, errNotAnObject
	}
	if err := expectEOF(dec); err != nil {
		return nil, err
	}
	return members, nil
}

var errNotAnObject = &jsonError{"not a JSON object"}

type jsonError struct{ msg string }

func (e *jsonError) Error() string { return e.msg }

func expectEOF(dec *json.Decoder) error {
	if _, err := dec.Token(); err != io.EOF {
		return &jsonError{"trailing content after the JSON value"}
	}
	return nil
}

// canonicalDigest hashes a body by its JSON *value*: key order and whitespace do not
// matter (§7). Numbers keep their literal text so no precision is invented.
func canonicalDigest(raw []byte) (string, error) {
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	var value any
	if err := dec.Decode(&value); err != nil {
		return "", err
	}
	// Go marshals map keys in sorted order, which is the canonical form we want.
	canonical, err := json.Marshal(value)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(canonical)
	return hex.EncodeToString(sum[:]), nil
}

// asObject re-reads a value as a JSON object, for the handful of responses that are a
// typed struct plus a field or two.
func asObject(value any) (map[string]any, error) {
	encoded, err := json.Marshal(value)
	if err != nil {
		return nil, err
	}
	var object map[string]any
	if err := json.Unmarshal(encoded, &object); err != nil {
		return nil, err
	}
	return object, nil
}

// ---- typed member access -------------------------------------------------

func jsonKind(raw json.RawMessage) string {
	trimmed := bytes.TrimSpace(raw)
	if len(trimmed) == 0 {
		return "null"
	}
	switch trimmed[0] {
	case '"':
		return "string"
	case '{':
		return "object"
	case '[':
		return "array"
	case 't', 'f':
		return "bool"
	case 'n':
		return "null"
	default:
		return "number"
	}
}

// stringMember returns a string field. A member of another JSON type is 400
// malformed_request (§5); absent or null counts as not present.
func (b *jsonBody) stringMember(name string) (string, bool, *apiError) {
	raw, ok := b.members[name]
	if !ok || jsonKind(raw) == "null" {
		return "", false, nil
	}
	if jsonKind(raw) != "string" {
		return "", false, malformed("field " + name + " must be a string")
	}
	var value string
	if err := json.Unmarshal(raw, &value); err != nil {
		return "", false, malformed("field " + name + " must be a string")
	}
	return value, true, nil
}

// requiredString adds §5's "a required field is missing" rule.
func (b *jsonBody) requiredString(name string) (string, *apiError) {
	value, present, err := b.stringMember(name)
	if err != nil {
		return "", err
	}
	if !present {
		return "", validationFailed("field " + name + " is required")
	}
	return value, nil
}

// integerMember reads a field that must be a plain JSON integer. Unlike the generic
// rule, *every* unusable value here is 422: §5 names `party_size` as an
// endpoint-specific field whose wrong types are validation failures, not 400s.
func (b *jsonBody) integerMember(name string) (int, bool, *apiError) {
	raw, ok := b.members[name]
	if !ok || jsonKind(raw) == "null" {
		return 0, false, nil
	}
	if jsonKind(raw) != "number" {
		return 0, false, validationFailed("field " + name + " must be an integer")
	}
	text := string(bytes.TrimSpace(raw))
	if !isPlainInteger(text) {
		return 0, false, validationFailed("field " + name + " must be an integer")
	}
	value, err := parseBoundedInt(text)
	if err != nil {
		return 0, false, validationFailed("field " + name + " is out of range")
	}
	return value, true, nil
}

// isPlainInteger accepts the decimal integer forms JSON allows and nothing else:
// no fraction, no exponent. §5 settles the equivalent query-parameter case the same
// way -- `4.0` is a validation failure whatever its numeric value.
func isPlainInteger(text string) bool {
	if text == "" {
		return false
	}
	digits := text
	if strings.HasPrefix(digits, "-") {
		digits = digits[1:]
	}
	if digits == "" {
		return false
	}
	for _, c := range digits {
		if c < '0' || c > '9' {
			return false
		}
	}
	return true
}
