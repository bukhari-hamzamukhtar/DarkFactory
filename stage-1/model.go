package main

import (
	"encoding/json"
	"time"
)

// The model in §4. Restaurants and tables arrive only through a reset fixture or an
// import, so these types double as the fixture shape, the export shape and the
// in-memory shape -- one definition, no translation layer to drift.

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

	// Derived on load; unexported fields never reach JSON.
	loc     *time.Location
	byTable map[string]*table
	byDay   map[string]openingHours
}

func (r *restaurant) duration() time.Duration {
	return time.Duration(r.DurationMinutes) * time.Minute
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
	ID            string `json:"id"`
	Reference     string `json:"reference"`
	UserID        string `json:"user_id"`
	RestaurantID  string `json:"restaurant_id"`
	TableID       string `json:"table_id"`
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
