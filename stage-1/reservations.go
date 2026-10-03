package main

import (
	"net/http"
	"sort"
	"time"
)

// bookingPlan is a validated target for a booking: which table, which instant, how
// many people. Building one never changes anything, which is what lets a batch plan
// every move before any of them commits (§11).
type bookingPlan struct {
	rest      *restaurant
	tbl       *table
	stamp     civilStamp
	start     time.Time
	partySize int
}

// amendment is the set of fields a caller asked to change. Absent is not the same as
// unchanged-by-coincidence: §8 and §11 both say an omitted field keeps its value, and
// a field that is not being changed is not re-validated against today's rules.
type amendment struct {
	tableID      string
	hasTable     bool
	startsLocal  string
	hasStart     bool
	partySize    int
	hasPartySize bool
}

func parseAmendment(body *jsonBody) (*amendment, *apiError) {
	change := &amendment{}
	tableID, present, aerr := body.stringMember("table_id")
	if aerr != nil {
		return nil, aerr
	}
	if present {
		if !isValidID(tableID) {
			return nil, validationFailed("table_id must be 1 to 64 characters")
		}
		change.tableID, change.hasTable = tableID, true
	}
	startsLocal, present, aerr := body.stringMember("starts_at_local")
	if aerr != nil {
		return nil, aerr
	}
	if present {
		change.startsLocal, change.hasStart = startsLocal, true
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

// planFor validates what `res` would become after `change`.
//
// The order is §8's table, read top to bottom: an unknown restaurant or table is a
// 404 before any rule about time, a local time that does not exist is rejected before
// the grid is consulted, and capacity is the last thing checked before occupancy --
// which the caller checks, because a batch has to consider all of its moves at once.
func (s *state) planFor(res *reservation, change *amendment) (*bookingPlan, *apiError) {
	rest := s.restaurantsByID[res.RestaurantID]
	if rest == nil {
		return nil, notFound()
	}
	tableID := res.TableID
	if change.hasTable {
		tableID = change.tableID
	}
	tbl := rest.byTable[tableID]
	if tbl == nil {
		return nil, notFound()
	}
	partySize := res.PartySize
	if change.hasPartySize {
		partySize = change.partySize
	}

	plan := &bookingPlan{rest: rest, tbl: tbl, partySize: partySize}
	if change.hasStart {
		stamp, ok := parseCivilStamp(change.startsLocal)
		if !ok {
			return nil, validationFailed("starts_at_local must be a bare local YYYY-MM-DDTHH:MM")
		}
		start, aerr := resolveAgainstHours(rest, stamp)
		if aerr != nil {
			return nil, aerr
		}
		plan.stamp, plan.start = stamp, start
	} else {
		// The time is not changing, so the booking keeps the very instant it already
		// has and is not re-measured against the restaurant's current grid.
		stamp, _ := parseCivilStamp(res.StartsAtLocal)
		plan.stamp, plan.start = stamp, res.start
	}
	if partySize > tbl.Capacity {
		return nil, unprocessable("party_exceeds_capacity",
			"that party does not fit the table")
	}
	return plan, nil
}

// resolveAgainstHours turns a wall-clock stamp into an instant and checks it is a
// bookable slot: §9's existence rule first, then §8's grid, then opening hours.
func resolveAgainstHours(rest *restaurant, stamp civilStamp) (time.Time, *apiError) {
	start, exists := resolveLocal(rest.loc, stamp)
	if !exists {
		return time.Time{}, unprocessable("invalid_local_time",
			"that local time does not exist on that date at this restaurant")
	}
	hours, open := rest.byDay[stamp.date.weekdayKey()]
	if !open {
		// A closed day has no grid to be on, so the only honest answer is that the
		// restaurant is not open then.
		return time.Time{}, unprocessable("outside_opening_hours",
			"the restaurant is closed on that day")
	}
	opens, _ := parseHourMinute(hours.Opens)
	closes, _ := parseHourMinute(hours.Closes)
	if (stamp.minutes-opens)%rest.SlotMinutes != 0 {
		return time.Time{}, unprocessable("not_on_slot_grid",
			"bookings start on the restaurant's slot grid")
	}
	if stamp.minutes < opens || stamp.minutes+rest.DurationMinutes > closes {
		return time.Time{}, unprocessable("outside_opening_hours",
			"the reservation would fall outside the restaurant's opening hours")
	}
	return start, nil
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
	return map[string]any{
		"reservation_id":  res.ID,
		"reference":       res.Reference,
		"restaurant_id":   res.RestaurantID,
		"table_id":        res.TableID,
		"party_size":      res.PartySize,
		"status":          res.Status,
		"starts_at_local": res.StartsAtLocal,
		"starts_at":       res.StartsAt,
		"ends_at":         endsAt,
		"created_at":      res.CreatedAt,
	}
}

// apply commits a planned change to a booking. Releasing the old occupancy and taking
// the new one is this single assignment under the store lock, so no reader can see
// the booking in neither place or in both (§8).
func (res *reservation) apply(plan *bookingPlan) {
	res.TableID = plan.tbl.ID
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
		if _, aerr := body.requiredString("table_id"); aerr != nil {
			return 0, nil, aerr
		}
		if _, aerr := body.requiredString("starts_at_local"); aerr != nil {
			return 0, nil, aerr
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

		// A new booking is an amendment to nothing: the same planner validates both,
		// so create and amend cannot drift apart (§8).
		blank := &reservation{RestaurantID: restaurantID}
		plan, aerr := s.st.planFor(blank, change)
		if aerr != nil {
			return 0, nil, aerr
		}
		if s.st.tableOccupied(plan.rest, plan.tbl.ID, plan.start, nil) {
			return 0, nil, conflict("table_unavailable", "that table is taken for that interval")
		}

		res := &reservation{
			ID:            "res_" + randomHex(8),
			Reference:     s.st.freshReference(),
			UserID:        caller.ID,
			RestaurantID:  plan.rest.ID,
			TableID:       plan.tbl.ID,
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
	if s.tableOccupied(plan.rest, plan.tbl.ID, plan.start, map[string]bool{res.ID: true}) {
		return conflict("table_unavailable", "that table is taken for that interval")
	}
	res.apply(plan)
	return nil
}
