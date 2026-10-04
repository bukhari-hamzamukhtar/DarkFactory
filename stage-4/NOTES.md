# Stage-3 readings

The requirements leave some things to judgement. Each decision below is recorded with
the sentence it comes from, so a reviewer can disagree with the reading rather than
guess at the behaviour.

## Settled readings

**Restaurant revision.** Stage 3 says adoption "increments the restaurant revision
once for the whole operation" and that a move batch "increases [it] once for the whole
batch", but never shows it in a response. It starts at 1 when a fixture is loaded (the
same convention as a reservation's revision) and is exposed in two places: on
`GET /restaurants/{id}` as `revision`, and in the export state. The restaurant detail
otherwise still answers the original fixture configuration, which is what the
requirements insist on; a counter is not configuration.

**Policy 0 when nothing qualifies.** "Policy 0 is the original fixture's rules and
applies before any published policy." A booking whose local start date precedes every
`effective_from` therefore gets policy 0, not the earliest published policy. Policy 0
is also what a seeded booking accepted, by the same sentence plus "Seeded bookings
start at revision 1 under policy 0".

**Policy selection.** For a booking's local start date, the greatest `effective_from`
not later than that date wins; a tie goes to the greatest `policy_version`. Publication
order is irrelevant to selection, so a policy published later with an earlier date does
not win on recency alone. Availability for a date selects with that date.

**`explain` is a query parameter only.** Its only accepted value is the string `true`;
`false`, `1`, `TRUE` and the empty string are 422. Absent means absent: the response
then carries no explanation field anywhere, which is stage 1's shape exactly.

**History and decision are 404 without a token.** Stage 3 says both carry "history's
owner-only 404 rule" and "return 404 even without authentication, resolving the
exception to stage 1's general 401 rule". So no bearer token gives 404 `not_found`, not
401, on these two paths only -- every other authenticated path keeps stage 1's 401.

**History entry field order.** The example lists `seq`, `at`, `event`, `changes`, and
the prose adds `revision` and `accepted_terms`; entries are emitted in that order, and
each change as `field`, `from`, `to`. Within a `changed` entry the fields appear in the
order `table_id`, `starts_at_local`, `party_size`, as stated.

**A combination in history.** A creation of a pair reports `table_ids` from null to the
pair, in declared order, instead of `table_id`. Any later change involving a pair
reports complete before and after lists under `table_ids`. A single-to-single operation
keeps `table_id`. A reversed input pair names the same set, so it is not a change at
all.

**Occupancy is compared interval by interval.** Once policies can change the duration,
two bookings on one table may have different lengths. A booking keeps the duration it
accepted ("A policy publication does not change existing bookings, their end times"),
so an overlap test compares each booking's own `[start, start + its accepted duration)`
against the candidate's own interval rather than assuming one length for both.

**Series revision and exceptions.** Adoption returns revision 1. A real individual
amendment of an occurrence marks it `exception: true` permanently and increments the
series revision once; a no-op or a failure does neither. A cancellation increments the
series revision once and keeps the occurrence, without marking it an exception, and a
repeated cancel does nothing. A move batch increments each affected series once and
marks each changed occurrence an exception.

**Occurrence zero is untouched.** The sample asserts that the first occurrence's
embedded reservation equals the anchor's original creation response, so adoption must
not alter the anchor's revision, terms, history or timestamps.

**Imported stage-1 and stage-2 state.** Those exports carry no policies, revisions,
terms, history or series. On import each reservation is given revision 1 and policy-0
terms from its restaurant's fixture configuration, plus a single synthesised `created`
history entry stamped with its own `created_at` -- nothing is regenerated, and adoption
then works on those bookings. The import shape check still requires every member a real
export carries and still rejects unknown members; the stage-3 additions are optional
members, which is what lets an older export in.

## Carried forward from stage 2

Two wrong expectations in my own stage-2 tests, found just before stage 2 closed, are
corrected in this folder's copies:

- New York's fall-back night: 90 real minutes from 01:30 EDT ends at local **02:00**
  EST (`2026-11-01T02:00:00-05:00`), not 01:30. The service was right.
- A booking whose start is in the past cannot be amended at all, because its cutoff has
  passed, so a test that wants an opening-hours refusal has to use a future date.

One service improvement identified there and applied here: a timestamp supplied by a
fixture or an import keeps its own spelling when it already carries a numeric offset,
and is otherwise rendered with whatever sub-second precision it had, so a
`...:00.250Z` created_at survives as the same instant rather than being truncated.

# Stage-4 readings

**The restaurant revision starts at 0.** Stage 3 never said where it began and this
folder's predecessor started it at 1; stage 4 is explicit that it "starts at 0 after
reset", so it does. It now increments once for each successful new booking, real
amendment, cancellation, policy publication, plan application, series adoption,
changing move batch and changing series amendment. A preview, a replay, a no-op and a
failure all leave it alone. It is reported on `GET /restaurants/{id}` as both `revision`
and `restaurant_revision` -- the same number under the name stage 4 uses in its own
responses and the name stage 3's folder introduced -- and in the export state.

**An imported state's revision.** A stage-3 export carries a revision and keeps it. A
stage-1 or stage-2 export carries none, so those restaurants start at 0, exactly as a
reset leaves them. Nothing is invented and no booking is re-counted.

**`plan_already_applied` is decided before `stale_plan`.** Applying a plan increments
the restaurant revision, so an applied plan is always stale as well. Checking staleness
first would make `plan_already_applied` unreachable, which cannot be the intent of
naming it. Order on apply: unknown plan or a plan from another restaurant is 404, then
`plan_already_applied`, then `stale_plan`, then the application itself.

**A closure is per restaurant.** `stale_plan` compares the plan against the revision of
*its own* restaurant, so a booking, amendment or closure at another restaurant cannot
invalidate it.

**The `reassigned` history entry.** A moved booking gains one entry whose event is
`reassigned`, carrying a single `table_ids` change with complete before and after lists
and a `plan_id` naming the application, plus the resulting revision and the unchanged
accepted terms. Times and terms are identical before and after, so nothing else is
reported as having changed.

**Replan field types.** A `from` or `to` of the wrong JSON type is 400
`malformed_request`, following §5's general rule; a string that is not an RFC 3339
instant, a missing field, or an interval whose `from` is not before its `to` is 422
`validation_failed`, which is what the endpoint states. Field validation precedes the
table lookup, so an invalid interval with an unknown table is the 422 -- the same
ordering this service documents for every other endpoint.

**The considered set.** Every confirmed booking at the restaurant whose own interval
overlaps `[from, to)` is considered, not only those on the closing table: a repair may
need to move a neighbour to make room. Everything else is fixed. With no considered
booking the plan is feasible and its assignments are an empty list.

**Series amendment and scheduled dates.** An amendment changes the clock time on each
eligible occurrence's scheduled local date. An occurrence's current local date *is* its
scheduled date: only an individual amendment can move a date, and that marks the
occurrence an exception, which excludes it from eligibility; a seating repair preserves
dates by requirement. So the current date is used, and cancelled and exception
occurrences are skipped.

**All-no-op amendments.** An eligible set that is empty, or whose every member already
sits at the requested time, succeeds and increments nothing -- not the series revision,
not the restaurant revision, and no history.
