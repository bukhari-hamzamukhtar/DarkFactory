"""Stage 4: amending a recurring agreement's clock time."""
from __future__ import annotations

import copy

import conftest as fx
from conftest import ADA, BOB, Api, burst, failed, new_key, ok, tally
import pytest


@pytest.fixture
def agreement(api, reset):
    """A three-week agreement at 19:00 on t_2, owned by Ada, in a managed restaurant."""
    def _make(count=3, at="19:00", party=4, table_id="t_2", interval_weeks=1,
              **kwargs):
        reset(fx.fixture(restaurants=[fx.managed("u_ada", **kwargs)]))

        class World:
            pass

        world = World()
        world.date = fx.booking_date()
        world.ada = api().authenticated(ADA["email"], ADA["password"])
        world.bob = api().authenticated(BOB["email"], BOB["password"])
        world.anon = api()
        world.at = lambda hhmm, date=None: fx.local(date or world.date, hhmm)
        world.anchor = ok(world.ada.book(table_id=table_id, party_size=party,
                                         starts_at_local=world.at(at)), 201)
        world.series = ok(world.ada.post("/series", json={
            "anchor_reference": world.anchor["reference"], "count": count,
            "interval_weeks": interval_weeks}, key=new_key()), 201)
        world.sid = world.series["series_id"]
        world.revision = lambda: ok(world.anon.get("/restaurants/r_anker"))["revision"]
        world.read = lambda: ok(world.ada.get(f"/series/{world.sid}"))
        return world
    return _make


def amend(world, *, expected_revision=1, from_index=0, local_time="20:00", key=None,
          who=None, **extra):
    body = {"expected_revision": expected_revision, "from_index": from_index,
            "local_time": local_time}
    body.update(extra)
    return (who or world.ada).post(f"/series/{world.sid}/amend", json=body,
                                   key=new_key() if key is None else key)


def times_of(series):
    return [occurrence["reservation"]["starts_at_local"]
            for occurrence in series["occurrences"]]


def entries_of(client, reference):
    return ok(client.get(f"/reservations/{reference}/history"))["entries"]


# ---- the ordinary case ---------------------------------------------------

def test_every_eligible_occurrence_moves_to_the_new_time(agreement):
    world = agreement()
    before = world.read()
    result = ok(amend(world, local_time="20:00"), 201)
    assert [time.split("T")[1] for time in times_of(result)] == ["20:00"] * 3
    assert [time.split("T")[0] for time in times_of(result)] == \
        [time.split("T")[0] for time in times_of(before)], \
        "the scheduled dates do not move, only the clock time"
    assert result["revision"] == 2, "one revision for the whole operation"
    assert [occurrence["exception"] for occurrence in result["occurrences"]] == \
        [False] * 3, "a series amendment marks no exceptions"
    for occurrence in result["occurrences"]:
        assert occurrence["reservation"]["revision"] == 2
        entries = entries_of(world.ada, occurrence["reference"])
        assert [entry["event"] for entry in entries] == ["created", "changed"]
        assert entries[1]["changes"] == [{
            "field": "starts_at_local",
            "from": occurrence["reservation"]["starts_at_local"].replace("20:00", "19:00"),
            "to": occurrence["reservation"]["starts_at_local"]}]


def test_from_index_leaves_the_earlier_occurrences_alone(agreement):
    world = agreement()
    result = ok(amend(world, from_index=1, local_time="20:00"), 201)
    assert [time.split("T")[1] for time in times_of(result)] == \
        ["19:00", "20:00", "20:00"]
    assert ok(world.ada.get(f"/reservations/{world.anchor['reference']}")) == \
        world.anchor, "the anchor was not eligible and did not move"


def test_the_series_and_restaurant_revisions_each_move_once(agreement):
    world = agreement()
    before = world.revision()
    ok(amend(world, local_time="20:00"), 201)
    assert world.read()["revision"] == 2
    assert world.revision() == before + 1, \
        "one restaurant revision for the whole operation"


def test_an_all_no_op_amendment_changes_no_revision(agreement):
    world = agreement()
    before_revision = world.revision()
    result = ok(amend(world, local_time="19:00"), 201)
    assert result["revision"] == 1
    assert world.revision() == before_revision
    assert [time.split("T")[1] for time in times_of(result)] == ["19:00"] * 3
    for occurrence in result["occurrences"]:
        assert occurrence["reservation"]["revision"] == 1
        assert len(entries_of(world.ada, occurrence["reference"])) == 1


def test_an_empty_eligible_set_succeeds_and_changes_nothing(agreement):
    world = agreement()
    # Make the last occurrence an exception, then amend from it: nothing is eligible.
    last = world.series["occurrences"][2]["reference"]
    ok(world.ada.patch(f"/reservations/{last}", json={"party_size": 2}))
    revision_now = world.read()["revision"]
    before = world.revision()
    result = ok(amend(world, expected_revision=revision_now, from_index=2,
                      local_time="21:00"), 201)
    assert result["revision"] == revision_now
    assert world.revision() == before
    assert [time.split("T")[1] for time in times_of(result)] == ["19:00"] * 3


def test_cancelled_and_exception_occurrences_are_skipped(agreement):
    world = agreement()
    cancelled = world.series["occurrences"][1]["reference"]
    excepted = world.series["occurrences"][2]["reference"]
    ok(world.ada.post(f"/reservations/{cancelled}/cancel"))
    ok(world.ada.patch(f"/reservations/{excepted}", json={"party_size": 2}))
    revision_now = world.read()["revision"]

    result = ok(amend(world, expected_revision=revision_now, local_time="20:00"), 201)
    times = times_of(result)
    assert times[0].endswith("20:00"), "the anchor moved"
    assert times[1].endswith("19:00"), "a cancelled occurrence is left alone"
    assert times[2].endswith("19:00"), "and so is a diner's exception"
    assert result["occurrences"][1]["reservation"]["status"] == "cancelled"
    assert result["occurrences"][2]["exception"] is True


# ---- validation and precedence -------------------------------------------

@pytest.mark.parametrize("body", [
    {"from_index": 0, "local_time": "20:00"},
    {"expected_revision": 0, "from_index": 0, "local_time": "20:00"},
    {"expected_revision": -1, "from_index": 0, "local_time": "20:00"},
    {"expected_revision": True, "from_index": 0, "local_time": "20:00"},
    {"expected_revision": "1", "from_index": 0, "local_time": "20:00"},
    {"expected_revision": 1.5, "from_index": 0, "local_time": "20:00"},
    {"expected_revision": 1, "local_time": "20:00"},
    {"expected_revision": 1, "from_index": -1, "local_time": "20:00"},
    {"expected_revision": 1, "from_index": 3, "local_time": "20:00"},
    {"expected_revision": 1, "from_index": True, "local_time": "20:00"},
    {"expected_revision": 1, "from_index": 0},
    {"expected_revision": 1, "from_index": 0, "local_time": "25:00"},
    {"expected_revision": 1, "from_index": 0, "local_time": "19:60"},
    {"expected_revision": 1, "from_index": 0, "local_time": "7:00"},
    {"expected_revision": 1, "from_index": 0, "local_time": "19:0"},
    {"expected_revision": 1, "from_index": 0, "local_time": "19:00:00"},
    {"expected_revision": 1, "from_index": 0, "local_time": ""},
])
def test_invalid_input_is_422_and_changes_nothing(agreement, body):
    world = agreement()
    resp = world.ada.post(f"/series/{world.sid}/amend", json=body, key=new_key())
    failed(resp, 422, "validation_failed")
    assert world.read()["revision"] == 1
    assert [time.split("T")[1] for time in times_of(world.read())] == ["19:00"] * 3


def test_a_non_string_local_time_is_malformed(agreement):
    world = agreement()
    resp = world.ada.post(f"/series/{world.sid}/amend", json={
        "expected_revision": 1, "from_index": 0, "local_time": 2000}, key=new_key())
    failed(resp, 400, "malformed_request")


def test_unknown_fields_are_ignored(agreement):
    world = agreement()
    ok(amend(world, local_time="20:00", souvenir="yes", count=99), 201)


def test_a_stale_revision_is_409(agreement):
    world = agreement()
    ok(amend(world, expected_revision=1, local_time="20:00"), 201)
    failed(amend(world, expected_revision=1, local_time="21:00"),
           409, "stale_revision")
    assert [time.split("T")[1] for time in times_of(world.read())] == ["20:00"] * 3


def test_validation_is_answered_before_a_stale_revision(agreement):
    """Stage 4 puts validation first, then the revision, then anything about the
    occurrences themselves."""
    world = agreement()
    ok(amend(world, expected_revision=1, local_time="20:00"), 201)
    # Both wrong: the revision is stale *and* the time is nonsense.
    failed(amend(world, expected_revision=1, local_time="99:99"),
           422, "validation_failed")


def test_a_stale_revision_is_answered_before_any_cutoff(api, reset, pool):
    """A crafted state gives one occurrence an accepted cutoff wide enough that it can
    no longer be changed. With a stale revision as well, staleness answers first.
    """
    reset(fx.fixture(restaurants=[fx.managed("u_ada")]))
    ada = api().authenticated(ADA["email"], ADA["password"])
    anon = api()
    date = fx.booking_date()
    anchor = ok(ada.book(table_id="t_2", starts_at_local=fx.local(date, "19:00")), 201)
    series = ok(ada.post("/series", json={"anchor_reference": anchor["reference"],
                                          "count": 2, "interval_weeks": 1},
                         key=new_key()), 201)

    # Widen the anchor's accepted cutoff in the exported state, then import it back.
    snapshot = ok(anon.get("/_test/export", timeout=fx.CONTROL_TIMEOUT))
    crafted = copy.deepcopy(snapshot)
    for reservation in crafted["state"]["reservations"]:
        if reservation["reference"] == anchor["reference"]:
            reservation["accepted_terms"]["cancellation_cutoff_minutes"] = 60 * 24 * 3650
    resp = anon.post("/_test/import", json=crafted, timeout=fx.CONTROL_TIMEOUT)
    assert resp.status_code == 204, fx.described(resp)

    sid = series["series_id"]
    # The cutoff really has passed for that occurrence.
    failed(ada.post(f"/series/{sid}/amend", json={
        "expected_revision": 1, "from_index": 0, "local_time": "20:00"},
        key=new_key()), 409, "cutoff_passed")
    # And a stale revision is answered before it.
    failed(ada.post(f"/series/{sid}/amend", json={
        "expected_revision": 7, "from_index": 0, "local_time": "20:00"},
        key=new_key()), 409, "stale_revision")
    assert ok(ada.get(f"/series/{sid}"))["revision"] == 1


@pytest.mark.parametrize("local_time,code", [
    ("19:15", "not_on_slot_grid"),
    ("17:00", "outside_opening_hours"),
    ("22:00", "outside_opening_hours"),
])
def test_booking_rules_still_apply_to_every_occurrence(agreement, local_time, code):
    world = agreement()
    failed(amend(world, local_time=local_time), 422, code)
    assert [time.split("T")[1] for time in times_of(world.read())] == ["19:00"] * 3
    assert world.read()["revision"] == 1


def test_an_occupied_target_is_table_unavailable_and_changes_nothing(agreement):
    world = agreement()
    # Bob takes t_2 at 20:30 on the middle occurrence's date.
    middle = world.series["occurrences"][1]["reservation"]
    other_date = middle["starts_at_local"].split("T")[0]
    ok(world.bob.book(table_id="t_2", starts_at_local=fx.local(other_date, "20:30")), 201)
    failed(amend(world, local_time="20:30"), 409, "table_unavailable")
    assert [time.split("T")[1] for time in times_of(world.read())] == ["19:00"] * 3
    assert world.read()["revision"] == 1
    for occurrence in world.read()["occurrences"]:
        assert len(entries_of(world.ada, occurrence["reference"])) == 1


def test_an_applied_closure_blocks_an_amendment(agreement):
    """A repair's closure is as real as a booking."""
    world = agreement()
    first_date = world.anchor["starts_at_local"].split("T")[0]
    from zoneinfo import ZoneInfo
    import datetime as dt

    def instant(at):
        hour, minute = (int(part) for part in at.split(":"))
        return dt.datetime.combine(dt.date.fromisoformat(first_date),
                                   dt.time(hour, minute),
                                   tzinfo=ZoneInfo("Europe/Berlin")).isoformat()

    # Close t_2 for the 20:30 sitting on the anchor's date only.
    plan = ok(world.ada.post("/restaurants/r_anker/replans", json={
        "table_id": "t_2", "from": instant("20:30"), "to": instant("22:00")},
        key=new_key()), 201)
    ok(world.ada.post(f"/restaurants/r_anker/replans/{plan['plan_id']}/apply",
                      json={}, key=new_key()), 201)
    revision_now = world.read()["revision"]
    failed(amend(world, expected_revision=revision_now, local_time="20:30"),
           409, "table_unavailable")


# ---- who may do it -------------------------------------------------------

def test_only_the_owner_may_amend(agreement):
    world = agreement()
    failed(amend(world, who=world.bob), 404, "not_found")
    resp = world.anon.post(f"/series/{world.sid}/amend", json={
        "expected_revision": 1, "from_index": 0, "local_time": "20:00"},
        key=new_key(), token=None)
    failed(resp, 401, "unauthenticated")
    failed(world.ada.post("/series/ser_nope/amend", json={
        "expected_revision": 1, "from_index": 0, "local_time": "20:00"},
        key=new_key()), 404, "not_found")


def test_an_amendment_needs_an_idempotency_key_and_replays(agreement):
    world = agreement()
    failed(world.ada.post(f"/series/{world.sid}/amend", json={
        "expected_revision": 1, "from_index": 0, "local_time": "20:00"}),
        400, "missing_idempotency_key")
    key = new_key()
    first = ok(amend(world, local_time="20:00", key=key), 201)
    # Later edits do not change what the replay says.
    ok(amend(world, expected_revision=2, local_time="20:30"), 201)
    replay = ok(amend(world, local_time="20:00", key=key), 200)
    assert replay == first, "a replay returns the original response"
    assert [time.split("T")[1] for time in times_of(world.read())] == ["20:30"] * 3


def test_a_failed_amendment_leaves_the_key_reusable(agreement):
    world = agreement()
    key = new_key()
    failed(amend(world, local_time="19:15", key=key), 422, "not_on_slot_grid")
    ok(amend(world, local_time="20:00", key=key), 201)


# ---- daylight saving -----------------------------------------------------

def test_an_amendment_onto_a_skipped_local_time_is_refused(api, reset):
    """A weekly agreement that crosses a spring-forward night cannot be moved into the
    hour that does not exist on one of its dates, and nothing moves.

    The dates are 2027 rather than the two transitions stage 1 names, because adoption
    needs an anchor whose cutoff has not passed and both named spring-forward nights
    are in the past. The rule under test is the same one.
    """
    berlin = {**fx.managed("u_ada"), "timezone": "Europe/Berlin",
              "opening_hours": fx.all_week("00:00", "23:30")}
    reset(fx.fixture(restaurants=[berlin]))
    ada = api().authenticated(ADA["email"], ADA["password"])
    # 2027-03-28 is Berlin's spring-forward night: 02:00-02:59 does not exist.
    anchor = ok(ada.book(table_id="t_2", starts_at_local="2027-03-21T01:00"), 201)
    series = ok(ada.post("/series", json={"anchor_reference": anchor["reference"],
                                          "count": 2, "interval_weeks": 1},
                         key=new_key()), 201)
    sid = series["series_id"]
    failed(ada.post(f"/series/{sid}/amend", json={
        "expected_revision": 1, "from_index": 0, "local_time": "02:30"},
        key=new_key()), 422, "invalid_local_time")
    assert [occurrence["reservation"]["starts_at_local"]
            for occurrence in ok(ada.get(f"/series/{sid}"))["occurrences"]] ==         ["2027-03-21T01:00", "2027-03-28T01:00"]

    # A time that exists on both nights is fine.
    result = ok(ada.post(f"/series/{sid}/amend", json={
        "expected_revision": 1, "from_index": 0, "local_time": "01:30"},
        key=new_key()), 201)
    starts = [occurrence["reservation"]["starts_at"]
              for occurrence in result["occurrences"]]
    assert starts[0].endswith("+01:00") and starts[1].endswith("+01:00"), starts


def test_an_amendment_into_a_repeated_hour_takes_the_first_occurrence(api, reset):
    """Europe/Berlin, 2026-10-25: 02:00 happens twice and the earlier one is the one
    that can be booked, so that is the one an amendment lands on."""
    berlin = {**fx.managed("u_ada"), "timezone": "Europe/Berlin",
              "opening_hours": fx.all_week("00:00", "23:30")}
    reset(fx.fixture(restaurants=[berlin]))
    ada = api().authenticated(ADA["email"], ADA["password"])
    anchor = ok(ada.book(table_id="t_2", starts_at_local="2026-10-18T01:00"), 201)
    series = ok(ada.post("/series", json={"anchor_reference": anchor["reference"],
                                          "count": 2, "interval_weeks": 1},
                         key=new_key()), 201)
    result = ok(ada.post(f"/series/{series['series_id']}/amend", json={
        "expected_revision": 1, "from_index": 0, "local_time": "02:00"},
        key=new_key()), 201)
    occurrences = result["occurrences"]
    assert occurrences[0]["reservation"]["starts_at"] == "2026-10-18T02:00:00+02:00"
    assert occurrences[1]["reservation"]["starts_at"] == "2026-10-25T02:00:00+02:00",         "the repeated hour resolves to the occurrence before the clocks change"
    # 90 real minutes from there reads 02:00 CET on the transition night.
    assert occurrences[1]["reservation"]["ends_at"] == "2026-10-25T02:30:00+01:00"


# ---- concurrency ---------------------------------------------------------

def test_two_amendments_from_one_revision_cannot_both_change(agreement, pool):
    """Stage 4: concurrent amendments from the same expected revision may not both
    make a real change."""
    world = agreement()
    token = world.ada.token
    bodies = [{"expected_revision": 1, "from_index": 0, "local_time": "20:00"},
              {"expected_revision": 1, "from_index": 0, "local_time": "20:30"}]

    responses = burst(lambda i: Api(pool, token).post(
        f"/series/{world.sid}/amend", json=bodies[i], key=new_key()), 2)
    counts = tally(responses)
    assert counts.get(201, 0) == 1, counts
    assert counts.get(409, 0) == 1, counts
    for resp in responses:
        if resp.status_code == 409:
            assert resp.json()["error"]["code"] == "stale_revision"
    assert world.read()["revision"] == 2
