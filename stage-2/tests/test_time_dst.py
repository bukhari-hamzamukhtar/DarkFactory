"""§9: local time at the restaurant, including both daylight-saving transitions.

The transitions named in the requirements, and the three rules that govern them: a
skipped hour has no instants, a repeated hour resolves to its first occurrence, and a
reservation's duration is absolute time rather than wall-clock time.
"""
from __future__ import annotations

import datetime as dt

import conftest as fx
from conftest import ADA, failed, new_key, ok
import pytest

BERLIN_SPRING, BERLIN_FALL = "2026-03-29", "2026-10-25"
NY_SPRING, NY_FALL = "2026-03-08", "2026-11-01"


@pytest.fixture
def zones(api, reset):
    """Two restaurants either side of the Atlantic, open nearly around the clock."""
    def _make(slot_minutes=30, duration=90):
        berlin = fx.restaurant("r_berlin", timezone="Europe/Berlin",
                               slot_minutes=slot_minutes,
                               reservation_duration_minutes=duration,
                               opening_hours=fx.all_week("00:00", "23:30"))
        newyork = fx.restaurant("r_ny", timezone="America/New_York",
                                slot_minutes=slot_minutes,
                                reservation_duration_minutes=duration,
                                opening_hours=fx.all_week("00:00", "23:30"))
        reset(fx.fixture(restaurants=[berlin, newyork]))
        return api().authenticated(ADA["email"], ADA["password"]), api()
    return _make


def times_on(client, rid, date):
    body = ok(client.get("/availability", params={
        "restaurant_id": rid, "date": date, "party_size": 4}))
    return [slot["starts_at_local"].split("T")[1] for slot in body["slots"]]


def book_at(client, rid, stamp, *, table_id="t_2", party_size=4):
    return client.post("/reservations", json={
        "restaurant_id": rid, "table_id": table_id,
        "starts_at_local": stamp, "party_size": party_size}, key=new_key())


# ---- spring forward ------------------------------------------------------

@pytest.mark.parametrize("rid,date", [("r_berlin", BERLIN_SPRING), ("r_ny", NY_SPRING)])
def test_the_skipped_hour_never_appears_in_availability(zones, rid, date):
    _, anon = zones()
    times = times_on(anon, rid, date)
    assert "02:00" not in times and "02:30" not in times, times
    assert "01:30" in times and "03:00" in times, times
    assert times.count("03:00") == 1, times


@pytest.mark.parametrize("rid,date", [("r_berlin", BERLIN_SPRING), ("r_ny", NY_SPRING)])
@pytest.mark.parametrize("at", ["02:00", "02:30"])
def test_booking_a_skipped_local_time_is_rejected(zones, rid, date, at):
    ada, _ = zones()
    failed(book_at(ada, rid, f"{date}T{at}"), 422, "invalid_local_time")


def test_a_reservation_over_the_spring_gap_ends_90_real_minutes_later(zones):
    """01:30 CET plus 90 real minutes is 04:00 CEST: the wall clock jumped an hour
    in the middle of the booking, and the booking did not get longer."""
    ada, _ = zones()
    body = ok(book_at(ada, "r_berlin", f"{BERLIN_SPRING}T01:30"), 201)
    assert body["starts_at"].endswith("+01:00"), body["starts_at"]
    assert body["ends_at"] == f"{BERLIN_SPRING}T04:00:00+02:00", body["ends_at"]
    assert elapsed(body) == dt.timedelta(minutes=90)


def test_occupancy_across_the_spring_gap_is_measured_in_absolute_time(zones):
    """01:30 occupies until 02:00 UTC, and the next slot that really starts then is
    local 04:00. 03:00 and 03:30 are inside the booking even though they read as
    after its local end."""
    ada, anon = zones()
    ok(book_at(ada, "r_berlin", f"{BERLIN_SPRING}T01:30"), 201)
    free = {slot["starts_at_local"].split("T")[1]: slot["available_table_ids"]
            for slot in ok(anon.get("/availability", params={
                "restaurant_id": "r_berlin", "date": BERLIN_SPRING,
                "party_size": 4}))["slots"]}
    for inside in ("01:30", "03:00", "03:30"):
        assert "t_2" not in free[inside], f"t_2 still offered at {inside}"
    assert "t_2" in free["04:00"], "04:00 is exactly when the booking ends"


def test_a_fixture_cannot_seed_a_booking_in_a_skipped_hour(reset):
    berlin = fx.restaurant("r_berlin", timezone="Europe/Berlin",
                           opening_hours=fx.all_week("00:00", "23:30"))
    seeded = {"id": "res_seed", "reference": "SEED01", "user_id": "u_ada",
              "restaurant_id": "r_berlin", "table_id": "t_2",
              "starts_at_local": f"{BERLIN_SPRING}T02:30", "party_size": 4}
    failed(reset(fx.fixture(restaurants=[berlin], reservations=[seeded]), raw=True),
           422, "validation_failed")


# ---- fall back -----------------------------------------------------------

@pytest.mark.parametrize("rid,date,repeated", [
    ("r_berlin", BERLIN_FALL, "02:00"),
    ("r_ny", NY_FALL, "01:00"),
])
def test_the_repeated_hour_appears_exactly_once(zones, rid, date, repeated):
    _, anon = zones()
    times = times_on(anon, rid, date)
    assert times.count(repeated) == 1, times
    assert times.count(add_half_hour(repeated)) == 1, times
    assert len(times) == len(set(times)), f"no slot may appear twice: {times}"


@pytest.mark.parametrize("rid,date,at,offset", [
    ("r_berlin", BERLIN_FALL, "02:00", "+02:00"),
    ("r_berlin", BERLIN_FALL, "02:30", "+02:00"),
    ("r_ny", NY_FALL, "01:00", "-04:00"),
    ("r_ny", NY_FALL, "01:30", "-04:00"),
])
def test_a_repeated_local_time_resolves_to_its_first_occurrence(zones, rid, date, at, offset):
    ada, anon = zones()
    body = ok(book_at(ada, rid, f"{date}T{at}"), 201)
    assert body["starts_at"] == f"{date}T{at}:00{offset}", body["starts_at"]
    listed = next(slot for slot in ok(anon.get("/availability", params={
        "restaurant_id": rid, "date": date, "party_size": 4}))["slots"]
        if slot["starts_at_local"].endswith(at))
    assert listed["starts_at"].endswith(offset), \
        "availability and the booking must agree on which occurrence it is"


@pytest.mark.parametrize("rid,date,at", [
    ("r_berlin", BERLIN_FALL, "02:00"),
    ("r_ny", NY_FALL, "01:00"),
])
def test_the_second_occurrence_is_not_bookable(zones, rid, date, at):
    """There is no spelling of the second occurrence: the same local stamp always
    names the first one, so asking again is a collision rather than a new booking."""
    ada, _ = zones()
    ok(book_at(ada, rid, f"{date}T{at}"), 201)
    failed(book_at(ada, rid, f"{date}T{at}"), 409, "table_unavailable")


def test_duration_is_absolute_time_across_the_fall_back(zones):
    """90 real minutes from 01:30 CEST ends at local 02:00, not 03:00.

    The offset on the end is +01:00, because the instant 90 minutes after
    01:30+02:00 is exactly when the clocks go back: the same wall reading as the
    start plus half an hour, on the other side of the transition.
    """
    ada, _ = zones()
    body = ok(book_at(ada, "r_berlin", f"{BERLIN_FALL}T01:30"), 201)
    assert body["starts_at"] == f"{BERLIN_FALL}T01:30:00+02:00", body["starts_at"]
    assert body["ends_at"] == f"{BERLIN_FALL}T02:00:00+01:00", body["ends_at"]
    assert elapsed(body) == dt.timedelta(minutes=90)


def test_duration_is_absolute_time_across_the_new_york_fall_back(zones):
    """01:00 EDT plus 90 real minutes reads 01:30 EST -- earlier on the wall clock
    than a naive addition would give."""
    ada, _ = zones()
    body = ok(book_at(ada, "r_ny", f"{NY_FALL}T01:00"), 201)
    assert body["starts_at"] == f"{NY_FALL}T01:00:00-04:00", body["starts_at"]
    assert body["ends_at"] == f"{NY_FALL}T01:30:00-05:00", body["ends_at"]
    assert elapsed(body) == dt.timedelta(minutes=90)


def test_both_sides_of_a_transition_carry_their_own_offset(zones):
    ada, _ = zones()
    for rid, date, at, offset in (
        ("r_berlin", BERLIN_FALL, "01:00", "+02:00"),
        ("r_berlin", BERLIN_FALL, "12:00", "+01:00"),
        ("r_berlin", BERLIN_SPRING, "01:00", "+01:00"),
        ("r_berlin", BERLIN_SPRING, "12:00", "+02:00"),
        ("r_ny", NY_FALL, "00:00", "-04:00"),
        ("r_ny", NY_FALL, "12:00", "-05:00"),
        ("r_ny", NY_SPRING, "01:00", "-05:00"),
        ("r_ny", NY_SPRING, "12:00", "-04:00"),
    ):
        # Each of these eight is a different restaurant, date or hour, so they can
        # all stand at once on one table and none needs cancelling afterwards --
        # which matters, because a booking in the past cannot be cancelled (§8).
        body = ok(book_at(ada, rid, f"{date}T{at}", table_id="t_3", party_size=4), 201)
        assert body["starts_at"].endswith(offset), (rid, date, at, body["starts_at"])


def test_the_repeated_hour_offers_one_hour_of_capacity_not_two(zones):
    """The consequence of always resolving to the first occurrence.

    A booking at 01:30 CEST runs from 23:30Z to 01:00Z. Local 02:00 names the
    *first* occurrence, 00:00Z, which is inside that booking -- so it collides. Local
    03:00 is 02:00Z, which is free. The second occurrence of 02:00 can never be
    booked, exactly as §9 requires.
    """
    ada, _ = zones()
    first = ok(book_at(ada, "r_berlin", f"{BERLIN_FALL}T01:30"), 201)
    assert first["ends_at"] == f"{BERLIN_FALL}T02:00:00+01:00"

    failed(book_at(ada, "r_berlin", f"{BERLIN_FALL}T02:00"), 409, "table_unavailable")

    later = ok(book_at(ada, "r_berlin", f"{BERLIN_FALL}T03:00"), 201)
    assert later["starts_at"] == f"{BERLIN_FALL}T03:00:00+01:00", later["starts_at"]
    assert dt.datetime.fromisoformat(later["starts_at"])         - dt.datetime.fromisoformat(first["ends_at"]) == dt.timedelta(hours=1)


def test_a_seeded_booking_at_a_repeated_time_holds_the_first_occurrence(reset, api):
    berlin = fx.restaurant("r_berlin", timezone="Europe/Berlin",
                           opening_hours=fx.all_week("00:00", "23:30"))
    seeded = {"id": "res_seed", "reference": "SEED01", "user_id": "u_ada",
              "restaurant_id": "r_berlin", "table_id": "t_2",
              "starts_at_local": f"{BERLIN_FALL}T02:00", "party_size": 4}
    reset(fx.fixture(restaurants=[berlin], reservations=[seeded]))
    ada = api().authenticated(ADA["email"], ADA["password"])
    body = ok(ada.get("/reservations/SEED01"))
    assert body["starts_at"] == f"{BERLIN_FALL}T02:00:00+02:00", body["starts_at"]
    free = {slot["starts_at_local"].split("T")[1]: slot["available_table_ids"]
            for slot in ok(ada.get("/availability", params={
                "restaurant_id": "r_berlin", "date": BERLIN_FALL,
                "party_size": 4}))["slots"]}
    assert "t_2" not in free["02:00"]


# ---- zones are independent ----------------------------------------------

def test_two_zones_reach_the_same_instant_from_different_local_times(zones):
    ada, _ = zones()
    berlin = ok(book_at(ada, "r_berlin", "2026-12-01T18:00"), 201)
    newyork = ok(book_at(ada, "r_ny", "2026-12-01T12:00"), 201)
    assert dt.datetime.fromisoformat(berlin["starts_at"]) == \
        dt.datetime.fromisoformat(newyork["starts_at"])
    assert berlin["starts_at"].endswith("+01:00")
    assert newyork["starts_at"].endswith("-05:00")


def test_a_slot_grid_of_15_minutes_still_skips_the_whole_missing_hour(zones):
    _, anon = zones(slot_minutes=15, duration=60)
    times = times_on(anon, "r_berlin", BERLIN_SPRING)
    for missing in ("02:00", "02:15", "02:30", "02:45"):
        assert missing not in times, times
    assert "01:45" in times and "03:00" in times, times


def test_an_amendment_onto_a_skipped_local_time_is_rejected(zones):
    """The booking being amended is in the future, so its cutoff is clear and the
    only thing left to object to is the target time itself."""
    ada, _ = zones()
    future = fx.booking_date("Europe/Berlin")
    booking = ok(book_at(ada, "r_berlin", f"{future}T19:00"), 201)
    failed(ada.patch(f"/reservations/{booking['reference']}",
                     json={"starts_at_local": f"{BERLIN_SPRING}T02:30"}),
           422, "invalid_local_time")
    assert ok(ada.get(f"/reservations/{booking['reference']}"))["starts_at"] ==         booking["starts_at"], "a refused amendment changes nothing"


def test_a_booking_in_the_past_is_accepted(zones):
    """§4: a booking must not be rejected solely for starting in the past."""
    ada, _ = zones()
    body = ok(book_at(ada, "r_berlin", f"{BERLIN_SPRING}T19:00"), 201)
    assert body["status"] == "confirmed"


def test_the_cutoff_refuses_a_past_booking(zones):
    """...and the cutoff rule still applies to it (§4, §8)."""
    ada, _ = zones()
    body = ok(book_at(ada, "r_berlin", f"{BERLIN_SPRING}T19:00"), 201)
    failed(ada.post(f"/reservations/{body['reference']}/cancel"), 409, "cutoff_passed")
    failed(ada.patch(f"/reservations/{body['reference']}", json={"party_size": 2}),
           409, "cutoff_passed")


# ---- helpers -------------------------------------------------------------

def elapsed(body) -> dt.timedelta:
    return (dt.datetime.fromisoformat(body["ends_at"])
            - dt.datetime.fromisoformat(body["starts_at"]))


def add_half_hour(hhmm: str) -> str:
    hour, minute = (int(part) for part in hhmm.split(":"))
    minute += 30
    return f"{hour + minute // 60:02d}:{minute % 60:02d}"
