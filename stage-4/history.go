package main

import (
	"net/http"
	"time"
)

// A reservation's own record (stage 3).
//
// Every real write appends exactly one entry; a write that changes nothing appends
// none, and a replay appends none either, because a replay does not re-run the
// operation. `seq` starts at 1 and increases by one, so the order is total even when
// two writes land in the same second.

// bookingSnapshot is what an entry compares: the three fields a diner can change.
type bookingSnapshot struct {
	tableIDs  []string
	stamp     string
	partySize int
}

func snapshotOf(res *reservation) bookingSnapshot {
	return bookingSnapshot{
		tableIDs:  append([]string(nil), res.TableIDs...),
		stamp:     res.StartsAtLocal,
		partySize: res.PartySize,
	}
}

// createdEntry is the first entry of every record: all three fields, each from null.
// A booking of a pair reports `table_ids` instead of `table_id`.
func createdEntry(res *reservation, at string) historyEntry {
	changes := []historyChange{tableChange(nil, res.TableIDs)}
	changes = append(changes,
		historyChange{Field: "starts_at_local", From: nil, To: res.StartsAtLocal},
		historyChange{Field: "party_size", From: nil, To: res.PartySize})
	return historyEntry{
		Seq:           1,
		At:            at,
		Event:         eventCreated,
		Changes:       changes,
		Revision:      res.Revision,
		AcceptedTerms: res.AcceptedTerms.copy(),
	}
}

// tableChange names the seating change the way stage 3 asks: `table_id` when a single
// table becomes a single table, and `table_ids` with complete lists whenever a
// combination is on either side -- including a creation, where `before` is nil.
func tableChange(before, after []string) historyChange {
	singleBefore := len(before) == 1
	singleAfter := len(after) == 1
	if before == nil && singleAfter {
		return historyChange{Field: "table_id", From: nil, To: after[0]}
	}
	if before == nil {
		return historyChange{Field: "table_ids", From: nil, To: copyIDs(after)}
	}
	if singleBefore && singleAfter {
		return historyChange{Field: "table_id", From: before[0], To: after[0]}
	}
	return historyChange{Field: "table_ids", From: copyIDs(before), To: copyIDs(after)}
}

func copyIDs(ids []string) []string {
	return append([]string(nil), ids...)
}

// changesBetween is the diff an amendment records, in the stated field order:
// table, then starts_at_local, then party_size. A field set to the value it already
// has is not a change, and a reversed pair names the same set, so neither records
// anything.
func changesBetween(before, after bookingSnapshot) []historyChange {
	changes := make([]historyChange, 0, 3)
	if !sameTableSet(before.tableIDs, after.tableIDs) {
		changes = append(changes, tableChange(before.tableIDs, after.tableIDs))
	}
	if before.stamp != after.stamp {
		changes = append(changes, historyChange{
			Field: "starts_at_local", From: before.stamp, To: after.stamp})
	}
	if before.partySize != after.partySize {
		changes = append(changes, historyChange{
			Field: "party_size", From: before.partySize, To: after.partySize})
	}
	return changes
}

func sameTableSet(first, second []string) bool {
	if len(first) != len(second) {
		return false
	}
	for _, id := range first {
		found := false
		for _, other := range second {
			if id == other {
				found = true
				break
			}
		}
		if !found {
			return false
		}
	}
	return true
}

// record appends one entry, carrying the reservation's resulting revision and its
// complete accepted terms. Earlier entries are never touched, so an old entry cannot
// acquire newer terms.
func record(res *reservation, event string, changes []historyChange, at time.Time) {
	res.History = append(res.History, historyEntry{
		Seq:           len(res.History) + 1,
		At:            formatUTC(at),
		Event:         event,
		Changes:       changes,
		Revision:      res.Revision,
		AcceptedTerms: res.AcceptedTerms.copy(),
	})
}

// ---- reads ---------------------------------------------------------------

// ownRecord resolves a reservation for the two read paths that answer 404 rather than
// 401 when the caller is unknown.
//
// Stage 3 is explicit: history and decision "return 404 even without authentication,
// resolving the exception to stage 1's general 401 rule". So a missing token, a bad
// token and another diner's booking are all the same answer, and none of them reveals
// that the reference exists.
func (s *server) ownRecordUnderLock(r *http.Request, reference string) (*reservation, *apiError) {
	token, _ := bearerToken(r)
	caller := s.st.userByToken(token)
	res := s.st.reservationByReference(reference)
	if caller == nil || res == nil || res.UserID != caller.ID {
		return nil, notFound()
	}
	return res, nil
}

func (s *server) handleHistory(w http.ResponseWriter, r *http.Request, reference string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	res, aerr := s.ownRecordUnderLock(r, reference)
	if aerr != nil {
		writeError(w, aerr)
		return
	}
	entries := make([]any, 0, len(res.History))
	for i := range res.History {
		entries = append(entries, res.History[i])
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"reference": res.Reference,
		"entries":   entries,
	})
}

func (s *server) handleDecision(w http.ResponseWriter, r *http.Request, reference string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	res, aerr := s.ownRecordUnderLock(r, reference)
	if aerr != nil {
		writeError(w, aerr)
		return
	}
	// The current decision, including after cancellation.
	writeJSON(w, http.StatusOK, map[string]any{
		"reference":      res.Reference,
		"revision":       res.Revision,
		"accepted_terms": res.AcceptedTerms,
	})
}
