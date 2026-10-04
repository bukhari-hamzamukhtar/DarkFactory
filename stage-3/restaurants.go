package main

import (
	"net/http"
	"sort"
	"time"
)

func (s *server) handleListRestaurants(w http.ResponseWriter, r *http.Request) {
	s.mu.Lock()
	defer s.mu.Unlock()
	listed := make([]any, 0, len(s.st.Restaurants))
	for _, rest := range s.st.Restaurants {
		listed = append(listed, map[string]any{
			"id":       rest.ID,
			"name":     rest.Name,
			"timezone": rest.Timezone,
		})
	}
	writeJSON(w, http.StatusOK, map[string]any{"restaurants": listed})
}

// handleGetRestaurant answers the restaurant's *original fixture configuration*, which
// is what stage 3 insists on: a published policy changes booking decisions, not this
// document. The restaurant's revision counter rides along, because it is a fact about
// the restaurant rather than part of its configuration, and nothing else exposes it.
func (s *server) handleGetRestaurant(w http.ResponseWriter, r *http.Request, id string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	rest := s.st.restaurantsByID[id]
	if rest == nil {
		writeError(w, notFound())
		return
	}
	writeJSON(w, http.StatusOK, rest)
}

// requiredQueryID reads a query parameter that must be present and id-shaped.
func requiredQueryID(value, name string) (string, *apiError) {
	if value == "" {
		return "", validationFailed(name + " is required")
	}
	if len(value) > maxIDLength {
		return "", validationFailed(name + " must be at most 64 characters")
	}
	return value, nil
}

// requiredQueryCount parses an integer query parameter. §5 is explicit that such a
// parameter is written as plain decimal digits, so `4.0`, `+4` and `1e9` are rejected
// on their spelling rather than their value.
func requiredQueryCount(value, name string) (int, *apiError) {
	if value == "" {
		return 0, validationFailed(name + " is required")
	}
	for _, c := range value {
		if c < '0' || c > '9' {
			return 0, validationFailed(name + " must be written as plain decimal digits")
		}
	}
	count, err := parseBoundedInt(value)
	if err != nil {
		return 0, validationFailed(name + " is out of range")
	}
	if count < 1 {
		return 0, validationFailed(name + " must be at least 1")
	}
	return count, nil
}

func (s *server) handleAvailability(w http.ResponseWriter, r *http.Request) {
	query := r.URL.Query() // unknown parameters are ignored (§3.4)
	restaurantID, aerr := requiredQueryID(query.Get("restaurant_id"), "restaurant_id")
	if aerr != nil {
		writeError(w, aerr)
		return
	}
	rawDate := query.Get("date")
	if rawDate == "" {
		writeError(w, validationFailed("date is required"))
		return
	}
	date, ok := parseCivilDate(rawDate)
	if !ok {
		writeError(w, validationFailed("date must be a calendar date as YYYY-MM-DD"))
		return
	}
	partySize, aerr := requiredQueryCount(query.Get("party_size"), "party_size")
	if aerr != nil {
		writeError(w, aerr)
		return
	}
	// `explain` is optional and its only accepted value is "true". Anything else --
	// including "false", "1" and the empty string -- is a validation failure, and
	// without it the response keeps stage 1's shape exactly (stage 3).
	explain := false
	if raw, carried := query["explain"]; carried {
		if len(raw) != 1 || raw[0] != "true" {
			writeError(w, validationFailed("explain accepts only the value true"))
			return
		}
		explain = true
	}

	s.mu.Lock()
	defer s.mu.Unlock()
	rest := s.st.restaurantsByID[restaurantID]
	if rest == nil {
		writeError(w, notFound())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"restaurant_id": rest.ID,
		"date":          date.String(),
		"timezone":      rest.Timezone,
		"slots":         s.slotsOn(rest, date, partySize, explain),
	})
}

// bookableSlot is one step of the grid that really exists and really fits.
type bookableSlot struct {
	stamp civilStamp
	start time.Time
}

// slotsOn builds §8's slot list for one local date, under the policy that governs that
// date (stage 3).
//
// A slot exists for every `slot_minutes` step from a sitting's `opens` whose
// reservation still ends by that sitting's `closes`, and a day with no sitting has none
// at all. Two things make that more than arithmetic. A step whose local time does not
// exist -- the skipped hour of a spring-forward night -- is left out entirely, and a
// repeated hour yields one slot because the stamp resolves to its first occurrence
// (§9). And the end of the reservation is compared as an absolute instant against the
// instant the clock reaches `closes`, because the duration is absolute time.
func (s *server) slotsOn(rest *restaurant, date civilDate, partySize int, explain bool) []any {
	selected := s.st.termsFor(rest, date)
	found := make([]bookableSlot, 0)
	seen := map[int]bool{}
	for _, hours := range selected.sittings(date.weekdayKey()) {
		opens, _ := parseHourMinute(hours.Opens)
		closes, _ := parseHourMinute(hours.Closes)
		closing := instantReaching(rest.loc, date.at(closes))
		for minutes := opens; minutes < closes; minutes += selected.SlotMinutes {
			if seen[minutes] {
				continue
			}
			stamp := date.at(minutes)
			start, exists := resolveLocal(rest.loc, stamp)
			if !exists {
				continue
			}
			if start.Add(selected.duration()).After(closing) {
				continue
			}
			seen[minutes] = true
			found = append(found, bookableSlot{stamp, start})
		}
	}
	sort.SliceStable(found, func(i, j int) bool {
		return found[i].stamp.minutes < found[j].stamp.minutes
	})

	slots := make([]any, 0, len(found))
	for _, slot := range found {
		slots = append(slots, s.slotView(rest, selected, slot, partySize, explain))
	}
	return slots
}

// slotView decides one slot for one party size, and -- when asked -- explains itself.
//
// Availability is two independent rules (stage 3): `capacity`, which holds when the
// party fits the selected policy's capacity for that table, and `no_overlap`, which
// holds when no confirmed booking covers the slot's interval. A table is available
// exactly when both hold, and every table is reported once whatever the answer, so a
// rule that holds is still reported for a table some other rule already excluded.
func (s *server) slotView(rest *restaurant, selected terms, slot bookableSlot,
	partySize int, explain bool) map[string]any {
	singles := make([]string, 0, len(rest.Tables))
	options := make([]any, 0, len(rest.Tables)+len(rest.pairs))
	explanations := make([]any, 0, len(rest.Tables))

	for i := range rest.Tables {
		t := &rest.Tables[i]
		fitsCapacity := partySize <= selected.Capacities[t.ID]
		noOverlap := !s.st.tableOccupied(rest, t.ID, slot.start, selected.duration(), nil)
		available := fitsCapacity && noOverlap
		if available {
			singles = append(singles, t.ID)
			options = append(options, map[string]any{
				"table_ids": []string{t.ID},
				"capacity":  selected.Capacities[t.ID],
			})
		}
		if explain {
			explanations = append(explanations, map[string]any{
				"table_id":       t.ID,
				"policy_version": selected.PolicyVersion,
				"available":      available,
				"rules": []any{
					map[string]any{"rule": "capacity", "holds": fitsCapacity},
					map[string]any{"rule": "no_overlap", "holds": noOverlap},
				},
			})
		}
	}

	// Singles first in fixture order, then declared pairs in `combinable` order, each
	// pair holding its ids in the order the restaurant declared (stage 2). A pair's
	// capacity is the sum of the selected policy's capacities (stage 3).
	for _, pair := range rest.pairs {
		if selected.capacityOf(pair.ids) < partySize {
			continue
		}
		if s.st.anyTableOccupied(rest, pair.ids, slot.start, selected.duration(), nil) {
			continue
		}
		options = append(options, map[string]any{
			"table_ids": pair.ids,
			"capacity":  selected.capacityOf(pair.ids),
		})
	}

	view := map[string]any{
		"starts_at_local": slot.stamp.String(),
		"starts_at":       formatInstant(slot.start, rest.loc),
		// Single tables only, exactly as stage 1 defined it.
		"available_table_ids": singles,
		"available_options":   options,
	}
	if explain {
		view["explain"] = explanations
	}
	return view
}
