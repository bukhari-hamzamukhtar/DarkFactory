package main

import (
	"encoding/json"
	"sort"
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
//
// Validation here is deliberately narrow. It rejects what is impossible -- a booking
// on a table that does not exist, two confirmed bookings on one table at one time, a
// zone that cannot be loaded -- and accepts everything a fixture is merely free to
// say. Refusing a reset costs every later request in the run, so leniency is the
// default wherever the requirements do not state a rule.
func (s *state) prepare() *apiError {
	// Empty collections are kept non-nil, so an export always presents every member
	// as a list or an object. Import then has something definite to insist on.
	if s.Tokens == nil {
		s.Tokens = map[string]string{}
	}
	if s.Users == nil {
		s.Users = []*user{}
	}
	if s.Restaurants == nil {
		s.Restaurants = []*restaurant{}
	}
	if s.Reservations == nil {
		s.Reservations = []*reservation{}
	}
	if s.Receipts == nil {
		s.Receipts = []*receipt{}
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
	// A slot grid of zero minutes names no slots at all and would not terminate;
	// a reservation of no minutes occupies nothing. Both are impossible rather
	// than merely unusual.
	if r.SlotMinutes < 1 || r.SlotMinutes > 24*60 {
		return validationFailed("slot_minutes must be between 1 and 1440")
	}
	if r.DurationMinutes < 1 || r.DurationMinutes > maxCountedValue {
		return validationFailed("reservation_duration_minutes must be at least 1")
	}
	if r.CutoffMinutes < 0 || r.CutoffMinutes > maxCountedValue {
		return validationFailed("cancellation_cutoff_minutes must not be negative")
	}

	r.byDay = map[string][]openingHours{}
	for _, hours := range r.OpeningHours {
		if !isWeekdayKey(hours.Weekday) {
			return validationFailed("weekday must be one of mon tue wed thu fri sat sun")
		}
		opens, okOpens := parseHourMinute(hours.Opens)
		closes, okCloses := parseHourMinute(hours.Closes)
		if !okOpens || !okCloses {
			return validationFailed("opens and closes must be local HH:MM")
		}
		if closes <= opens {
			return validationFailed("closes must be later than opens on the same day")
		}
		r.byDay[hours.Weekday] = append(r.byDay[hours.Weekday], hours)
	}
	for weekday := range r.byDay {
		sittings := r.byDay[weekday]
		sort.SliceStable(sittings, func(i, j int) bool {
			first, _ := parseHourMinute(sittings[i].Opens)
			second, _ := parseHourMinute(sittings[j].Opens)
			return first < second
		})
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
		// §5 makes a negative count a validation failure. A capacity of zero is
		// odd but says something coherent -- a table nobody can be seated at --
		// so it is accepted and simply never offered.
		if t.Capacity < 0 || t.Capacity > maxCountedValue {
			return validationFailed("table capacity must not be negative")
		}
		r.byTable[t.ID] = t
	}

	r.pairs = nil
	for _, pair := range r.Combinable {
		// Pairs only, never three or more.
		if len(pair) != 2 {
			return validationFailed("each combinable entry must be a pair of table ids")
		}
		if pair[0] == pair[1] {
			return validationFailed("a combinable pair must name two different tables")
		}
		first, firstKnown := r.byTable[pair[0]]
		second, secondKnown := r.byTable[pair[1]]
		if !firstKnown || !secondKnown {
			return validationFailed("a combinable pair names a table this restaurant does not have")
		}
		r.pairs = append(r.pairs, tablePair{
			ids:      []string{pair[0], pair[1]},
			capacity: first.Capacity + second.Capacity,
		})
	}
	s.restaurantsByID[r.ID] = r
	return nil
}

func (s *state) prepareReservation(res *reservation) *apiError {
	if res == nil {
		return validationFailed("a reservation entry is null")
	}
	// A stage-1 state, and a stage-1 style fixture, name one table in `table_id`.
	// Fold it in here so the rest of the service only knows about sets (§10).
	if len(res.TableIDs) == 0 && res.LegacyTableID != "" {
		res.TableIDs = []string{res.LegacyTableID}
	}
	res.LegacyTableID = ""
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
	if len(res.TableIDs) == 0 {
		return validationFailed("reservation " + res.Reference + " names no table")
	}
	seen := map[string]bool{}
	for _, id := range res.TableIDs {
		if _, known := rest.byTable[id]; !known {
			return validationFailed("reservation " + res.Reference + " names an unknown table")
		}
		if seen[id] {
			return validationFailed("reservation " + res.Reference + " names one table twice")
		}
		seen[id] = true
	}
	if res.PartySize < 1 || res.PartySize > maxCountedValue {
		return validationFailed("reservation party_size must be at least 1")
	}
	if res.Status == "" {
		// §4 and stage 2: a seeded reservation is confirmed unless it says otherwise.
		res.Status = statusConfirmed
	}
	if res.Status != statusConfirmed && res.Status != statusCancelled {
		return validationFailed("reservation status must be confirmed or cancelled")
	}
	if _, ok := parseCivilStamp(res.StartsAtLocal); !ok {
		return validationFailed("reservation starts_at_local must be YYYY-MM-DDTHH:MM")
	}
	rendered, start, ok := normalizeTimestamp(res.StartsAt)
	if !ok {
		return validationFailed("reservation starts_at must be a valid RFC 3339 timestamp")
	}
	res.StartsAt, res.start = rendered, start
	if res.CreatedAt == "" {
		res.CreatedAt = formatUTC(time.Now())
	}
	createdAt, _, ok := normalizeTimestamp(res.CreatedAt)
	if !ok {
		return validationFailed("reservation created_at must be a valid RFC 3339 timestamp")
	}
	res.CreatedAt = createdAt
	s.reservationsByRef[res.Reference] = res
	return nil
}

// checkNoOverlap enforces §1's invariant on any state that is about to go live. A
// fixture or an import that double-books a table is rejected rather than installed,
// because no sequence of API calls could have produced it. A combination counts on
// every table it holds.
func (s *state) checkNoOverlap() *apiError {
	type place struct{ restaurantID, tableID string }
	held := map[place][]*reservation{}
	for _, res := range s.Reservations {
		if !res.occupies() {
			continue
		}
		rest := s.restaurantsByID[res.RestaurantID]
		for _, tableID := range res.TableIDs {
			where := place{res.RestaurantID, tableID}
			for _, other := range held[where] {
				if other.overlaps(res.start, rest.duration()) {
					return validationFailed("reservations " + other.Reference + " and " +
						res.Reference + " occupy table " + tableID + " at overlapping times")
				}
			}
			held[where] = append(held[where], res)
		}
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
		if res.RestaurantID != rest.ID || except[res.ID] || !res.holds(tableID) {
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

// anyTableOccupied is the same question for a whole seating: a combination can be
// booked only when every table in it is free.
func (s *state) anyTableOccupied(rest *restaurant, tableIDs []string, start time.Time, except map[string]bool) bool {
	for _, tableID := range tableIDs {
		if s.tableOccupied(rest, tableID, start, except) {
			return true
		}
	}
	return false
}
