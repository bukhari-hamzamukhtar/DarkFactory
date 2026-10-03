package main

import (
	"encoding/json"
	"net/http"
	"runtime"
	"sync"
	"time"
)

// The test control surface of §3.3 and §10. These endpoints are part of the delivered
// image and take no authentication, by requirement.

const (
	exportTrack         = "tablekeeper"
	exportFormatVersion = 1
)

// fixture is §4's fixture format. It is close to the stored model but not identical:
// a fixture carries a plaintext password, which is never stored, and a reservation
// without the absolute instant the service derives for it.
type fixture struct {
	Users        []fixtureUser        `json:"users"`
	Restaurants  []*restaurant        `json:"restaurants"`
	Reservations []fixtureReservation `json:"reservations"`
}

type fixtureUser struct {
	ID          string `json:"id"`
	Email       string `json:"email"`
	Password    string `json:"password"`
	DisplayName string `json:"display_name"`
}

type fixtureReservation struct {
	ID            string `json:"id"`
	Reference     string `json:"reference"`
	UserID        string `json:"user_id"`
	RestaurantID  string `json:"restaurant_id"`
	TableID       string `json:"table_id"`
	StartsAtLocal string `json:"starts_at_local"`
	PartySize     int    `json:"party_size"`
	Status        string `json:"status"`
	CreatedAt     string `json:"created_at"`
}

func (s *server) handleReset(w http.ResponseWriter, r *http.Request) {
	raw, aerr := readBody(r)
	if aerr != nil {
		writeError(w, aerr)
		return
	}
	var incoming fixture
	if err := json.Unmarshal(raw, &incoming); err != nil {
		// A field of the wrong JSON type lands here too, which §5 calls malformed.
		writeError(w, malformed("the fixture could not be read as the documented shape"))
		return
	}
	built, aerr := buildFixtureState(&incoming)
	if aerr != nil {
		writeError(w, aerr)
		return
	}
	s.mu.Lock()
	s.st = built
	s.mu.Unlock()
	// Synchronous by construction: the next request takes the same lock and can only
	// see the fixture that was just installed (§3.3).
	writeNoContent(w)
}

// buildFixtureState turns a fixture into live state, or rejects it whole. Nothing is
// installed until every part of it validates, so a rejected reset leaves the previous
// world intact.
func buildFixtureState(in *fixture) (*state, *apiError) {
	built := &state{Tokens: map[string]string{}}

	hashes, aerr := hashFixturePasswords(in.Users)
	if aerr != nil {
		return nil, aerr
	}
	for i, u := range in.Users {
		built.Users = append(built.Users, &user{
			ID:           u.ID,
			Email:        u.Email,
			DisplayName:  u.DisplayName,
			PasswordHash: hashes[i],
		})
	}
	built.Restaurants = in.Restaurants

	// Validate users and restaurants first: resolving a seeded booking's local time
	// needs its restaurant's zone, which only exists once the restaurant is sound.
	if err := built.prepare(); err != nil {
		return nil, err
	}

	for _, seeded := range in.Reservations {
		res, err := seededReservation(built, seeded)
		if err != nil {
			return nil, err
		}
		built.Reservations = append(built.Reservations, res)
	}
	if err := built.prepare(); err != nil {
		return nil, err
	}
	return built, nil
}

func seededReservation(built *state, seeded fixtureReservation) (*reservation, *apiError) {
	rest := built.restaurantsByID[seeded.RestaurantID]
	if rest == nil {
		return nil, validationFailed("a seeded reservation names an unknown restaurant")
	}
	stamp, ok := parseCivilStamp(seeded.StartsAtLocal)
	if !ok {
		return nil, validationFailed("a seeded starts_at_local must be YYYY-MM-DDTHH:MM")
	}
	start, exists := resolveLocal(rest.loc, stamp)
	if !exists {
		return nil, validationFailed(
			"a seeded reservation starts at a local time that does not exist")
	}
	status := seeded.Status
	if status == "" {
		status = statusConfirmed
	}
	id := seeded.ID
	if id == "" {
		id = "res_" + randomHex(8)
	}
	reference := seeded.Reference
	if reference == "" {
		reference = built.freshReference()
	}
	createdAt := seeded.CreatedAt
	if createdAt == "" {
		createdAt = formatUTC(time.Now())
	}
	return &reservation{
		ID:            id,
		Reference:     reference,
		UserID:        seeded.UserID,
		RestaurantID:  seeded.RestaurantID,
		TableID:       seeded.TableID,
		StartsAtLocal: stamp.String(),
		StartsAt:      formatInstant(start, rest.loc),
		CreatedAt:     createdAt,
		PartySize:     seeded.PartySize,
		Status:        status,
		start:         start,
	}, nil
}

// hashFixturePasswords hashes seeded passwords in parallel. Hashing is deliberately
// slow, and a fixture may seed many accounts; §2 gives reset 10 seconds, so the work
// is spread across the available CPUs instead of being done one at a time.
func hashFixturePasswords(users []fixtureUser) ([]string, *apiError) {
	hashes := make([]string, len(users))
	failures := make([]*apiError, len(users))
	workers := runtime.NumCPU()
	if workers < 1 {
		workers = 1
	}
	jobs := make(chan int)
	var wait sync.WaitGroup
	for worker := 0; worker < workers; worker++ {
		wait.Add(1)
		go func() {
			defer wait.Done()
			for i := range jobs {
				if users[i].Password == "" {
					// No password seeded: the account exists but cannot be logged
					// into, rather than being logged into with an empty password.
					hashes[i] = ""
					continue
				}
				hash, err := hashPassword(users[i].Password)
				if err != nil {
					failures[i] = err
					continue
				}
				hashes[i] = hash
			}
		}()
	}
	for i := range users {
		jobs <- i
	}
	close(jobs)
	wait.Wait()
	for _, err := range failures {
		if err != nil {
			return nil, validationFailed("a seeded password could not be hashed")
		}
	}
	return hashes, nil
}

// ---- export and import ---------------------------------------------------

func (s *server) handleExport(w http.ResponseWriter, r *http.Request) {
	s.mu.Lock()
	// Encoding under the lock is what makes the snapshot atomic and read-only: the
	// bytes are complete before any later write can touch the store (§10).
	encoded, err := json.Marshal(s.st)
	s.mu.Unlock()
	if err != nil {
		writeError(w, errorf(http.StatusInternalServerError, "internal_error",
			"the state could not be encoded"))
		return
	}
	writeRaw(w, http.StatusOK, mustEncodeExport(encoded))
}

func mustEncodeExport(stateJSON []byte) []byte {
	envelope, err := json.Marshal(map[string]any{
		"track":          exportTrack,
		"format_version": exportFormatVersion,
		"state":          json.RawMessage(stateJSON),
	})
	if err != nil {
		panic("the export envelope must always encode: " + err.Error())
	}
	return envelope
}

// isNumberEqualTo compares a JSON number by value, so an export whose version was
// re-encoded as 1.0 by some intermediary is still version 1.
func isNumberEqualTo(raw json.RawMessage, want int) bool {
	if jsonKind(raw) != "number" {
		return false
	}
	var value float64
	if err := json.Unmarshal(raw, &value); err != nil {
		return false
	}
	return value == float64(want)
}

func (s *server) handleImport(w http.ResponseWriter, r *http.Request) {
	body, aerr := s.readObjectBody(r)
	if aerr != nil {
		writeError(w, aerr)
		return
	}
	// A missing field, the wrong track or version, or an unusable state are all
	// validation failures here, and none of them may touch the destination (§10).
	track, present, _ := body.stringMember("track")
	if !present || track != exportTrack {
		writeError(w, validationFailed("track must be \"tablekeeper\""))
		return
	}
	rawVersion, present := body.members["format_version"]
	if !present || !isNumberEqualTo(rawVersion, exportFormatVersion) {
		writeError(w, validationFailed("format_version must be 1"))
		return
	}
	rawState, present := body.members["state"]
	if !present || jsonKind(rawState) != "object" {
		writeError(w, validationFailed("state must be an object"))
		return
	}
	var incoming state
	if err := json.Unmarshal(rawState, &incoming); err != nil {
		writeError(w, validationFailed("state is not a state this service can hold"))
		return
	}
	if err := incoming.prepare(); err != nil {
		writeError(w, err)
		return
	}
	s.mu.Lock()
	// Replacement, not merge: the destination's own accounts, tokens, bookings and
	// receipts go away with the old state (§10).
	s.st = &incoming
	s.mu.Unlock()
	writeNoContent(w)
}
