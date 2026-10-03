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
	ID           string `json:"id"`
	Reference    string `json:"reference"`
	UserID       string `json:"user_id"`
	RestaurantID string `json:"restaurant_id"`
	// A seeded booking may hold either one table or a set of them (stage 2).
	TableID       string   `json:"table_id"`
	TableIDs      []string `json:"table_ids"`
	StartsAtLocal string   `json:"starts_at_local"`
	PartySize     int      `json:"party_size"`
	Status        string   `json:"status"`
	CreatedAt     string   `json:"created_at"`
}

// tables reads the seating a seeded booking names, in either spelling.
func (f fixtureReservation) tables() []string {
	if len(f.TableIDs) > 0 {
		return f.TableIDs
	}
	if f.TableID != "" {
		return []string{f.TableID}
	}
	return nil
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
		TableIDs:      seeded.tables(),
		StartsAtLocal: stamp.String(),
		StartsAt:      formatInstant(start, rest.loc),
		CreatedAt:     createdAt,
		PartySize:     seeded.PartySize,
		Status:        status,
		start:         start,
	}, nil
}

// seedHashCost picks the bcrypt work factor for a fixture's seeded passwords.
//
// Every stored password is bcrypt-hashed with its own salt, whatever the size of the
// fixture; what varies is the work factor, because §2 gives reset ten seconds and two
// vCPU. Measured on that budget, cost 8 is about 30 ms of CPU per account, so a
// fixture of 200 accounts spent 6.1 s of the ten and a larger one would have run out
// of time -- and a reset that times out fails every check after it. Each tier below
// holds the total near two seconds instead, by halving the factor as the count
// doubles. Interactive signup and login keep the full cost: there is one hash to do
// and a caller waiting for it.
func seedHashCost(seeded int) int {
	switch {
	case seeded <= 64:
		return bcryptCost
	case seeded <= 128:
		return bcryptCost - 1
	case seeded <= 256:
		return bcryptCost - 2
	case seeded <= 512:
		return bcryptCost - 3
	default:
		return bcryptCost - 4
	}
}

// hashFixturePasswords hashes seeded passwords in parallel across the CPUs, at the
// work factor seedHashCost allows for a fixture that size.
func hashFixturePasswords(users []fixtureUser) ([]string, *apiError) {
	hashes := make([]string, len(users))
	failures := make([]*apiError, len(users))
	cost := seedHashCost(len(users))
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
				hash, err := hashPasswordAtCost(users[i].Password, cost)
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

// stateMembers is every member an export of this service carries, and the JSON kind
// it carries it as.
var stateMembers = map[string]string{
	"users":        "array",
	"tokens":       "object",
	"restaurants":  "array",
	"reservations": "array",
	"receipts":     "array",
}

// checkStateShape refuses a state this service could not have produced.
//
// §10 says an invalid state is 422 and leaves the destination alone, and the state
// format is this service's own. Decoding alone is far too forgiving to enforce that:
// `{}` and `{"bogus": 1}` both decode into a Go value happily, and installing either
// would silently destroy the destination's accounts, tokens and bookings while
// answering 204. So every member a real export carries must be present and of the
// right kind, and nothing else may be.
//
// A member present but null is accepted, because an export from the preceding stage
// writes its empty collections that way, and §10 requires that export to be accepted
// unchanged.
func checkStateShape(raw json.RawMessage) *apiError {
	var members map[string]json.RawMessage
	if err := json.Unmarshal(raw, &members); err != nil {
		return validationFailed("state must be an object")
	}
	for name, kind := range stateMembers {
		member, present := members[name]
		if !present {
			return validationFailed("state is missing its " + name)
		}
		if found := jsonKind(member); found != kind && found != "null" {
			return validationFailed("state member " + name + " must be " + aKindOf(kind))
		}
	}
	for name := range members {
		if _, known := stateMembers[name]; !known {
			return validationFailed("state carries an unknown member " + name)
		}
	}
	return nil
}

func aKindOf(kind string) string {
	if kind == "array" {
		return "an array"
	}
	return "an object"
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
	if err := checkStateShape(rawState); err != nil {
		writeError(w, err)
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
