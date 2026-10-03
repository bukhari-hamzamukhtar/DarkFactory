package main

import (
	"encoding/json"
	"time"
)

// The model in §4, with stage 2's combinable pairs. Restaurants and tables arrive
// only through a reset fixture or an import, so these types double as the fixture
// shape, the export shape and the in-memory shape -- one definition, no translation
// layer to drift.

type table struct {
	ID       string `json:"id"`
	Label    string `json:"label"`
	Capacity int    `json:"capacity"`
}

type openingHours struct {
	Weekday string `json:"weekday"`
	Opens   string `json:"opens"`
	Closes  string `json:"closes"`
}

type restaurant struct {
	ID              string         `json:"id"`
	Name            string         `json:"name"`
	Timezone        string         `json:"timezone"`
	SlotMinutes     int            `json:"slot_minutes"`
	DurationMinutes int            `json:"reservation_duration_minutes"`
	CutoffMinutes   int            `json:"cancellation_cutoff_minutes"`
	OpeningHours    []openingHours `json:"opening_hours"`
	Tables          []table        `json:"tables"`
	// Combinable is the restaurant's declared pairs of tables, each an unordered
	// pair that may be booked as one seating. The order within a pair is the order
	// the restaurant listed, because that is the order the API must echo.
	Combinable [][]string `json:"combinable,omitempty"`

	// Derived on load; unexported fields never reach JSON.
	loc *time.Location
	// byDay holds every sitting on a weekday. A restaurant that serves lunch and
	// dinner declares two, and each is its own grid anchored at its own opening
	// time, so neither has to be guessed at nor the fixture rejected.
	byDay   map[string][]openingHours
	byTable map[string]*table
	pairs   []tablePair
}

// tablePair is a declared combination, kept in declaration order with its summed
// capacity worked out once.
type tablePair struct {
	ids      []string
	capacity int
}

func (r *restaurant) duration() time.Duration {
	return time.Duration(r.DurationMinutes) * time.Minute
}

// pairAllowed reports whether a two-table set is one the restaurant declared, and
// returns it in the declared order. Combining is neither implied by table sizes nor
// transitive: only a listed pair counts.
func (r *restaurant) pairAllowed(ids []string) ([]string, bool) {
	if len(ids) != 2 {
		return nil, false
	}
	for _, pair := range r.pairs {
		if (pair.ids[0] == ids[0] && pair.ids[1] == ids[1]) ||
			(pair.ids[0] == ids[1] && pair.ids[1] == ids[0]) {
			return pair.ids, true
		}
	}
	return nil, false
}

type user struct {
	ID           string `json:"id"`
	Email        string `json:"email"`
	DisplayName  string `json:"display_name"`
	PasswordHash string `json:"password_hash"`
}

const (
	statusConfirmed = "confirmed"
	statusCancelled = "cancelled"
)

type reservation struct {
	ID        string `json:"id"`
	Reference string `json:"reference"`
	UserID    string `json:"user_id"`
	// TableIDs is the whole seating: one table, or a declared pair. A reservation
	// occupies every table in it for its full duration.
	TableIDs     []string `json:"table_ids"`
	RestaurantID string   `json:"restaurant_id"`
	// LegacyTableID carries a single table id the way stage 1 wrote it. It exists
	// only so that a stage-1 export imports unchanged (§10); prepare folds it into
	// TableIDs and clears it, so nothing downstream has two places to look.
	LegacyTableID string `json:"table_id,omitempty"`
	StartsAtLocal string `json:"starts_at_local"`
	// StartsAt and CreatedAt are stored as rendered RFC 3339 strings so that an
	// export and import hand back the very same timestamps rather than new ones
	// computed from a clock or a zone database (§10).
	StartsAt  string `json:"starts_at"`
	CreatedAt string `json:"created_at"`
	PartySize int    `json:"party_size"`
	Status    string `json:"status"`

	start time.Time // derived from StartsAt on load
}

func (r *reservation) occupies() bool { return r.Status == statusConfirmed }

// holds reports whether this reservation occupies the given table.
func (r *reservation) holds(tableID string) bool {
	for _, id := range r.TableIDs {
		if id == tableID {
			return true
		}
	}
	return false
}

// overlaps compares half-open intervals [starts_at, starts_at+duration) in absolute
// time (§1, §9): wall-clock arithmetic would be wrong across a DST transition.
func (r *reservation) overlaps(start time.Time, duration time.Duration) bool {
	end := r.start.Add(duration)
	otherEnd := start.Add(duration)
	return r.start.Before(otherEnd) && start.Before(end)
}

// receipt is one completed idempotent request: the body that produced it and the
// exact response bytes to replay (§7).
type receipt struct {
	UserID   string          `json:"user_id"`
	Method   string          `json:"method"`
	Path     string          `json:"path"`
	Key      string          `json:"key"`
	BodyHash string          `json:"body_hash"`
	Status   int             `json:"status"`
	Response json.RawMessage `json:"response"`
}

func receiptKey(userID, method, path, key string) string {
	return userID + "\x00" + method + "\x00" + path + "\x00" + key
}
