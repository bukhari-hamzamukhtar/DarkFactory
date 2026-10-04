package main

import (
	"net/http"
	"time"
)

// Recurring agreements (stage 3).
//
// A series adopts an existing booking as occurrence zero and generates the rest from
// it. Adoption is all or nothing: no partial series, no stray reservations, no
// histories, no counters and no idempotency claim survives a failure, and the first
// occurrence that cannot be made -- in index order -- decides the error.

const (
	minSeriesCount   = 2
	maxSeriesCount   = 12
	minIntervalWeeks = 1
	maxIntervalWeeks = 4
	pathSeries       = "/series"
)

func (s *server) handleCreateSeries(w http.ResponseWriter, r *http.Request) {
	s.serveIdempotent(w, r, pathSeries, func(caller *user, body *jsonBody) (int, any, *apiError) {
		anchorReference, aerr := body.requiredString("anchor_reference")
		if aerr != nil {
			return 0, nil, aerr
		}
		count, present, aerr := body.integerMember("count")
		if aerr != nil {
			return 0, nil, validationFailed("count must be an integer from 2 to 12")
		}
		if !present {
			return 0, nil, validationFailed("count is required")
		}
		if count < minSeriesCount || count > maxSeriesCount {
			return 0, nil, validationFailed("count must be an integer from 2 to 12")
		}
		intervalWeeks, present, aerr := body.integerMember("interval_weeks")
		if aerr != nil {
			return 0, nil, validationFailed("interval_weeks must be an integer from 1 to 4")
		}
		if !present {
			return 0, nil, validationFailed("interval_weeks is required")
		}
		if intervalWeeks < minIntervalWeeks || intervalWeeks > maxIntervalWeeks {
			return 0, nil, validationFailed("interval_weeks must be an integer from 1 to 4")
		}

		anchor, aerr := s.st.ownedReservation(caller, anchorReference)
		if aerr != nil {
			return 0, nil, aerr
		}
		if anchor.Status == statusCancelled {
			return 0, nil, conflict("reservation_cancelled",
				"a cancelled booking cannot be adopted")
		}
		if anchor.SeriesID != "" {
			return 0, nil, conflict("already_in_series",
				"this booking already belongs to a recurring agreement")
		}
		now := time.Now()
		if cutoffPassed(anchor, now) {
			return 0, nil, conflict("cutoff_passed",
				"this booking is too close to its start time to adopt")
		}

		rest := s.st.restaurantsByID[anchor.RestaurantID]
		anchorStamp, ok := parseCivilStamp(anchor.StartsAtLocal)
		if !ok {
			return 0, nil, validationFailed("this booking has no usable local start time")
		}

		// Plan every generated occurrence before writing anything. Each one selects
		// the policy for its own date, so a series can span a policy change and a
		// DST transition and still be judged correctly.
		type planned struct {
			plan *bookingPlan
		}
		occurrences := make([]planned, 0, count-1)
		for index := 1; index < count; index++ {
			date := anchorStamp.date.plusDays(index * intervalWeeks * 7)
			stamp := civilStamp{date: date, minutes: anchorStamp.minutes}
			selected := s.st.termsFor(rest, date)
			start, aerr := resolveAgainstHours(rest, selected, stamp)
			if aerr != nil {
				// The first failing occurrence in index order decides the error.
				return 0, nil, aerr
			}
			if anchor.PartySize > selected.capacityOf(anchor.TableIDs) {
				return 0, nil, unprocessable("party_exceeds_capacity",
					"that party does not fit the seating under a later policy")
			}
			if s.st.anyTableOccupied(rest, anchor.TableIDs, start, selected.duration(), nil) {
				return 0, nil, conflict("table_unavailable",
					"one of those tables is already taken for a later occurrence")
			}
			// Occurrences must also not collide with each other, which a short
			// interval and a long duration can manage between them.
			for _, earlier := range occurrences {
				if !setsIntersect(earlier.plan.tableIDs, anchor.TableIDs) {
					continue
				}
				if start.Before(earlier.plan.start.Add(earlier.plan.terms.duration())) &&
					earlier.plan.start.Before(start.Add(selected.duration())) {
					return 0, nil, conflict("table_unavailable",
						"two occurrences of this agreement would overlap")
				}
			}
			occurrences = append(occurrences, planned{plan: &bookingPlan{
				rest:      rest,
				tableIDs:  append([]string(nil), anchor.TableIDs...),
				stamp:     stamp,
				start:     start,
				partySize: anchor.PartySize,
				terms:     selected,
			}})
		}

		// Nothing above has written anything, so from here the whole adoption
		// commits together.
		agreement := &series{
			ID:            "ser_" + randomHex(8),
			UserID:        caller.ID,
			RestaurantID:  rest.ID,
			IntervalWeeks: intervalWeeks,
			Revision:      1,
		}
		// Occurrence zero is the anchor itself: its reference, identity, revision,
		// terms, history and timestamps are left exactly as they were.
		anchor.SeriesID = agreement.ID
		agreement.Occurrences = append(agreement.Occurrences,
			seriesOccurrence{Reference: anchor.Reference})
		for _, occurrence := range occurrences {
			plan := occurrence.plan
			res := &reservation{
				ID:            "res_" + randomHex(8),
				Reference:     s.st.freshReference(),
				UserID:        caller.ID,
				RestaurantID:  rest.ID,
				TableIDs:      plan.tableIDs,
				StartsAtLocal: plan.stamp.String(),
				StartsAt:      formatInstant(plan.start, rest.loc),
				CreatedAt:     formatUTC(now),
				PartySize:     plan.partySize,
				Status:        statusConfirmed,
				Revision:      1,
				AcceptedTerms: plan.terms.copy(),
				SeriesID:      agreement.ID,
				start:         plan.start,
			}
			res.History = []historyEntry{createdEntry(res, res.CreatedAt)}
			s.st.addReservation(res)
			agreement.Occurrences = append(agreement.Occurrences,
				seriesOccurrence{Reference: res.Reference})
		}
		// Adoption is one whole-restaurant operation.
		rest.Revision++
		s.st.addSeries(agreement)
		return http.StatusCreated, s.st.seriesView(agreement), nil
	})
}

// handleGetSeries is GET /series/{id}, with the current state of every occurrence.
// Only the owner may read it; another user or no token is the same 404.
func (s *server) handleGetSeries(w http.ResponseWriter, r *http.Request, seriesID string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	token, _ := bearerToken(r)
	caller := s.st.userByToken(token)
	agreement := s.st.seriesByID[seriesID]
	if caller == nil || agreement == nil || agreement.UserID != caller.ID {
		writeError(w, notFound())
		return
	}
	writeJSON(w, http.StatusOK, s.st.seriesView(agreement))
}

// seriesView is the agreement as the API reports it: the occurrences in index order,
// each with its ordinary reservation response.
func (s *state) seriesView(agreement *series) map[string]any {
	occurrences := make([]any, 0, len(agreement.Occurrences))
	for index, occurrence := range agreement.Occurrences {
		entry := map[string]any{
			"index":     index,
			"reference": occurrence.Reference,
			"exception": occurrence.Exception,
		}
		if res := s.reservationsByRef[occurrence.Reference]; res != nil {
			entry["reservation"] = s.reservationView(res)
		}
		occurrences = append(occurrences, entry)
	}
	return map[string]any{
		"series_id":      agreement.ID,
		"revision":       agreement.Revision,
		"interval_weeks": agreement.IntervalWeeks,
		"occurrences":    occurrences,
	}
}
