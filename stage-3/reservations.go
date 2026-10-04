package main

import (
	"encoding/json"
	"net/http"
	"sort"
	"time"
)

// bookingPlan is a validated target for a booking: which seating, which instant, how
// many people, and under which terms. Building one never changes anything, which is
// what lets a batch plan every move before any of them commits (§11).
type bookingPlan struct {
	rest      *restaurant
	tableIDs  []string
	stamp     civilStamp
	start     time.Time
	partySize int
	// terms are the policy selected for the resulting local start date (stage 3).
	terms terms
}

// amendment is the set of fields a caller asked to change. Absent is not the same as
// unchanged-by-coincidence: §8 and §11 both say an omitted field keeps its value.
type amendment struct {
	tableIDs     []string
	hasTables    bool
	stamp        civilStamp
	hasStart     bool
	partySize    int
	hasPartySize bool
	// expectedRevision is stage 3's optimistic check. Zero means it was not sent.
	expectedRevision int
	hasExpected      bool
}

// parseAmendment reads the fields a booking request may carry, and settles every
// question that can be answered from the body alone: JSON types, formats, ranges and
// the shape of a table set. Resource lookups come later, so a field of the wrong
// format is a validation failure rather than a 404 about something else (§5).
func parseAmendment(body *jsonBody) (*amendment, *apiError) {
	change := &amendment{}

	tableIDs, present, aerr := parseTableSet(body)
	if aerr != nil {
		return nil, aerr
	}
	if present {
		change.tableIDs, change.hasTables = tableIDs, true
	}

	startsLocal, present, aerr := body.stringMember("starts_at_local")
	if aerr != nil {
		return nil, aerr
	}
	if present {
		stamp, ok := parseCivilStamp(startsLocal)
		if !ok {
			return nil, validationFailed(
				"starts_at_local must be a bare local YYYY-MM-DDTHH:MM")
		}
		change.stamp, change.hasStart = stamp, true
	}

	partySize, present, aerr := body.integerMember("party_size")
	if aerr != nil {
		return nil, aerr
	}
	if present {
		if partySize < 1 {
			return nil, validationFailed("party_size must be a whole number of at least 1")
		}
		change.partySize, change.hasPartySize = partySize, true
	}

	// expected_revision is optional; an invalid type or range is 422, and a mismatch
	// is decided later, before the cutoff and the fields (stage 3).
	if raw, carried := body.members["expected_revision"]; carried && jsonKind(raw) != "null" {
		expected, _, aerr := body.integerMember("expected_revision")
		if aerr != nil {
			return nil, validationFailed("expected_revision must be a positive integer")
		}
		if expected < 1 {
			return nil, validationFailed("expected_revision must be a positive integer")
		}
		change.expectedRevision, change.hasExpected = expected, true
	}
	return change, nil
}

// parseTableSet reads the seating a request names.
//
// Stage 2 takes `table_ids`; `table_id` is still accepted and means a set of one.
// Sending both is a validation failure, because the two could disagree and nothing
// says which would win.
func parseTableSet(body *jsonBody) ([]string, bool, *apiError) {
	single, hasSingle, aerr := body.stringMember("table_id")
	if aerr != nil {
		return nil, false, aerr
	}
	raw, hasList := body.members["table_ids"]
	if hasList && jsonKind(raw) == "null" {
		hasList = false
	}
	if hasSingle && hasList {
		return nil, false, validationFailed(
			"send either table_id or table_ids, not both")
	}
	if hasSingle {
		if !isValidID(single) {
			return nil, false, validationFailed("table_id must be 1 to 64 characters")
		}
		return []string{single}, true, nil
	}
	if !hasList {
		return nil, false, nil
	}
	if jsonKind(raw) != "array" {
		return nil, false, malformed("field table_ids must be an array of strings")
	}
	var elements []json.RawMessage
	if err := json.Unmarshal(raw, &elements); err != nil {
		return nil, false, malformed("field table_ids must be an array of strings")
	}
	ids := make([]string, 0, len(elements))
	seen := map[string]bool{}
	for _, element := range elements {
		if jsonKind(element) != "string" {
			return nil, false, malformed("field table_ids must be an array of strings")
		}
		var id string
		if err := json.Unmarshal(element, &id); err != nil {
			return nil, false, malformed("field table_ids must be an array of strings")
		}
		if !isValidID(id) {
			return nil, false, validationFailed("each table id must be 1 to 64 characters")
		}
		if seen[id] {
			return nil, false, validationFailed("a table may appear in a set only once")
		}
		seen[id] = true
		ids = append(ids, id)
	}
	if len(ids) == 0 {
		return nil, false, validationFailed("table_ids must name at least one table")
	}
	return ids, true, nil
}

// planFor validates what `res` would become after `change`.
//
// The order is §8's table read top to bottom, with stage 2's combination rules and
// stage 3's policies in their places: an unknown restaurant or table is a 404 before
// any rule about seating or time; an undeclared combination is refused before the clock
// is consulted; the policy for the *resulting* local start date is then selected, and
// the grid, opening hours, duration and capacity all come from it; occupancy is left to
// the caller, because a batch has to consider all of its moves at once.
func (s *state) planFor(res *reservation, change *amendment) (*bookingPlan, *apiError) {
	rest := s.restaurantsByID[res.RestaurantID]
	if rest == nil {
		return nil, notFound()
	}
	tableIDs := res.TableIDs
	if change.hasTables {
		tableIDs = change.tableIDs
	}
	for _, id := range tableIDs {
		if rest.byTable[id] == nil {
			return nil, notFound()
		}
	}
	if len(tableIDs) > 2 {
		return nil, unprocessable("combination_not_allowed",
			"only two tables may be combined")
	}
	if len(tableIDs) == 2 {
		declared, allowed := rest.pairAllowed(tableIDs)
		if !allowed {
			return nil, unprocessable("combination_not_allowed",
				"this restaurant has not declared those tables combinable")
		}
		// Echo the pair the way the restaurant declared it.
		tableIDs = declared
	}

	partySize := res.PartySize
	if change.hasPartySize {
		partySize = change.partySize
	}
	stamp := change.stamp
	if !change.hasStart {
		stamp, _ = parseCivilStamp(res.StartsAtLocal)
	}

	// Stage 3: the resulting start date chooses the policy, and everything below is
	// measured against it.
	selected := s.termsFor(rest, stamp.date)
	start, aerr := resolveAgainstHours(rest, selected, stamp)
	if aerr != nil {
		return nil, aerr
	}
	if partySize > selected.capacityOf(tableIDs) {
		return nil, unprocessable("party_exceeds_capacity",
			"that party does not fit the seating")
	}
	return &bookingPlan{
		rest:      rest,
		tableIDs:  tableIDs,
		stamp:     stamp,
		start:     start,
		partySize: partySize,
		terms:     selected,
	}, nil
}

// resolveAgainstHours turns a wall-clock stamp into an instant and checks it is a
// bookable slot under the given terms: §9's existence rule first, then §8's grid, then
// opening hours.
//
// A restaurant may keep more than one sitting on a weekday, and each sitting is its own
// grid anchored at its own opening time.
//
// The end of the reservation is compared as an absolute instant against the instant the
// restaurant's clock reaches `closes`, because the duration is absolute time (§9).
// Ninety minutes from 01:30 on a spring-forward night reads 04:00 on the wall, so a
// restaurant closing at 03:30 is shut before the table is free; ninety minutes from
// 01:30 on a fall-back night reads 02:00, so a restaurant closing at 02:30 is still
// open. Comparing wall-clock minutes gets both backwards.
func resolveAgainstHours(rest *restaurant, t terms, stamp civilStamp) (time.Time, *apiError) {
	start, exists := resolveLocal(rest.loc, stamp)
	if !exists {
		return time.Time{}, unprocessable("invalid_local_time",
			"that local time does not exist on that date at this restaurant")
	}
	sittings := t.sittings(stamp.date.weekdayKey())
	if len(sittings) == 0 {
		// A closed day has no grid to be on, so the only honest answer is that the
		// restaurant is not open then.
		return time.Time{}, unprocessable("outside_opening_hours",
			"the restaurant is closed on that day")
	}
	onGrid := false
	for _, hours := range sittings {
		opens, _ := parseHourMinute(hours.Opens)
		closes, _ := parseHourMinute(hours.Closes)
		if (stamp.minutes-opens)%t.SlotMinutes != 0 {
			continue
		}
		onGrid = true
		if stamp.minutes < opens {
			continue
		}
		if !start.Add(t.duration()).After(instantReaching(rest.loc, stamp.date.at(closes))) {
			return start, nil
		}
	}
	if !onGrid {
		return time.Time{}, unprocessable("not_on_slot_grid",
			"bookings start on the restaurant's slot grid")
	}
	return time.Time{}, unprocessable("outside_opening_hours",
		"the reservation would fall outside the restaurant's opening hours")
}

// cutoffPassed is §8's rule, measured against the cutoff the booking accepted and its
// current start: refused within that many minutes of the start, or any time after it.
func cutoffPassed(res *reservation, now time.Time) bool {
	deadline := res.start.Add(-time.Duration(res.AcceptedTerms.CutoffMinutes) * time.Minute)
	return !now.Before(deadline)
}

func (s *state) reservationView(res *reservation) map[string]any {
	view := map[string]any{
		"reservation_id":  res.ID,
		"reference":       res.Reference,
		"restaurant_id":   res.RestaurantID,
		"table_ids":       res.TableIDs,
		"party_size":      res.PartySize,
		"status":          res.Status,
		"starts_at_local": res.StartsAtLocal,
		"starts_at":       res.StartsAt,
		// Duration is absolute time, taken from the terms this booking accepted, so
		// a policy published later does not move the end (stage 3).
		"ends_at":        formatInstant(res.endsAt(), s.locationOf(res.RestaurantID)),
		"created_at":     res.CreatedAt,
		"revision":       res.Revision,
		"accepted_terms": res.AcceptedTerms,
	}
	// `table_id` is carried only when the seating is a single table, and omitted
	// otherwise, so a client cannot read a combination as one table.
	if len(res.TableIDs) == 1 {
		view["table_id"] = res.TableIDs[0]
	}
	return view
}

func (s *state) locationOf(restaurantID string) *time.Location {
	if rest := s.restaurantsByID[restaurantID]; rest != nil {
		return rest.loc
	}
	return time.UTC
}

// apply commits a planned change to a booking. Releasing the old occupancy, taking the
// new one, adopting the new terms and moving the revision are this one assignment under
// the store lock, so no reader can see a half-applied amendment.
func (res *reservation) apply(plan *bookingPlan) {
	res.TableIDs = plan.tableIDs
	res.PartySize = plan.partySize
	res.StartsAtLocal = plan.stamp.String()
	res.StartsAt = formatInstant(plan.start, plan.rest.loc)
	res.start = plan.start
	res.AcceptedTerms = plan.terms.copy()
}

// ---- POST /reservations --------------------------------------------------

func (s *server) handleCreateReservation(w http.ResponseWriter, r *http.Request) {
	s.serveIdempotent(w, r, pathReservations, func(caller *user, body *jsonBody) (int, any, *apiError) {
		restaurantID, aerr := body.requiredString("restaurant_id")
		if aerr != nil {
			return 0, nil, aerr
		}
		if !isValidID(restaurantID) {
			return 0, nil, validationFailed("restaurant_id must be 1 to 64 characters")
		}
		if _, present := body.members["starts_at_local"]; !present {
			return 0, nil, validationFailed("field starts_at_local is required")
		}
		if _, present, aerr := body.integerMember("party_size"); aerr != nil {
			return 0, nil, aerr
		} else if !present {
			return 0, nil, validationFailed("field party_size is required")
		}
		change, aerr := parseAmendment(body)
		if aerr != nil {
			return 0, nil, aerr
		}
		if !change.hasTables {
			return 0, nil, validationFailed("field table_ids is required")
		}
		if !change.hasStart {
			return 0, nil, validationFailed("field starts_at_local is required")
		}

		// A new booking is an amendment to nothing: the same planner validates both,
		// so create and amend cannot drift apart (§8).
		blank := &reservation{RestaurantID: restaurantID}
		plan, aerr := s.st.planFor(blank, change)
		if aerr != nil {
			return 0, nil, aerr
		}
		if s.st.anyTableOccupied(plan.rest, plan.tableIDs, plan.start,
			plan.terms.duration(), nil) {
			return 0, nil, conflict("table_unavailable",
				"that seating is taken for that interval")
		}

		now := time.Now()
		res := &reservation{
			ID:            "res_" + randomHex(8),
			Reference:     s.st.freshReference(),
			UserID:        caller.ID,
			RestaurantID:  plan.rest.ID,
			TableIDs:      plan.tableIDs,
			StartsAtLocal: plan.stamp.String(),
			StartsAt:      formatInstant(plan.start, plan.rest.loc),
			CreatedAt:     formatUTC(now),
			PartySize:     plan.partySize,
			Status:        statusConfirmed,
			Revision:      1,
			AcceptedTerms: plan.terms.copy(),
			start:         plan.start,
		}
		res.History = []historyEntry{createdEntry(res, res.CreatedAt)}
		s.st.addReservation(res)
		return http.StatusCreated, s.st.reservationView(res), nil
	})
}

// ---- reads ---------------------------------------------------------------

func (s *server) handleListReservations(w http.ResponseWriter, r *http.Request) {
	token, aerr := bearerToken(r)
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
	mine := make([]*reservation, 0)
	for _, res := range s.st.Reservations {
		if res.UserID == caller.ID {
			mine = append(mine, res)
		}
	}
	// starts_at descending (§8); reference breaks ties so the order is total.
	sort.SliceStable(mine, func(i, j int) bool {
		if !mine[i].start.Equal(mine[j].start) {
			return mine[i].start.After(mine[j].start)
		}
		return mine[i].Reference < mine[j].Reference
	})
	views := make([]any, 0, len(mine))
	for _, res := range mine {
		views = append(views, s.st.reservationView(res))
	}
	writeJSON(w, http.StatusOK, map[string]any{"reservations": views})
}

func (s *server) handleGetReservation(w http.ResponseWriter, r *http.Request, reference string) {
	token, aerr := bearerToken(r)
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
	res, aerr := s.st.ownedReservation(caller, reference)
	if aerr != nil {
		writeError(w, aerr)
		return
	}
	writeJSON(w, http.StatusOK, s.st.reservationView(res))
}

// ---- cancel --------------------------------------------------------------

func (s *server) handleCancelReservation(w http.ResponseWriter, r *http.Request, reference string) {
	token, aerr := bearerToken(r)
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
	res, aerr := s.st.ownedReservation(caller, reference)
	if aerr != nil {
		writeError(w, aerr)
		return
	}
	// Cancelling twice is not an error, and changes nothing a second time: no
	// revision, no history entry, no series revision (§8, stage 3).
	if res.Status == statusCancelled {
		writeJSON(w, http.StatusOK, s.st.reservationView(res))
		return
	}
	now := time.Now()
	// The cutoff is the one this booking accepted, measured against its current
	// start (stage 3).
	if cutoffPassed(res, now) {
		writeError(w, conflict("cutoff_passed",
			"this booking is too close to its start time to cancel"))
		return
	}
	// Cancelling frees every table in the seating, which falls out of the status
	// change: occupancy is only ever read from confirmed bookings.
	res.Status = statusCancelled
	res.Revision++
	record(res, eventCancelled, []historyChange{}, now)
	// A cancelled occurrence stays in its series and is not an exception, but the
	// agreement has moved on (stage 3).
	if agreement := s.st.seriesOf(res); agreement != nil {
		agreement.Revision++
	}
	writeJSON(w, http.StatusOK, s.st.reservationView(res))
}

// ---- amend ---------------------------------------------------------------

func (s *server) handlePatchReservation(w http.ResponseWriter, r *http.Request, reference string) {
	body, aerr := s.readObjectBody(r)
	if aerr != nil {
		writeError(w, aerr)
		return
	}
	token, aerr := bearerToken(r)
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
	res, aerr := s.st.ownedReservation(caller, reference)
	if aerr != nil {
		writeError(w, aerr)
		return
	}
	changed, aerr := s.st.amend(res, body, time.Now())
	if aerr != nil {
		writeError(w, aerr)
		return
	}
	if changed {
		// A real individual amendment permanently marks that occurrence as an
		// exception and moves the agreement on once (stage 3).
		s.st.markException(res)
	}
	writeJSON(w, http.StatusOK, s.st.reservationView(res))
}

// amend applies one amendment to one booking, in the precedence stages 1 and 3 fix: a
// cancelled booking is refused first, then a stale revision, then the booking's own
// accepted cutoff, then -- only if anything is actually changing -- the resulting
// fields against the policy for the resulting date, then occupancy.
//
// It reports whether a real change happened. A no-op keeps its terms, its end time, its
// revision and its history, and still answers 200.
func (s *state) amend(res *reservation, body *jsonBody, now time.Time) (bool, *apiError) {
	if res.Status == statusCancelled {
		return false, conflict("reservation_cancelled", "this booking has been cancelled")
	}
	change, aerr := parseAmendment(body)
	if aerr != nil {
		return false, aerr
	}
	// expected_revision is decided before the cutoff and before the fields.
	if change.hasExpected && change.expectedRevision != res.Revision {
		return false, conflict("stale_revision",
			"this booking has changed since that revision")
	}
	if cutoffPassed(res, now) {
		return false, conflict("cutoff_passed",
			"this booking is too close to its start time to change")
	}

	before := snapshotOf(res)
	// A no-op is settled before any validation: nothing is resulting, so there is
	// nothing to measure against a policy.
	if !isRealChange(res, change) {
		return false, nil
	}
	plan, aerr := s.planFor(res, change)
	if aerr != nil {
		return false, aerr
	}
	if s.anyTableOccupied(plan.rest, plan.tableIDs, plan.start, plan.terms.duration(),
		map[string]bool{res.ID: true}) {
		return false, conflict("table_unavailable", "that seating is taken for that interval")
	}
	res.apply(plan)
	res.Revision++
	record(res, eventChanged, changesBetween(before, snapshotOf(res)), now)
	return true, nil
}

// isRealChange reports whether an amendment asks for anything the booking does not
// already have. A field set to the value it already holds is not a change, and a
// reversed pair names the same set (stage 3).
func isRealChange(res *reservation, change *amendment) bool {
	if change.hasTables && !sameTableSet(res.TableIDs, change.tableIDs) {
		return true
	}
	if change.hasStart && change.stamp.String() != res.StartsAtLocal {
		return true
	}
	if change.hasPartySize && change.partySize != res.PartySize {
		return true
	}
	return false
}

// markException records that a diner changed one occurrence of an agreement by hand.
// The flag is permanent and the agreement's revision moves once.
func (s *state) markException(res *reservation) {
	agreement := s.seriesOf(res)
	if agreement == nil {
		return
	}
	if index := agreement.indexOf(res.Reference); index >= 0 {
		agreement.Occurrences[index].Exception = true
	}
	agreement.Revision++
}
