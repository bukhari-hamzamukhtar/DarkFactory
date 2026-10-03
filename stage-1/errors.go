package main

import "net/http"

// apiError is the one failure currency in this service. Every 4xx and 5xx leaves
// the process as one of these, so the envelope in §5 is built in exactly one place.
type apiError struct {
	status  int
	code    string
	message string
}

func (e *apiError) Error() string { return e.code + ": " + e.message }

func errorf(status int, code, message string) *apiError {
	return &apiError{status: status, code: code, message: message}
}

// The §5 table.

func malformed(message string) *apiError {
	return errorf(http.StatusBadRequest, "malformed_request", message)
}

func missingIdempotencyKey() *apiError {
	return errorf(http.StatusBadRequest, "missing_idempotency_key",
		"an Idempotency-Key header is required on this request")
}

func unauthenticated() *apiError {
	return errorf(http.StatusUnauthorized, "unauthenticated",
		"a valid bearer token is required")
}

func notFound() *apiError {
	return errorf(http.StatusNotFound, "not_found", "no such resource")
}

func idempotencyKeyReuse() *apiError {
	return errorf(http.StatusConflict, "idempotency_key_reuse",
		"this Idempotency-Key was already used with a different request body")
}

func validationFailed(message string) *apiError {
	return errorf(http.StatusUnprocessableEntity, "validation_failed", message)
}

// Endpoint-specific codes.

func unprocessable(code, message string) *apiError {
	return errorf(http.StatusUnprocessableEntity, code, message)
}

func conflict(code, message string) *apiError {
	return errorf(http.StatusConflict, code, message)
}
