package main

import (
	"encoding/json"
	"net/http"
	"sort"
	"time"
)

// bookingPlan is a validated target for a booking: which seating, which instant, how
// many people. Building one never changes anything, which is what lets a batch plan
// every move before any of them commits (§11).
type bookingPlan struct {
	rest      *restaurant
	tableIDs  []string
	stamp     civilStamp
	start     time.Time
	partySize int
}

// amendment is the set of fields a caller asked to change. Absent is not the same as
// unchanged-by-coincidence: §8 and §11 both say an omitted field keeps its value, and
// a field that is not being changed is not re-validated against today's rules.
type amendment struct {
	tableIDs     []string
	hasTables    bool
	stamp        civilStamp
	hasStart     bool
	partySize    int
	hasPartySize bool
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
// The order is §8's table, read top to bottom, with stage 2's combination rules
// sitting where they belong: an unknown restaurant or table is a 404 before any rule
// about seating or time, a set that the restaurant has not declared combinable is
// refused before the clock is consulted, a local time that does not exist is rejected
// before the grid, and capacity is the last thing checked before occupancy -- which
// the caller checks, because a batch has to consider all of its moves at once.
func (s *state) planFor(res *reservation, change *amendment) (*bookingPlan, *apiError) {
	rest := s.restaurantsByID[res.RestaurantID]
	if rest == nil {
		return nil, notFound()
	}
	tableIDs := res.TableIDs
	if change.hasTables {
		tableIDs = change.tableIDs
	}
	capacity := 0
	for _, id := range tableIDs {
		t := rest.byTable[id]
		if t == nil {
			return nil, notFound()
		}
		capacity += t.Capacity
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
		// Echo the pair the way the restaurant declared it (§ combined tables).
		tableIDs = declared
	}

	partySize := res.PartySize
	if change.hasPartySize {
		partySize = change.partySize
	}

	plan := &bookingPlan{rest: rest, tableIDs: tableIDs, partySize: partySize}
	if change.hasStart {
		start, aerr := resolveAgainstHours(rest, change.stamp)
		if aerr != nil {
			return nil, aerr
		}
		plan.stamp, plan.start = change.stamp, start
	} else {
		// The time is not changing, so the booking keeps the very instant it already
		// has and is not re-measured against the restaurant's current grid.
		stamp, _ := parseCivilStamp(res.StartsAtLocal)
		plan.stamp, plan.start = stamp, res.start
	}
	if partySize > capacity {
		return nil, unprocessable("party_exceeds_capacity",
			"that party does not fit the seating")
	}
	return plan, nil
}

// resolveAgainstHours turns a wall-clock stamp into an instant and checks it is a
// bookable slot: §9's existence rule first, then §8's grid, then opening hours.
//
// A restaurant may keep more than one sitting on a weekday, and each sitting is its
// own grid anchored at its own opening time.
//
// The end of the reservation is compared as an absolute instant against the instant
// the restaurant's clock reaches `closes`, because reservation_duration_minutes is
// absolute time (§9). Ninety minutes from 01:30 on a spring-forward night reads
// 04:00 on the wall, so a restaurant closing at 03:30 is shut before the table is
// free; ninety minutes from 01:30 on a fall-back night reads 02:00, so a restaurant
// closing at 02:30 is still open. Comparing wall-clock minutes gets both backwards.
func resolveAgainstHours(rest *restaurant, stamp civilStamp) (time.Time, *apiError) {
	start, exists := resolveLocal(rest.loc, stamp)
	if !exists {
		return time.Time{}, unprocessable("invalid_local_time",
			"that local time does not exist on that date at this restaurant")
	}
	sittings := rest.byDay[stamp.date.weekdayKey()]
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
		if (stamp.minutes-opens)%rest.SlotMinutes != 0 {
			continue
		}
		onGrid = true
		if stamp.minutes < opens {
			continue
		}
		if !start.Add(rest.duration()).After(instantReaching(rest.loc, stamp.date.at(closes))) {
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

// cutoffPassed is §8's rule for cancelling or changing: refused within
// cancellation_cutoff_minutes of the current start, or any time after it.
func cutoffPassed(rest *restaurant, res *reservation, now time.Time) bool {
	deadline := res.start.Add(-time.Duration(rest.CutoffMinutes) * time.Minute)
	return !now.Before(deadline)
}

func (s *state) reservationView(res *reservation) map[string]any {
	rest := s.restaurantsByID[res.RestaurantID]
	endsAt := ""
	if rest != nil {
		// Duration is absolute time, so the end is the start plus the duration and
		// then rendered locally -- not 90 minutes added to a wall clock (§9).
		endsAt = formatInstant(res.start.Add(rest.duration()), rest.loc)
	}
	view := map[string]any{
		"reservation_id":  res.ID,
		"reference":       res.Reference,
		"restaurant_id":   res.RestaurantID,
		"table_ids":       res.TableIDs,
		"party_size":      res.PartySize,
		"status":          res.Status,
		"starts_at_local": res.StartsAtLocal,
		"starts_at":       res.StartsAt,
		"ends_at":         endsAt,
		"created_at":      res.CreatedAt,
	}
	// `table_id` is carried only when the seating is a single table, and omitted
	// otherwise, so a client cannot read a combination as one table.
	if len(res.TableIDs) == 1 {
		view["table_id"] = res.TableIDs[0]
	}
	return view
}

// apply commits a planned change to a booking. Releasing the old occupancy and taking
// the new one is this single assignment under the store lock, so no reader can see
// the booking in neither place or in both (§8).
func (res *reservation) apply(plan *bookingPlan) {
	res.TableIDs = plan.tableIDs
	res.PartySize = plan.partySize
	res.StartsAtLocal = plan.stamp.String()
	res.StartsAt = formatInstant(plan.start, plan.rest.loc)
	res.start = plan.start
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
		if s.st.anyTableOccupied(plan.rest, plan.tableIDs, plan.start, nil) {
			return 0, nil, conflict("table_unavailable",
				"that seating is taken for that interval")
		}

		res := &reservation{
			ID:            "res_" + randomHex(8),
			Reference:     s.st.freshReference(),
			UserID:        caller.ID,
			RestaurantID:  plan.rest.ID,
			TableIDs:      plan.tableIDs,
			StartsAtLocal: plan.stamp.String(),
			StartsAt:      formatInstant(plan.start, plan.rest.loc),
			CreatedAt:     formatUTC(time.Now()),
			PartySize:     plan.partySize,
			Status:        statusConfirmed,
			start:         plan.start,
		}
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
	// Cancelling twice is not an error, and that holds whatever the clock says: the
	// booking is already in the state the caller is asking for (§8).
	if res.Status == statusCancelled {
		writeJSON(w, http.StatusOK, s.st.reservationView(res))
		return
	}
	rest := s.st.restaurantsByID[res.RestaurantID]
	if cutoffPassed(rest, res, time.Now()) {
		writeError(w, conflict("cutoff_passed",
			"this booking is too close to its start time to cancel"))
		return
	}
	// Cancelling frees every table in the seating, which falls out of the status
	// change: occupancy is only ever read from confirmed bookings.
	res.Status = statusCancelled
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
	if aerr := s.st.amend(res, body, time.Now()); aerr != nil {
		writeError(w, aerr)
		return
	}
	writeJSON(w, http.StatusOK, s.st.reservationView(res))
}

// amend applies one amendment to one booking, in the precedence §8 and §11 describe:
// a cancelled booking is refused before anything else is considered, then the
// booking's own cutoff, then the fields, then occupancy.
func (s *state) amend(res *reservation, body *jsonBody, now time.Time) *apiError {
	if res.Status == statusCancelled {
		return conflict("reservation_cancelled", "this booking has been cancelled")
	}
	rest := s.restaurantsByID[res.RestaurantID]
	if cutoffPassed(rest, res, now) {
		return conflict("cutoff_passed",
			"this booking is too close to its start time to change")
	}
	change, aerr := parseAmendment(body)
	if aerr != nil {
		return aerr
	}
	plan, aerr := s.planFor(res, change)
	if aerr != nil {
		return aerr
	}
	if s.anyTableOccupied(plan.rest, plan.tableIDs, plan.start, map[string]bool{res.ID: true}) {
		return conflict("table_unavailable", "that seating is taken for that interval")
	}
	res.apply(plan)
	return nil
}
