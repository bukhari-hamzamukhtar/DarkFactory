package main

import (
	"net/http"
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
	// response: one shape in, the same shape out (§8).
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

// slotsOn builds §8's slot list for one local date.
//
// A slot exists for every `slot_minutes` step from `opens` while the reservation
// still ends by `closes`; a day with no opening hours has none at all. A step whose
// local time does not exist -- the skipped hour of a spring-forward night -- is left
// out entirely (§9), and a repeated hour yields one slot because the stamp resolves
// to its first occurrence.
func (s *server) slotsOn(rest *restaurant, date civilDate, partySize int) []any {
	slots := make([]any, 0)
	hours, open := rest.byDay[date.weekdayKey()]
	if !open {
		return slots
	}
	opens, _ := parseHourMinute(hours.Opens)
	closes, _ := parseHourMinute(hours.Closes)
	for minutes := opens; minutes+rest.DurationMinutes <= closes; minutes += rest.SlotMinutes {
		stamp := date.at(minutes)
		start, exists := resolveLocal(rest.loc, stamp)
		if !exists {
			continue
		}
		free := make([]string, 0, len(rest.Tables))
		for i := range rest.Tables {
			t := &rest.Tables[i]
			if t.Capacity < partySize {
				continue
			}
			if s.st.tableOccupied(rest, t.ID, start, nil) {
				continue
			}
			free = append(free, t.ID)
		}
		slots = append(slots, map[string]any{
			"starts_at_local":     stamp.String(),
			"starts_at":           formatInstant(start, rest.loc),
			"available_table_ids": free,
		})
	}
	return slots
}
