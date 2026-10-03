package main

import (
	"encoding/json"
	"log"
	"net/http"
	"strings"
	"sync"
	"unicode/utf8"
)

// Canonical paths. An idempotency key is scoped to the caller *and* the path (§7),
// so the scope must come from the route rather than from whatever the client typed.
const (
	pathReservations    = "/reservations"
	pathReservationMove = "/reservation-moves"
)

const maxIdempotencyKeyLength = 255

// server owns the whole store behind one mutex.
//
// Every request that reads or writes takes that lock for its entire decision, so a
// booking decision and the write it implies can never be split by another request.
// That is what makes §1's invariant hold under the 50 concurrent requests of §2
// without optimistic retries, and why no request can observe a half-applied batch.
// The work under the lock is small in-memory scanning; password hashing, which is
// deliberately slow, is kept outside it.
type server struct {
	mu sync.Mutex
	st *state
}

func newServer() *server {
	return &server{st: newState()}
}

func (s *server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	defer func() {
		if recovered := recover(); recovered != nil {
			log.Printf("panic serving %s %s: %v", r.Method, r.URL.Path, recovered)
			writeError(w, errorf(http.StatusInternalServerError, "internal_error",
				"the request could not be served"))
		}
	}()
	s.route(w, r)
}

func (s *server) route(w http.ResponseWriter, r *http.Request) {
	segments := splitPath(r.URL.Path)
	method := r.Method

	switch {
	case len(segments) == 1 && segments[0] == "health":
		if method == http.MethodGet {
			writeJSON(w, http.StatusOK, map[string]any{"status": "ok"})
			return
		}

	case len(segments) == 2 && segments[0] == "_test":
		switch {
		case segments[1] == "reset" && method == http.MethodPost:
			s.handleReset(w, r)
			return
		case segments[1] == "export" && method == http.MethodGet:
			s.handleExport(w, r)
			return
		case segments[1] == "import" && method == http.MethodPost:
			s.handleImport(w, r)
			return
		}

	case len(segments) == 2 && segments[0] == "auth":
		switch {
		case segments[1] == "signup" && method == http.MethodPost:
			s.handleSignup(w, r)
			return
		case segments[1] == "login" && method == http.MethodPost:
			s.handleLogin(w, r)
			return
		}

	case len(segments) == 1 && segments[0] == "restaurants":
		if method == http.MethodGet {
			s.handleListRestaurants(w, r)
			return
		}

	case len(segments) == 2 && segments[0] == "restaurants":
		if method == http.MethodGet {
			s.handleGetRestaurant(w, r, segments[1])
			return
		}

	case len(segments) == 1 && segments[0] == "availability":
		if method == http.MethodGet {
			s.handleAvailability(w, r)
			return
		}

	case len(segments) == 1 && segments[0] == "reservations":
		switch method {
		case http.MethodPost:
			s.handleCreateReservation(w, r)
			return
		case http.MethodGet:
			s.handleListReservations(w, r)
			return
		}

	case len(segments) == 2 && segments[0] == "reservations":
		switch method {
		case http.MethodGet:
			s.handleGetReservation(w, r, segments[1])
			return
		case http.MethodPatch:
			s.handlePatchReservation(w, r, segments[1])
			return
		}

	case len(segments) == 3 && segments[0] == "reservations" && segments[2] == "cancel":
		if method == http.MethodPost {
			s.handleCancelReservation(w, r, segments[1])
			return
		}

	case len(segments) == 1 && segments[0] == "reservation-moves":
		if method == http.MethodPost {
			s.handleReservationMoves(w, r)
			return
		}
	}
	// An unknown path, or a method this service does not offer there, is simply a
	// resource that is not available: §5 has no code for either case.
	writeError(w, notFound())
}

func splitPath(path string) []string {
	trimmed := strings.Trim(path, "/")
	if trimmed == "" {
		return nil
	}
	return strings.Split(trimmed, "/")
}

// ---- request plumbing ----------------------------------------------------

// bearerToken reads the credential without resolving it: resolution happens under
// the store lock, so a handler never decides on a session that has since been reset.
func bearerToken(r *http.Request) (string, *apiError) {
	header := r.Header.Get("Authorization")
	if header == "" {
		return "", unauthenticated()
	}
	if len(header) < 7 || !strings.EqualFold(header[:7], "bearer ") {
		return "", unauthenticated()
	}
	token := strings.TrimSpace(header[7:])
	if token == "" {
		return "", unauthenticated()
	}
	return token, nil
}

func idempotencyKeyOf(r *http.Request) (string, *apiError) {
	key := r.Header.Get("Idempotency-Key")
	if strings.TrimSpace(key) == "" {
		return "", missingIdempotencyKey()
	}
	if utf8.RuneCountInString(key) > maxIdempotencyKeyLength {
		return "", validationFailed("Idempotency-Key must be 1 to 255 characters")
	}
	return key, nil
}

// callerUnderLock resolves the bearer token against the live store. The caller must
// already hold s.mu.
func (s *server) callerUnderLock(token string) (*user, *apiError) {
	caller := s.st.userByToken(token)
	if caller == nil {
		return nil, unauthenticated()
	}
	return caller, nil
}

// mutation is one idempotent write: it runs with the store lock held and either
// returns the response to record or an error that leaves the store untouched.
type mutation func(caller *user, body *jsonBody) (int, any, *apiError)

// serveIdempotent implements §7 for a write path, in the order §7 states: parse the
// body, authenticate, resolve the key, and only then validate fields or touch the
// resource. The lookup, the mutation and the receipt all happen inside one lock, so
// concurrent identical first uses settle as exactly one 201 and one or more 200s with
// the same body, and the operation takes effect once.
func (s *server) serveIdempotent(w http.ResponseWriter, r *http.Request, path string, mutate mutation) {
	raw, aerr := readBody(r)
	if aerr != nil {
		writeError(w, aerr)
		return
	}
	body, aerr := parseJSONObject(raw)
	if aerr != nil {
		writeError(w, aerr)
		return
	}
	token, aerr := bearerToken(r)
	if aerr != nil {
		writeError(w, aerr)
		return
	}
	key, aerr := idempotencyKeyOf(r)
	if aerr != nil {
		writeError(w, aerr)
		return
	}

	s.mu.Lock()
	defer s.mu.Unlock()

	caller, aerr := s.callerUnderLock(token)
	if aerr != nil {
		writeError(w, aerr)
		return
	}
	if rec := s.st.receiptFor(caller.ID, r.Method, path, key); rec != nil {
		if rec.BodyHash != body.digest {
			writeError(w, idempotencyKeyReuse())
			return
		}
		// A replay returns the original response and changes nothing, however the
		// resource has moved on since (§7).
		writeRaw(w, http.StatusOK, rec.Response)
		return
	}

	status, value, aerr := mutate(caller, body)
	if aerr != nil {
		// The key stays reusable after a 4xx: it was never spent (§7).
		writeError(w, aerr)
		return
	}
	encoded, err := json.Marshal(value)
	if err != nil {
		writeError(w, errorf(http.StatusInternalServerError, "internal_error",
			"the response could not be encoded"))
		return
	}
	s.st.putReceipt(&receipt{
		UserID:   caller.ID,
		Method:   r.Method,
		Path:     path,
		Key:      key,
		BodyHash: body.digest,
		Status:   status,
		Response: encoded,
	})
	writeRaw(w, status, encoded)
}

// ---- responses -----------------------------------------------------------

const contentTypeJSON = "application/json; charset=utf-8"

func writeRaw(w http.ResponseWriter, status int, body []byte) {
	w.Header().Set("Content-Type", contentTypeJSON)
	w.WriteHeader(status)
	_, _ = w.Write(body)
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	encoded, err := json.Marshal(value)
	if err != nil {
		log.Printf("could not encode a %d response: %v", status, err)
		encoded = []byte(`{"error":{"code":"internal_error","message":"encoding failed"}}`)
		status = http.StatusInternalServerError
	}
	writeRaw(w, status, encoded)
}

func writeNoContent(w http.ResponseWriter) {
	w.WriteHeader(http.StatusNoContent)
}

func writeError(w http.ResponseWriter, err *apiError) {
	writeJSON(w, err.status, map[string]any{
		"error": map[string]any{"code": err.code, "message": err.message},
	})
}
