package main

import (
	"net/http"
	"time"
)

// Amending a recurring agreement (stage 4).
//
// One request moves the clock time of every eligible occurrence from an index onwards,
// on each occurrence's own scheduled date. It is all or nothing: if any eligible
// occurrence cannot take the new time, no history, no revision and no idempotency
// record moves.

func (s *server) handleSeriesAmend(w http.ResponseWriter, r *http.Request, seriesID string) {
	path := "/series/" + seriesID + "/amend"
	s.serveIdempotent(w, r, path, func(caller *user, body *jsonBody) (int, any, *apiError) {
		agreement := s.st.seriesByID[seriesID]
		if agreement == nil || agreement.UserID != caller.ID {
			return 0, nil, notFound()
		}

		// Input is settled before anything about the agreement or its occurrences:
		// validation first, then the revision, then -- only then -- cutoffs and
		// booking rules.
		expected, present, aerr := body.integerMember("expected_revision")
		if aerr != nil || !present || expected < 1 {
			return 0, nil, validationFailed("expected_revision must be a positive integer")
		}
		fromIndex, present, aerr := body.integerMember("from_index")
		if aerr != nil || !present || fromIndex < 0 || fromIndex >= len(agreement.Occurrences) {
			return 0, nil, validationFailed(
				"from_index must be an integer within the agreement")
		}
		localTime, present, aerr := body.stringMember("local_time")
		if aerr != nil {
			return 0, nil, aerr
		}
		if !present {
			return 0, nil, validationFailed("local_time is required")
		}
		minutes, ok := parseHourMinute(localTime)
		if !ok || minutes >= 24*60 {
			return 0, nil, validationFailed("local_time must be HH:MM between 00:00 and 23:59")
		}
		if expected != agreement.Revision {
			return 0, nil, conflict("stale_revision",
				"this agreement has changed since that revision")
		}

		// Eligible occurrences: at or after the index, still live, and not already a
		// diner's exception.
		type pending struct {
			res  *reservation
			plan *bookingPlan
		}
		var changes []pending
		now := time.Now()
		for index := fromIndex; index < len(agreement.Occurrences); index++ {
			occurrence := agreement.Occurrences[index]
			if occurrence.Exception {
				continue
			}
			res := s.st.reservationsByRef[occurrence.Reference]
			if res == nil || res.Status == statusCancelled {
				continue
			}
			stamp, ok := parseCivilStamp(res.StartsAtLocal)
			if !ok {
				return 0, nil, validationFailed("an occurrence has no usable local start")
			}
			// The clock time changes; the scheduled date does not.
			wanted := stamp.date.at(minutes)
			if wanted.String() == res.StartsAtLocal {
				// A change with identical resulting fields is a no-op: it keeps its
				// terms, its revision and its history.
				continue
			}
			if cutoffPassed(res, now) {
				return 0, nil, conflict("cutoff_passed",
					"an occurrence is too close to its start time to change")
			}
			plan, aerr := s.st.planFor(res, &amendment{stamp: wanted, hasStart: true})
			if aerr != nil {
				// Non-occupancy errors take precedence in occurrence-index order.
				return 0, nil, aerr
			}
			changes = append(changes, pending{res: res, plan: plan})
		}

		// Occupancy is judged on the result: the occurrences that are moving do not
		// block themselves, but everything else does -- unchanged occurrences, other
		// diners' bookings and applied closures.
		moving := map[string]bool{}
		for _, change := range changes {
			moving[change.res.ID] = true
		}
		for i, change := range changes {
			if s.st.anyTableOccupied(change.plan.rest, change.plan.tableIDs,
				change.plan.start, change.plan.terms.duration(), moving) {
				return 0, nil, conflict("table_unavailable",
					"an occurrence would land on a table that is taken")
			}
			for _, other := range changes[i+1:] {
				if !setsIntersect(change.plan.tableIDs, other.plan.tableIDs) {
					continue
				}
				if change.plan.start.Before(other.plan.start.Add(other.plan.terms.duration())) &&
					other.plan.start.Before(change.plan.start.Add(change.plan.terms.duration())) {
					return 0, nil, conflict("table_unavailable",
						"two occurrences of this agreement would overlap")
				}
			}
		}

		// Everything is decided; the batch commits together.
		for _, change := range changes {
			before := snapshotOf(change.res)
			change.res.apply(change.plan)
			change.res.Revision++
			record(change.res, eventChanged,
				changesBetween(before, snapshotOf(change.res)), now)
		}
		if len(changes) > 0 {
			// One revision each for the agreement and the restaurant, for the whole
			// operation. A series amendment is not a diner's individual change, so
			// it marks no exceptions.
			agreement.Revision++
			if rest := s.st.restaurantsByID[agreement.RestaurantID]; rest != nil {
				rest.Revision++
			}
		}
		return http.StatusCreated, s.st.seriesView(agreement), nil
	})
}
