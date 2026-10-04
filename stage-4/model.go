package main

import (
	"encoding/json"
	"sort"
	"time"
)

// The model: §4's restaurants and tables, stage 2's combinable pairs, and stage 3's
// policies, accepted terms, history and recurring series. These types double as the
// fixture shape, the export shape and the in-memory shape, so an export cannot drift
// from what the service actually holds.

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
	// ManagerUserIDs are the only users who may publish a policy (stage 3).
	ManagerUserIDs []string `json:"manager_user_ids"`
	// Revision counts the whole-restaurant operations stage 3 defines: one per
	// series adoption, one per move batch. It starts at 1 with the fixture.
	Revision int `json:"revision"`

	// Derived on load; unexported fields never reach JSON.
	loc     *time.Location
	byTable map[string]*table
	pairs   []tablePair
}

// tablePair is a declared combination, kept in declaration order.
type tablePair struct {
	ids []string
}

// manages reports whether a user may publish policies for this restaurant.
func (r *restaurant) manages(userID string) bool {
	for _, id := range r.ManagerUserIDs {
		if id == userID {
			return true
		}
	}
	return false
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

// policyZero is the fixture's own rules: what applies before any policy is published,
// and what every seeded booking accepted.
func (r *restaurant) policyZero() terms {
	capacities := map[string]int{}
	for i := range r.Tables {
		capacities[r.Tables[i].ID] = r.Tables[i].Capacity
	}
	return terms{
		PolicyVersion:   0,
		SlotMinutes:     r.SlotMinutes,
		DurationMinutes: r.DurationMinutes,
		CutoffMinutes:   r.CutoffMinutes,
		OpeningHours:    append([]openingHours(nil), r.OpeningHours...),
		Capacities:      capacities,
	}
}

// terms is a complete set of booking rules as a reservation accepted them. It is also
// exactly the `accepted_terms` shape: a policy without its effective date.
type terms struct {
	PolicyVersion   int            `json:"policy_version"`
	SlotMinutes     int            `json:"slot_minutes"`
	DurationMinutes int            `json:"reservation_duration_minutes"`
	CutoffMinutes   int            `json:"cancellation_cutoff_minutes"`
	OpeningHours    []openingHours `json:"opening_hours"`
	Capacities      map[string]int `json:"capacities"`
}

func (t terms) duration() time.Duration {
	return time.Duration(t.DurationMinutes) * time.Minute
}

// sittings are these terms' opening hours for one weekday, earliest first. Each is its
// own slot grid, anchored at its own opening time.
func (t terms) sittings(weekday string) []openingHours {
	var open []openingHours
	for _, hours := range t.OpeningHours {
		if hours.Weekday == weekday {
			open = append(open, hours)
		}
	}
	sort.SliceStable(open, func(i, j int) bool {
		first, _ := parseHourMinute(open[i].Opens)
		second, _ := parseHourMinute(open[j].Opens)
		return first < second
	})
	return open
}

// capacityOf is a seating's capacity under these terms: for a combination, the sum of
// the selected policy's capacities (stage 3).
func (t terms) capacityOf(tableIDs []string) int {
	total := 0
	for _, id := range tableIDs {
		total += t.Capacities[id]
	}
	return total
}

func (t terms) copy() terms {
	capacities := make(map[string]int, len(t.Capacities))
	for id, seats := range t.Capacities {
		capacities[id] = seats
	}
	return terms{
		PolicyVersion:   t.PolicyVersion,
		SlotMinutes:     t.SlotMinutes,
		DurationMinutes: t.DurationMinutes,
		CutoffMinutes:   t.CutoffMinutes,
		OpeningHours:    append([]openingHours(nil), t.OpeningHours...),
		Capacities:      capacities,
	}
}

// policy is a published policy: a complete set of terms plus the local date from which
// they apply. Policies are immutable once published.
type policy struct {
	PolicyVersion   int            `json:"policy_version"`
	EffectiveFrom   string         `json:"effective_from"`
	SlotMinutes     int            `json:"slot_minutes"`
	DurationMinutes int            `json:"reservation_duration_minutes"`
	CutoffMinutes   int            `json:"cancellation_cutoff_minutes"`
	OpeningHours    []openingHours `json:"opening_hours"`
	Capacities      map[string]int `json:"capacities"`
}

func (p *policy) terms() terms {
	return terms{
		PolicyVersion:   p.PolicyVersion,
		SlotMinutes:     p.SlotMinutes,
		DurationMinutes: p.DurationMinutes,
		CutoffMinutes:   p.CutoffMinutes,
		OpeningHours:    p.OpeningHours,
		Capacities:      p.Capacities,
	}.copy()
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

// historyChange is one field that moved, with its old and new values. `from` is null
// on a creation.
type historyChange struct {
	Field string `json:"field"`
	From  any    `json:"from"`
	To    any    `json:"to"`
}

// historyEntry is one event in a reservation's own record. The field order is the
// order the requirements show.
type historyEntry struct {
	Seq     int             `json:"seq"`
	At      string          `json:"at"`
	Event   string          `json:"event"`
	Changes []historyChange `json:"changes"`
	// PlanID names the seating plan that moved the booking, on a `reassigned`
	// entry and nowhere else (stage 4).
	PlanID        string `json:"plan_id,omitempty"`
	Revision      int    `json:"revision"`
	AcceptedTerms terms  `json:"accepted_terms"`
}

const (
	eventCreated    = "created"
	eventChanged    = "changed"
	eventCancelled  = "cancelled"
	eventReassigned = "reassigned"
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
	// only so that an older export imports unchanged (§10); prepare folds it into
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

	// Stage 3: the terms this booking accepted, how many times it has really
	// changed, its own record, and the agreement it belongs to.
	Revision      int            `json:"revision"`
	AcceptedTerms terms          `json:"accepted_terms"`
	History       []historyEntry `json:"history"`
	SeriesID      string         `json:"series_id,omitempty"`

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

// endsAt is the end of this booking's occupancy, in absolute time, under the terms it
// accepted. A policy published afterwards does not move it.
func (r *reservation) endsAt() time.Time {
	return r.start.Add(r.AcceptedTerms.duration())
}

// overlaps compares half-open intervals in absolute time (§1, §9). The two intervals
// may be different lengths, because each booking keeps the duration it accepted.
func (r *reservation) overlaps(start time.Time, duration time.Duration) bool {
	return r.start.Before(start.Add(duration)) && start.Before(r.endsAt())
}

// seriesOccurrence is one slot of a recurring agreement.
type seriesOccurrence struct {
	Reference string `json:"reference"`
	Exception bool   `json:"exception"`
}

// series is a recurring agreement: an adopted anchor and the occurrences generated
// from it, in index order.
type series struct {
	ID            string             `json:"id"`
	UserID        string             `json:"user_id"`
	RestaurantID  string             `json:"restaurant_id"`
	IntervalWeeks int                `json:"interval_weeks"`
	Revision      int                `json:"revision"`
	Occurrences   []seriesOccurrence `json:"occurrences"`
}

func (s *series) indexOf(reference string) int {
	for i := range s.Occurrences {
		if s.Occurrences[i].Reference == reference {
			return i
		}
	}
	return -1
}

// closure is a table taken out of service over an absolute interval, recorded by a
// plan application (stage 4). While it stands, the table is unavailable: it drops out
// of availability on its own and inside any pair, it refuses bookings and amendments,
// and it reads as `no_overlap` false in an explanation.
type closure struct {
	RestaurantID string `json:"restaurant_id"`
	TableID      string `json:"table_id"`
	From         string `json:"from"`
	To           string `json:"to"`
	PlanID       string `json:"plan_id"`

	from time.Time
	to   time.Time
}

// covers reports whether this closure stands over any part of [start, end).
func (c *closure) covers(tableID string, start, end time.Time) bool {
	if c.TableID != tableID {
		return false
	}
	return c.from.Before(end) && start.Before(c.to)
}

// planAssignment is where one considered booking would sit under a plan.
type planAssignment struct {
	Reference string   `json:"reference"`
	TableIDs  []string `json:"table_ids"`
	Changed   bool     `json:"changed"`
}

// seatingPlan is a proposed repair for a closure: the arrangement a manager previewed,
// kept until it is applied or goes stale (stage 4).
type seatingPlan struct {
	ID           string `json:"id"`
	RestaurantID string `json:"restaurant_id"`
	TableID      string `json:"table_id"`
	From         string `json:"from"`
	To           string `json:"to"`
	// BasedOnRevision is the restaurant revision the plan was computed against. Any
	// later change to that restaurant invalidates the plan.
	BasedOnRevision int              `json:"based_on_revision"`
	Assignments     []planAssignment `json:"assignments"`
	MovedCount      int              `json:"moved_count"`
	UnusedSeats     int              `json:"unused_seats"`
	Applied         bool             `json:"applied"`

	from time.Time
	to   time.Time
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
