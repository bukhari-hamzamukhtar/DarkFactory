package main

import (
	"encoding/json"
	"net/http"
	"time"
)

const maxMovesPerBatch = 8

// move is one requested item of a batch: which booking, what to change about it, and
// -- once planned -- what it would become.
type move struct {
	reference string
	fields    *jsonBody
	res       *reservation
	change    *amendment
	plan      *bookingPlan
	real      bool
	before    bookingSnapshot
}

// handleReservationMoves is §11 as stage 3 extends it: several amendments that either
// all commit or none do, each carrying individual PATCH semantics.
//
// The whole batch is planned before anything is written, and the planning and the
// writing happen inside one hold of the store lock. That is what makes the batch atomic
// in the strong sense the requirements ask for -- no concurrent reader sees a
// half-applied batch, and a rejection leaves occupancy, bookings, revisions, histories,
// exception flags and the idempotency key exactly as they were.
func (s *server) handleReservationMoves(w http.ResponseWriter, r *http.Request) {
	s.serveIdempotent(w, r, pathReservationMove, func(caller *user, body *jsonBody) (int, any, *apiError) {
		moves, aerr := parseMoves(body)
		if aerr != nil {
			return 0, nil, aerr
		}

		// Ownership first: an unknown reference and someone else's are both 404, and
		// neither may be distinguishable from the other (§8, §11).
		for _, m := range moves {
			res, aerr := s.st.ownedReservation(caller, m.reference)
			if aerr != nil {
				return 0, nil, aerr
			}
			m.res = res
		}
		rest := s.st.restaurantsByID[moves[0].res.RestaurantID]
		for _, m := range moves {
			if m.res.RestaurantID != rest.ID {
				return 0, nil, validationFailed(
					"every booking in a batch must belong to the same restaurant")
			}
		}

		// Non-occupancy errors take precedence in input order. For each booking the
		// order is the one an individual PATCH uses: cancelled, then a stale
		// revision, then its own accepted cutoff, then -- only if anything is really
		// changing -- the resulting fields against the policy for the resulting date.
		now := time.Now()
		for _, m := range moves {
			if m.res.Status == statusCancelled {
				return 0, nil, conflict("reservation_cancelled",
					"this booking has been cancelled")
			}
			change, aerr := parseAmendment(m.fields)
			if aerr != nil {
				return 0, nil, aerr
			}
			m.change = change
			if change.hasExpected && change.expectedRevision != m.res.Revision {
				return 0, nil, conflict("stale_revision",
					"this booking has changed since that revision")
			}
			if cutoffPassed(m.res, now) {
				return 0, nil, conflict("cutoff_passed",
					"this booking is too close to its start time to change")
			}
			m.before = snapshotOf(m.res)
			m.real = isRealChange(m.res, change)
			if !m.real {
				// A no-op retains its terms, its end time, its revision and its
				// history, and still has to be left holding its tables.
				continue
			}
			plan, aerr := s.st.planFor(m.res, change)
			if aerr != nil {
				return 0, nil, aerr
			}
			m.plan = plan
		}

		// Occupancy is judged on the batch's *result*: against bookings outside the
		// batch, and against each other. An unchanged listed booking keeps the
		// occupancy and the duration it already has.
		listed := map[string]bool{}
		for _, m := range moves {
			listed[m.res.ID] = true
		}
		for i, m := range moves {
			tableIDs, start, duration := m.resulting()
			if s.st.anyTableOccupied(rest, tableIDs, start, duration, listed) {
				return 0, nil, conflict("table_unavailable",
					"a booking outside this batch holds one of those tables for that interval")
			}
			for _, other := range moves[i+1:] {
				otherIDs, otherStart, otherDuration := other.resulting()
				// No table may belong to two of the resulting bookings at once,
				// which for combinations means any shared member is a clash.
				if !setsIntersect(tableIDs, otherIDs) {
					continue
				}
				if start.Before(otherStart.Add(otherDuration)) &&
					otherStart.Before(start.Add(duration)) {
					return 0, nil, conflict("table_unavailable",
						"two bookings in this batch would occupy the same table at once")
				}
			}
		}

		// Everything is decided; now the batch commits.
		changedAnything := false
		affectedSeries := map[string]*series{}
		views := make([]any, 0, len(moves))
		for _, m := range moves {
			if m.real {
				m.res.apply(m.plan)
				m.res.Revision++
				record(m.res, eventChanged, changesBetween(m.before, snapshotOf(m.res)), now)
				changedAnything = true
				if agreement := s.st.seriesOf(m.res); agreement != nil {
					// A collective change is still a diner's change: the occurrence
					// becomes a permanent exception.
					if index := agreement.indexOf(m.res.Reference); index >= 0 {
						agreement.Occurrences[index].Exception = true
					}
					affectedSeries[agreement.ID] = agreement
				}
			}
			views = append(views, s.st.reservationView(m.res))
		}
		// Each affected agreement moves on once, however many of its occurrences the
		// batch touched, and so does the restaurant.
		for _, agreement := range affectedSeries {
			agreement.Revision++
		}
		if changedAnything {
			rest.Revision++
		}
		return http.StatusCreated, map[string]any{"reservations": views}, nil
	})
}

// resulting is the occupancy a move would leave behind: its planned seating when it is
// a real change, and the booking's current one when it is not.
func (m *move) resulting() ([]string, time.Time, time.Duration) {
	if m.real && m.plan != nil {
		return m.plan.tableIDs, m.plan.start, m.plan.terms.duration()
	}
	return m.res.TableIDs, m.res.start, m.res.AcceptedTerms.duration()
}

// parseMoves reads §11's body: 1..8 objects with distinct string references. Anything
// else about the batch's shape is a validation failure rather than a malformed body,
// because the requirements name that case explicitly.
func parseMoves(body *jsonBody) ([]*move, *apiError) {
	raw, present := body.members["moves"]
	if !present || jsonKind(raw) != "array" {
		return nil, validationFailed("moves must be an array of 1 to 8 objects")
	}
	var items []json.RawMessage
	if err := json.Unmarshal(raw, &items); err != nil {
		return nil, validationFailed("moves must be an array of 1 to 8 objects")
	}
	if len(items) < 1 || len(items) > maxMovesPerBatch {
		return nil, validationFailed("moves must hold between 1 and 8 items")
	}
	moves := make([]*move, 0, len(items))
	seen := map[string]bool{}
	for _, item := range items {
		if jsonKind(item) != "object" {
			return nil, validationFailed("each move must be an object")
		}
		members, err := decodeObject(item)
		if err != nil {
			return nil, validationFailed("each move must be an object")
		}
		fields := &jsonBody{members: members}
		reference, present, aerr := fields.stringMember("reference")
		if aerr != nil || !present || reference == "" {
			return nil, validationFailed("each move needs a reference string")
		}
		if seen[reference] {
			return nil, validationFailed("the references in a batch must be distinct")
		}
		seen[reference] = true
		moves = append(moves, &move{reference: reference, fields: fields})
	}
	return moves, nil
}

// setsIntersect reports whether two seatings share a table.
func setsIntersect(first, second []string) bool {
	for _, a := range first {
		for _, b := range second {
			if a == b {
				return true
			}
		}
	}
	return false
}
