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

func (s *server) handleGetRestaurant(w http.ResponseWriter, r *http.Request, id string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	rest := s.st.restaurantsByID[id]
	if rest == nil {
		writeError(w, notFound())
		return
	}
	// The restaurant type carries the fixture's field names, so it is the detail
	// response: one shape in, the same shape out (§8), including the combinable
	// pairs stage 2 adds.
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
		"slots":         s.slotsOn(rest, date, partySize),
	})
}

// bookableSlot is one step of the grid that really exists and really fits.
type bookableSlot struct {
	stamp civilStamp
	start time.Time
}

// slotsOn builds §8's slot list for one local date.
//
// A slot exists for every `slot_minutes` step from a sitting's `opens` whose
// reservation still ends by that sitting's `closes`, and a day with no sitting has
// none at all. Two things make that more than arithmetic. A step whose local time
// does not exist -- the skipped hour of a spring-forward night -- is left out
// entirely, and a repeated hour yields one slot because the stamp resolves to its
// first occurrence (§9). And the end of the reservation is compared as an absolute
// instant against the instant the clock reaches `closes`, because the duration is
// absolute time: on a transition night the wall clock and the real clock disagree
// about which slots fit, and the real clock is the one that decides.
func (s *server) slotsOn(rest *restaurant, date civilDate, partySize int) []any {
	found := make([]bookableSlot, 0)
	seen := map[int]bool{}
	for _, hours := range rest.byDay[date.weekdayKey()] {
		opens, _ := parseHourMinute(hours.Opens)
		closes, _ := parseHourMinute(hours.Closes)
		closing := instantReaching(rest.loc, date.at(closes))
		for minutes := opens; minutes < closes; minutes += rest.SlotMinutes {
			if seen[minutes] {
				continue
			}
			stamp := date.at(minutes)
			start, exists := resolveLocal(rest.loc, stamp)
			if !exists {
				continue
			}
			if start.Add(rest.duration()).After(closing) {
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
		singles := make([]string, 0, len(rest.Tables))
		options := make([]any, 0, len(rest.Tables)+len(rest.pairs))
		for i := range rest.Tables {
			t := &rest.Tables[i]
			if t.Capacity < partySize {
				continue
			}
			if s.st.tableOccupied(rest, t.ID, slot.start, nil) {
				continue
			}
			singles = append(singles, t.ID)
			options = append(options, map[string]any{
				"table_ids": []string{t.ID},
				"capacity":  t.Capacity,
			})
		}
		// Singles first in fixture order, then declared pairs in `combinable`
		// order, each pair holding its ids in the order the restaurant declared.
		for _, pair := range rest.pairs {
			if pair.capacity < partySize {
				continue
			}
			if s.st.anyTableOccupied(rest, pair.ids, slot.start, nil) {
				continue
			}
			options = append(options, map[string]any{
				"table_ids": pair.ids,
				"capacity":  pair.capacity,
			})
		}
		slots = append(slots, map[string]any{
			"starts_at_local": slot.stamp.String(),
			"starts_at":       formatInstant(slot.start, rest.loc),
			// Single tables only, exactly as stage 1 defined it.
			"available_table_ids": singles,
			"available_options":   options,
		})
	}
	return slots
}
