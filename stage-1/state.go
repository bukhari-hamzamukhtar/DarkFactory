package main

import (
	"encoding/json"
	"strings"
	"time"
)

// state is the whole world: §4's model, the live sessions, and the idempotency
// receipts of §7. One type serves three jobs -- in-memory store, export payload and
// import payload -- so an export can never drift from what the service actually
// holds, which is what §10 asks for.
type state struct {
	Users        []*user           `json:"users"`
	Tokens       map[string]string `json:"tokens"`
	Restaurants  []*restaurant     `json:"restaurants"`
	Reservations []*reservation    `json:"reservations"`
	Receipts     []*receipt        `json:"receipts"`

	usersByID         map[string]*user
	usersByEmail      map[string]*user
	restaurantsByID   map[string]*restaurant
	reservationsByRef map[string]*reservation
	receiptsByKey     map[string]*receipt
}

func newState() *state {
	empty := &state{Tokens: map[string]string{}}
	if err := empty.prepare(); err != nil {
		panic("the empty state must always be valid: " + err.Error())
	}
	return empty
}

// prepare validates a freshly decoded state and builds every index the request paths
// use. It is the single gate in front of the store: a fixture (§3.3) and an import
// (§10) both arrive here, so neither can install something the API could not have
// produced itself. On any failure the caller keeps its previous state untouched.
func (s *state) prepare() *apiError {
	if s.Tokens == nil {
		s.Tokens = map[string]string{}
	}
	s.usersByID = map[string]*user{}
	s.usersByEmail = map[string]*user{}
	s.restaurantsByID = map[string]*restaurant{}
	s.reservationsByRef = map[string]*reservation{}
	s.receiptsByKey = map[string]*receipt{}

	for _, u := range s.Users {
		if u == nil {
			return validationFailed("a user entry is null")
		}
		if !isValidID(u.ID) {
			return validationFailed("user id must be 1 to 64 characters")
		}
		if _, clash := s.usersByID[u.ID]; clash {
			return validationFailed("duplicate user id " + u.ID)
		}
		if u.Email == "" {
			return validationFailed("user email is required")
		}
		folded := strings.ToLower(u.Email)
		if _, clash := s.usersByEmail[folded]; clash {
			return validationFailed("duplicate user email " + u.Email)
		}
		s.usersByID[u.ID] = u
		s.usersByEmail[folded] = u
	}

	for _, r := range s.Restaurants {
		if err := s.prepareRestaurant(r); err != nil {
			return err
		}
	}

	for _, res := range s.Reservations {
		if err := s.prepareReservation(res); err != nil {
			return err
		}
	}
	if err := s.checkNoOverlap(); err != nil {
		return err
	}

	for token, userID := range s.Tokens {
		if token == "" {
			return validationFailed("a session token is empty")
		}
		if _, known := s.usersByID[userID]; !known {
			return validationFailed("a session token names an unknown user")
		}
	}

	for _, rec := range s.Receipts {
		if rec == nil {
			return validationFailed("an idempotency receipt is null")
		}
		if rec.Key == "" || rec.Method == "" || rec.Path == "" {
			return validationFailed("an idempotency receipt is incomplete")
		}
		if _, known := s.usersByID[rec.UserID]; !known {
			return validationFailed("an idempotency receipt names an unknown user")
		}
		if !json.Valid(rec.Response) {
			return validationFailed("an idempotency receipt holds no JSON response")
		}
		s.receiptsByKey[receiptKey(rec.UserID, rec.Method, rec.Path, rec.Key)] = rec
	}
	return nil
}

func (s *state) prepareRestaurant(r *restaurant) *apiError {
	if r == nil {
		return validationFailed("a restaurant entry is null")
	}
	if !isValidID(r.ID) {
		return validationFailed("restaurant id must be 1 to 64 characters")
	}
	if _, clash := s.restaurantsByID[r.ID]; clash {
		return validationFailed("duplicate restaurant id " + r.ID)
	}
	loc, err := time.LoadLocation(r.Timezone)
	if err != nil {
		return validationFailed("unknown IANA timezone " + r.Timezone)
	}
	r.loc = loc
	if r.SlotMinutes < 1 || r.SlotMinutes > 24*60 {
		return validationFailed("slot_minutes must be between 1 and 1440")
	}
	if r.DurationMinutes < 1 || r.DurationMinutes > maxCountedValue {
		return validationFailed("reservation_duration_minutes must be at least 1")
	}
	if r.CutoffMinutes < 0 || r.CutoffMinutes > maxCountedValue {
		return validationFailed("cancellation_cutoff_minutes must not be negative")
	}

	r.byDay = map[string]openingHours{}
	for _, hours := range r.OpeningHours {
		if !isWeekdayKey(hours.Weekday) {
			return validationFailed("weekday must be one of mon tue wed thu fri sat sun")
		}
		if _, clash := r.byDay[hours.Weekday]; clash {
			return validationFailed("two opening_hours entries for " + hours.Weekday)
		}
		opens, okOpens := parseHourMinute(hours.Opens)
		closes, okCloses := parseHourMinute(hours.Closes)
		if !okOpens || !okCloses {
			return validationFailed("opens and closes must be local HH:MM")
		}
		if closes <= opens {
			return validationFailed("closes must be later than opens on the same day")
		}
		r.byDay[hours.Weekday] = hours
	}

	r.byTable = map[string]*table{}
	for i := range r.Tables {
		t := &r.Tables[i]
		if !isValidID(t.ID) {
			return validationFailed("table id must be 1 to 64 characters")
		}
		if _, clash := r.byTable[t.ID]; clash {
			return validationFailed("duplicate table id " + t.ID)
		}
		if t.Capacity < 1 || t.Capacity > maxCountedValue {
			return validationFailed("table capacity must be at least 1")
		}
		r.byTable[t.ID] = t
	}
	s.restaurantsByID[r.ID] = r
	return nil
}

func (s *state) prepareReservation(res *reservation) *apiError {
	if res == nil {
		return validationFailed("a reservation entry is null")
	}
	if !isValidID(res.ID) {
		return validationFailed("reservation id must be 1 to 64 characters")
	}
	if !isValidReference(res.Reference) {
		return validationFailed("reservation reference must be 6 to 12 characters of A-Z0-9")
	}
	if _, clash := s.reservationsByRef[res.Reference]; clash {
		return validationFailed("duplicate reservation reference " + res.Reference)
	}
	if _, known := s.usersByID[res.UserID]; !known {
		return validationFailed("reservation " + res.Reference + " names an unknown user")
	}
	rest, known := s.restaurantsByID[res.RestaurantID]
	if !known {
		return validationFailed("reservation " + res.Reference + " names an unknown restaurant")
	}
	if _, known := rest.byTable[res.TableID]; !known {
		return validationFailed("reservation " + res.Reference + " names an unknown table")
	}
	if res.PartySize < 1 || res.PartySize > maxCountedValue {
		return validationFailed("reservation party_size must be at least 1")
	}
	if res.Status != statusConfirmed && res.Status != statusCancelled {
		return validationFailed("reservation status must be confirmed or cancelled")
	}
	if _, ok := parseCivilStamp(res.StartsAtLocal); !ok {
		return validationFailed("reservation starts_at_local must be YYYY-MM-DDTHH:MM")
	}
	start, err := time.Parse(offsetLayout, res.StartsAt)
	if err != nil {
		return validationFailed("reservation starts_at must be RFC 3339 with an offset")
	}
	res.start = start
	if _, err := time.Parse(offsetLayout, res.CreatedAt); err != nil {
		return validationFailed("reservation created_at must be RFC 3339 with an offset")
	}
	s.reservationsByRef[res.Reference] = res
	return nil
}

// checkNoOverlap enforces §1's invariant on any state that is about to go live. A
// fixture or an import that double-books a table is rejected rather than installed,
// because no sequence of API calls could have produced it.
func (s *state) checkNoOverlap() *apiError {
	type place struct{ restaurantID, tableID string }
	held := map[place][]*reservation{}
	for _, res := range s.Reservations {
		if !res.occupies() {
			continue
		}
		rest := s.restaurantsByID[res.RestaurantID]
		where := place{res.RestaurantID, res.TableID}
		for _, other := range held[where] {
			if other.overlaps(res.start, rest.duration()) {
				return validationFailed("reservations " + other.Reference + " and " +
					res.Reference + " occupy table " + res.TableID + " at overlapping times")
			}
		}
		held[where] = append(held[where], res)
	}
	return nil
}

// ---- queries -------------------------------------------------------------

func (s *state) userByToken(token string) *user {
	userID, ok := s.Tokens[token]
	if !ok {
		return nil
	}
	return s.usersByID[userID]
}

func (s *state) userByEmail(email string) *user {
	return s.usersByEmail[strings.ToLower(email)]
}

func (s *state) reservationByReference(reference string) *reservation {
	return s.reservationsByRef[reference]
}

// ownedReservation applies §8's "404 if it is not the caller's": another diner's
// booking is indistinguishable from one that does not exist.
func (s *state) ownedReservation(owner *user, reference string) (*reservation, *apiError) {
	res := s.reservationByReference(reference)
	if res == nil || res.UserID != owner.ID {
		return nil, notFound()
	}
	return res, nil
}

func (s *state) receiptFor(userID, method, path, key string) *receipt {
	return s.receiptsByKey[receiptKey(userID, method, path, key)]
}

func (s *state) putReceipt(rec *receipt) {
	s.Receipts = append(s.Receipts, rec)
	s.receiptsByKey[receiptKey(rec.UserID, rec.Method, rec.Path, rec.Key)] = rec
}

func (s *state) addUser(u *user) {
	s.Users = append(s.Users, u)
	s.usersByID[u.ID] = u
	s.usersByEmail[strings.ToLower(u.Email)] = u
}

func (s *state) issueToken(u *user) string {
	token := randomHex(24)
	s.Tokens[token] = u.ID
	return token
}

func (s *state) addReservation(res *reservation) {
	s.Reservations = append(s.Reservations, res)
	s.reservationsByRef[res.Reference] = res
}

func (s *state) freshReference() string {
	for {
		candidate := newReference()
		if _, clash := s.reservationsByRef[candidate]; !clash {
			return candidate
		}
	}
}

// tableOccupied reports whether a confirmed booking holds the table over
// [start, start+duration). Bookings in `except` are the ones the caller is itself
// about to move, so their current occupancy does not stand in their own way.
func (s *state) tableOccupied(rest *restaurant, tableID string, start time.Time, except map[string]bool) bool {
	for _, res := range s.Reservations {
		if res.RestaurantID != rest.ID || res.TableID != tableID || except[res.ID] {
			continue
		}
		if !res.occupies() {
			continue
		}
		if res.overlaps(start, rest.duration()) {
			return true
		}
	}
	return false
}
