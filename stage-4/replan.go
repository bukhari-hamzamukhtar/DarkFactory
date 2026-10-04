package main

import (
	"net/http"
	"sort"
	"time"
)

// Seating changes after a table closure (stage 4).
//
// A manager proposes taking a table out of service over an interval. The service works
// out where the bookings caught by that closure would sit, shows the arrangement, and
// applies it only when asked. Diners keep their times, party sizes and accepted terms;
// their cancellation cutoffs do not stand in the way of an operator's repair.

const (
	maxPlanTables   = 6
	maxPlanPairs    = 4
	maxPlanBookings = 6
)

// planOption is one seating a booking could be given, with the rank the requirements
// assign it: singles first in fixture order from 0, then declared pairs in declared
// order.
type planOption struct {
	ids  []string
	rank int
}

// candidate is one booking the closure catches, with the options open to it.
type candidate struct {
	res      *reservation
	current  []string
	start    time.Time
	end      time.Time
	feasible []planOption
}

// planOutcome is a complete assignment and its cost under the stated objective.
type planOutcome struct {
	choice []planOption // one per candidate, in reference order
	moved  int
	unused int
	ranks  []int
}

func (s *server) handleReplanPreview(w http.ResponseWriter, r *http.Request, restaurantID string) {
	path := "/restaurants/" + restaurantID + "/replans"
	s.serveIdempotent(w, r, path, func(caller *user, body *jsonBody) (int, any, *apiError) {
		rest, aerr := s.managedRestaurant(caller, restaurantID)
		if aerr != nil {
			return 0, nil, aerr
		}
		tableID, present, aerr := body.stringMember("table_id")
		if aerr != nil {
			return 0, nil, aerr
		}
		if !present {
			return 0, nil, validationFailed("table_id is required")
		}
		from, to, aerr := parseClosureInterval(body)
		if aerr != nil {
			return 0, nil, aerr
		}
		if _, known := rest.byTable[tableID]; !known {
			return 0, nil, notFound()
		}

		candidates, aerr := s.st.considered(rest, from, to)
		if aerr != nil {
			return 0, nil, aerr
		}
		options := planOptionsOf(rest)
		if len(rest.Tables) > maxPlanTables || len(rest.pairs) > maxPlanPairs ||
			len(candidates) > maxPlanBookings {
			return 0, nil, unprocessable("planning_limit",
				"this closure is larger than the service plans for")
		}
		for i := range candidates {
			candidates[i].feasible = s.st.feasibleOptions(rest, candidates[i], options,
				tableID, from, to)
		}
		best, found := searchBestPlan(candidates)
		if !found {
			return 0, nil, conflict("no_feasible_plan",
				"every arrangement would leave a booking without a table")
		}

		plan := &seatingPlan{
			ID:              "plan_" + randomHex(8),
			RestaurantID:    rest.ID,
			TableID:         tableID,
			From:            formatInstant(from, rest.loc),
			To:              formatInstant(to, rest.loc),
			BasedOnRevision: rest.Revision,
			MovedCount:      best.moved,
			UnusedSeats:     best.unused,
			from:            from,
			to:              to,
		}
		for i := range candidates {
			plan.Assignments = append(plan.Assignments, planAssignment{
				Reference: candidates[i].res.Reference,
				TableIDs:  copyIDs(best.choice[i].ids),
				Changed:   !sameTableSet(candidates[i].current, best.choice[i].ids),
			})
		}
		if plan.Assignments == nil {
			plan.Assignments = []planAssignment{}
		}
		// A preview stores a plan and nothing else: no closure, no occupancy, no
		// reservation revision, no history, and no restaurant revision.
		s.st.addPlan(plan)
		return http.StatusCreated, planView(plan, rest.Revision), nil
	})
}

func planView(plan *seatingPlan, revision int) map[string]any {
	assignments := make([]any, 0, len(plan.Assignments))
	for _, assignment := range plan.Assignments {
		assignments = append(assignments, assignment)
	}
	return map[string]any{
		"plan_id":             plan.ID,
		"restaurant_revision": revision,
		"closure": map[string]any{
			"table_id": plan.TableID,
			"from":     plan.From,
			"to":       plan.To,
		},
		"assignments":  assignments,
		"moved_count":  plan.MovedCount,
		"unused_seats": plan.UnusedSeats,
	}
}

// managedRestaurant resolves a restaurant the caller is allowed to operate: unknown is
// 404, a signed-in stranger is 403 (stage 3's permission rule, which stage 4 reuses).
func (s *server) managedRestaurant(caller *user, restaurantID string) (*restaurant, *apiError) {
	rest := s.st.restaurantsByID[restaurantID]
	if rest == nil {
		return nil, notFound()
	}
	if !rest.manages(caller.ID) {
		return nil, errorf(http.StatusForbidden, "forbidden",
			"only a manager of this restaurant may do that")
	}
	return rest, nil
}

// parseClosureInterval reads the proposed closure's bounds. A field of the wrong JSON
// type follows §5's general rule and is malformed; a string that is not an instant, a
// missing bound, or a bound that does not precede the other, is the 422 the endpoint
// names.
func parseClosureInterval(body *jsonBody) (time.Time, time.Time, *apiError) {
	bounds := make([]time.Time, 2)
	for i, name := range []string{"from", "to"} {
		text, present, aerr := body.stringMember(name)
		if aerr != nil {
			return time.Time{}, time.Time{}, aerr
		}
		if !present {
			return time.Time{}, time.Time{}, validationFailed(name + " is required")
		}
		instant, ok := parseInstant(text)
		if !ok {
			return time.Time{}, time.Time{}, validationFailed(
				name + " must be an RFC 3339 instant with an offset")
		}
		bounds[i] = instant
	}
	if !bounds[0].Before(bounds[1]) {
		return time.Time{}, time.Time{}, validationFailed("from must be before to")
	}
	return bounds[0], bounds[1], nil
}

// considered is every confirmed booking at this restaurant whose own interval overlaps
// the proposed closure, in ascending reference order.
//
// It is not only the bookings on the closing table: a repair may have to move a
// neighbour to make room for the table that is going out of service. Every other
// booking keeps its assignment and is treated as fixed.
func (s *state) considered(rest *restaurant, from, to time.Time) ([]*candidate, *apiError) {
	var caught []*candidate
	for _, res := range s.Reservations {
		if res.RestaurantID != rest.ID || !res.occupies() {
			continue
		}
		start, end := res.start, res.endsAt()
		if !start.Before(to) || !from.Before(end) {
			continue
		}
		caught = append(caught, &candidate{
			res:     res,
			current: copyIDs(res.TableIDs),
			start:   start,
			end:     end,
		})
	}
	sort.SliceStable(caught, func(i, j int) bool {
		return caught[i].res.Reference < caught[j].res.Reference
	})
	return caught, nil
}

// planOptionsOf ranks every seating the restaurant offers: singles first in fixture
// order, then declared pairs in declared order, starting at 0.
func planOptionsOf(rest *restaurant) []planOption {
	options := make([]planOption, 0, len(rest.Tables)+len(rest.pairs))
	for i := range rest.Tables {
		options = append(options, planOption{ids: []string{rest.Tables[i].ID},
			rank: len(options)})
	}
	for _, pair := range rest.pairs {
		options = append(options, planOption{ids: copyIDs(pair.ids), rank: len(options)})
	}
	return options
}

// feasibleOptions are the seatings a booking could take: big enough under its own
// accepted terms, and free of the proposed closure, of previously applied closures and
// of every booking that is not itself being replanned.
func (s *state) feasibleOptions(rest *restaurant, c *candidate, options []planOption,
	closingTable string, from, to time.Time) []planOption {
	// The bookings being replanned do not block each other here; the search checks
	// them against one another as it goes.
	except := map[string]bool{}
	for _, res := range s.Reservations {
		if res.RestaurantID == rest.ID && res.occupies() {
			if res.start.Before(to) && from.Before(res.endsAt()) {
				except[res.ID] = true
			}
		}
	}
	duration := c.res.AcceptedTerms.duration()
	open := make([]planOption, 0, len(options))
	for _, option := range options {
		if c.res.AcceptedTerms.capacityOf(option.ids) < c.res.PartySize {
			continue
		}
		blocked := false
		for _, id := range option.ids {
			// The proposed closure is not recorded yet, so it is checked by hand.
			if id == closingTable && c.start.Before(to) && from.Before(c.end) {
				blocked = true
				break
			}
			if s.tableOccupied(rest, id, c.start, duration, except) {
				blocked = true
				break
			}
		}
		if !blocked {
			open = append(open, option)
		}
	}
	return open
}

// searchBestPlan finds the feasible arrangement the requirements call best, by their
// definition and no other: fewest bookings whose table set changes, then the least
// total unused seats, then the smallest vector of option ranks read in ascending
// reservation-reference order.
//
// The search is exhaustive over the candidates in that reference order, with the only
// prunes that the objective permits: both counts can only grow as more bookings are
// assigned, so a partial arrangement already worse than the best complete one cannot
// recover, and when the two counts tie the rank prefix decides. Within the stated
// limits -- six tables, four pairs, six bookings -- that is small and exact, which
// matters more here than being clever.
func searchBestPlan(candidates []*candidate) (planOutcome, bool) {
	best := planOutcome{}
	found := false
	choice := make([]planOption, len(candidates))
	ranks := make([]int, 0, len(candidates))

	var walk func(index, moved, unused int)
	walk = func(index, moved, unused int) {
		if found {
			switch compareProgress(moved, unused, ranks, best) {
			case worse:
				return
			}
		}
		if index == len(candidates) {
			best = planOutcome{
				choice: append([]planOption(nil), choice...),
				moved:  moved,
				unused: unused,
				ranks:  append([]int(nil), ranks...),
			}
			found = true
			return
		}
		c := candidates[index]
		for _, option := range c.feasible {
			if conflictsWithChosen(candidates, choice, index, option) {
				continue
			}
			choice[index] = option
			addedMoved := 0
			if !sameTableSet(c.current, option.ids) {
				addedMoved = 1
			}
			addedUnused := c.res.AcceptedTerms.capacityOf(option.ids) - c.res.PartySize
			ranks = append(ranks, option.rank)
			walk(index+1, moved+addedMoved, unused+addedUnused)
			ranks = ranks[:len(ranks)-1]
		}
	}
	walk(0, 0, 0)
	return best, found
}

type progress int

const (
	better progress = iota
	equal
	worse
)

// compareProgress judges a partial arrangement against the best complete one. Both
// counts only grow, so "already worse" is final; a tie on both is decided by the rank
// prefix, which is also monotone in the lexicographic order.
func compareProgress(moved, unused int, ranks []int, best planOutcome) progress {
	if moved != best.moved {
		if moved > best.moved {
			return worse
		}
		return better
	}
	if unused != best.unused {
		if unused > best.unused {
			return worse
		}
		return better
	}
	for i, rank := range ranks {
		if i >= len(best.ranks) {
			return equal
		}
		if rank != best.ranks[i] {
			if rank > best.ranks[i] {
				return worse
			}
			return better
		}
	}
	return equal
}

// conflictsWithChosen reports whether an option would share a table with a booking
// already placed in this arrangement, over an overlapping interval.
func conflictsWithChosen(candidates []*candidate, choice []planOption, index int,
	option planOption) bool {
	for earlier := 0; earlier < index; earlier++ {
		if !setsIntersect(choice[earlier].ids, option.ids) {
			continue
		}
		if candidates[earlier].start.Before(candidates[index].end) &&
			candidates[index].start.Before(candidates[earlier].end) {
			return true
		}
	}
	return false
}

// ---- applying a plan -----------------------------------------------------

func (s *server) handleReplanApply(w http.ResponseWriter, r *http.Request,
	restaurantID, planID string) {
	path := "/restaurants/" + restaurantID + "/replans/" + planID + "/apply"
	s.serveIdempotent(w, r, path, func(caller *user, body *jsonBody) (int, any, *apiError) {
		rest, aerr := s.managedRestaurant(caller, restaurantID)
		if aerr != nil {
			return 0, nil, aerr
		}
		plan := s.st.plansByID[planID]
		if plan == nil || plan.RestaurantID != rest.ID {
			return 0, nil, notFound()
		}
		// Applying a plan moves the restaurant's revision, so an applied plan is
		// always stale as well. Deciding staleness first would make this answer
		// unreachable, so it is decided first.
		if plan.Applied {
			return 0, nil, conflict("plan_already_applied",
				"this plan has already been applied")
		}
		if plan.BasedOnRevision != rest.Revision {
			return 0, nil, conflict("stale_plan",
				"this restaurant has changed since the plan was made")
		}

		now := time.Now()
		affectedSeries := map[string]*series{}
		moved := false
		views := make([]any, 0, len(plan.Assignments))
		// The closure and every assignment are recorded together, under one hold of
		// the store lock, so no reader sees a partly moved arrangement.
		s.st.addClosure(&closure{
			RestaurantID: rest.ID,
			TableID:      plan.TableID,
			From:         plan.From,
			To:           plan.To,
			PlanID:       plan.ID,
			from:         plan.from,
			to:           plan.to,
		})
		for _, assignment := range plan.Assignments {
			res := s.st.reservationsByRef[assignment.Reference]
			if res == nil {
				continue
			}
			if !sameTableSet(res.TableIDs, assignment.TableIDs) {
				before := copyIDs(res.TableIDs)
				// Only the seating moves: the booking keeps its time, its party
				// size and the terms it accepted.
				res.TableIDs = copyIDs(assignment.TableIDs)
				res.Revision++
				entry := historyEntry{
					Seq:   len(res.History) + 1,
					At:    formatUTC(now),
					Event: eventReassigned,
					Changes: []historyChange{{
						Field: "table_ids", From: before, To: copyIDs(res.TableIDs)}},
					PlanID:        plan.ID,
					Revision:      res.Revision,
					AcceptedTerms: res.AcceptedTerms.copy(),
				}
				res.History = append(res.History, entry)
				moved = true
				if agreement := s.st.seriesOf(res); agreement != nil {
					// A repair is not a diner's change: the occurrence keeps its
					// exception flag as it was.
					affectedSeries[agreement.ID] = agreement
				}
			}
			views = append(views, s.st.reservationView(res))
		}
		plan.Applied = true
		// One revision for the whole plan, and one for each agreement that had a
		// member moved.
		rest.Revision++
		if moved {
			for _, agreement := range affectedSeries {
				agreement.Revision++
			}
		}
		return http.StatusCreated, map[string]any{
			"plan_id":             plan.ID,
			"restaurant_revision": rest.Revision,
			"reservations":        views,
		}, nil
	})
}
