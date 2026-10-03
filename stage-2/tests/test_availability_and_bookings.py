"""§8: availability, the booking lifecycle, and the reads around them."""
from __future__ import annotations

import datetime as dt
import re

import conftest as fx
from conftest import ADA, failed, new_key, ok
import pytest

REFERENCE = re.compile(r"^[A-Z0-9]{6,12}$")


def expected_slots(opens="18:00", closes="23:00", slot_minutes=30, duration=90):
    """Every step from `opens` while slot + duration still ends by `closes`."""
    def minutes(hhmm):
        hours, mins = (int(part) for part in hhmm.split(":"))
        return hours * 60 + mins

    out, at, end = [], minutes(opens), minutes(closes)
    while at + duration <= end:
        out.append(f"{at // 60:02d}:{at % 60:02d}")
        at += slot_minutes
    return out


@pytest.mark.parametrize("opens,closes,slot_minutes,duration", [
    ("18:00", "23:00", 30, 90),
    ("18:00", "23:00", 60, 120),
    ("18:00", "20:00", 30, 45),
    ("11:00", "14:00", 15, 60),
    ("00:00", "23:30", 30, 90),
    ("18:00", "19:00", 30, 90),   # no slot fits at all
])
def test_the_slot_grid_follows_the_stated_rule(api, reset, opens, closes,
                                               slot_minutes, duration):
    rest = fx.restaurant(opening_hours=fx.all_week(opens, closes),
                         slot_minutes=slot_minutes,
                         reservation_duration_minutes=duration)
    reset(fx.fixture(restaurants=[rest]))
    anon = api()
    date = fx.booking_date()
    body = ok(anon.get("/availability", params={
        "restaurant_id": "r_anker", "date": date, "party_size": 2}))
    assert body["restaurant_id"] == "r_anker"
    assert body["date"] == date
    assert body["timezone"] == "Europe/Berlin"
    times = [slot["starts_at_local"].split("T")[1] for slot in body["slots"]]
    assert times == expected_slots(opens, closes, slot_minutes, duration)
    for slot in body["slots"]:
        assert slot["starts_at_local"].startswith(f"{date}T")
        assert slot["starts_at"].startswith(f"{date}T{slot['starts_at_local'][-5:]}:00")


def test_opening_hours_are_per_weekday(api, reset):
    """A different closing time on the next day means a different last slot."""
    date = fx.booking_date()
    today = dt.date.fromisoformat(date)
    first, second = (fx.WEEKDAYS[today.weekday()],
                     fx.WEEKDAYS[(today + dt.timedelta(days=1)).weekday()])
    rest = fx.restaurant(opening_hours=[
        {"weekday": first, "opens": "18:00", "closes": "23:00"},
        {"weekday": second, "opens": "18:00", "closes": "23:30"}])
    reset(fx.fixture(restaurants=[rest]))
    anon = api()

    def times(on):
        return [s["starts_at_local"].split("T")[1] for s in ok(anon.get(
            "/availability", params={"restaurant_id": "r_anker", "date": on,
                                     "party_size": 2}))["slots"]]

    assert times(date)[-1] == "21:30"
    assert times((today + dt.timedelta(days=1)).isoformat())[-1] == "22:00"
    assert times((today + dt.timedelta(days=2)).isoformat()) == [], \
        "a weekday with no entry is closed"


def test_available_tables_are_filtered_by_capacity_in_fixture_order(api, reset):
    """Fixture order, not sorted order: the fixture lists t_3 first here."""
    tables = [{"id": "t_3", "label": "3", "capacity": 6},
              {"id": "t_1", "label": "1", "capacity": 2},
              {"id": "t_2", "label": "2", "capacity": 4}]
    reset(fx.fixture(restaurants=[fx.restaurant(tables=tables)]))
    anon = api()
    date = fx.booking_date()

    def ids(party_size):
        return ok(anon.get("/availability", params={
            "restaurant_id": "r_anker", "date": date,
            "party_size": party_size}))["slots"][0]["available_table_ids"]

    assert ids(2) == ["t_3", "t_1", "t_2"]
    assert ids(4) == ["t_3", "t_2"]
    assert ids(6) == ["t_3"]
    assert ids(7) == []


def test_a_booking_removes_itself_from_every_overlapping_slot(api, reset):
    """A 120-minute booking on a 30-minute grid shadows four slots."""
    rest = fx.restaurant(reservation_duration_minutes=120)
    reset(fx.fixture(restaurants=[rest]))
    ada = api().authenticated(ADA["email"], ADA["password"])
    anon = api()
    date = fx.booking_date()
    ok(ada.book(table_id="t_2", starts_at_local=fx.local(date, "19:00")), 201)

    free = {s["starts_at_local"].split("T")[1]: s["available_table_ids"]
            for s in ok(anon.get("/availability", params={
                "restaurant_id": "r_anker", "date": date,
                "party_size": 4}))["slots"]}
    for shadowed in ("17:30", "18:00", "18:30", "19:00", "19:30", "20:30"):
        if shadowed in free:
            assert "t_2" not in free[shadowed], shadowed
    assert "t_2" in free["21:00"], "21:00 is exactly when the booking ends"


def test_a_slot_with_no_free_table_still_appears(world):
    for table in ("t_2", "t_3"):
        ok(world.ada.book(table_id=table, starts_at_local=world.at("19:00")), 201)
    free = world.slots(world.anon, party_size=4)
    assert free["19:00"] == []
    assert "19:00" in free


def test_two_restaurants_with_the_same_table_ids_are_independent(api, reset):
    """Table ids are the restaurant's own: booking t_2 at one does not touch the
    other restaurant's t_2."""
    reset(fx.fixture(restaurants=[fx.restaurant("r_one"), fx.restaurant("r_two")]))
    ada = api().authenticated(ADA["email"], ADA["password"])
    anon = api()
    date = fx.booking_date()
    ok(ada.book(restaurant_id="r_one", table_id="t_2",
                starts_at_local=fx.local(date, "19:00")), 201)
    ok(ada.book(restaurant_id="r_two", table_id="t_2",
                starts_at_local=fx.local(date, "19:00")), 201)
    for rid, expected in (("r_one", False), ("r_two", False)):
        free = {s["starts_at_local"].split("T")[1]: s["available_table_ids"]
                for s in ok(anon.get("/availability", params={
                    "restaurant_id": rid, "date": date,
                    "party_size": 4}))["slots"]}
        assert ("t_2" in free["19:00"]) is expected


def test_cancelling_frees_the_table_immediately(world):
    booking = ok(world.ada.book(table_id="t_2",
                                starts_at_local=world.at("19:00")), 201)
    assert "t_2" not in world.slots(world.anon)["19:00"]
    body = ok(world.ada.post(f"/reservations/{booking['reference']}/cancel"))
    assert body["status"] == "cancelled"
    assert body["reference"] == booking["reference"]
    assert body["reservation_id"] == booking["reservation_id"]
    assert body["created_at"] == booking["created_at"]
    assert "t_2" in world.slots(world.anon)["19:00"]
    ok(world.bob.book(table_id="t_2", starts_at_local=world.at("19:00")), 201)


def test_cancelling_twice_is_not_an_error(world):
    booking = ok(world.ada.book(starts_at_local=world.at("19:00")), 201)
    first = ok(world.ada.post(f"/reservations/{booking['reference']}/cancel"))
    second = ok(world.ada.post(f"/reservations/{booking['reference']}/cancel"))
    assert first == second and second["status"] == "cancelled"


def test_an_amendment_releases_the_old_slot_and_takes_the_new_one(world):
    booking = ok(world.ada.book(table_id="t_2",
                                starts_at_local=world.at("19:00")), 201)
    moved = ok(world.ada.patch(f"/reservations/{booking['reference']}", json={
        "table_id": "t_3", "starts_at_local": world.at("20:30"), "party_size": 5}))
    assert moved["table_id"] == "t_3" and moved["party_size"] == 5
    assert moved["starts_at_local"] == world.at("20:30")
    assert moved["ends_at"].startswith(f"{world.date}T22:00:00")
    assert moved["reference"] == booking["reference"]
    assert moved["reservation_id"] == booking["reservation_id"]
    assert moved["created_at"] == booking["created_at"]
    free = world.slots(world.anon, party_size=4)
    assert "t_2" in free["19:00"] and "t_3" in free["19:00"]
    assert "t_3" not in free["20:30"]


def test_an_amendment_that_overlaps_its_own_old_interval_is_allowed(world):
    booking = ok(world.ada.book(table_id="t_2",
                                starts_at_local=world.at("19:00")), 201)
    moved = ok(world.ada.patch(f"/reservations/{booking['reference']}",
                               json={"starts_at_local": world.at("19:30")}))
    assert moved["starts_at_local"] == world.at("19:30")


def test_an_amendment_to_the_very_same_values_is_allowed(world):
    booking = ok(world.ada.book(table_id="t_2",
                                starts_at_local=world.at("19:00")), 201)
    same = ok(world.ada.patch(f"/reservations/{booking['reference']}", json={
        "table_id": "t_2", "starts_at_local": world.at("19:00"), "party_size": 4}))
    assert same == booking


def test_an_empty_amendment_changes_nothing(world):
    booking = ok(world.ada.book(starts_at_local=world.at("19:00")), 201)
    assert ok(world.ada.patch(f"/reservations/{booking['reference']}", json={})) == \
        booking


def test_a_failed_amendment_leaves_the_booking_untouched(world):
    booking = ok(world.ada.book(table_id="t_2",
                                starts_at_local=world.at("19:00")), 201)
    ok(world.bob.book(table_id="t_3", starts_at_local=world.at("19:00")), 201)
    for change, status, code in (
        ({"table_id": "t_3"}, 409, "table_unavailable"),
        ({"table_id": "t_1"}, 422, "party_exceeds_capacity"),
        ({"party_size": 7}, 422, "party_exceeds_capacity"),
        ({"starts_at_local": world.at("19:15")}, 422, "not_on_slot_grid"),
        ({"starts_at_local": world.at("22:00")}, 422, "outside_opening_hours"),
        ({"table_id": "t_nope"}, 404, "not_found"),
        ({"party_size": 0}, 422, "validation_failed"),
    ):
        failed(world.ada.patch(f"/reservations/{booking['reference']}", json=change),
               status, code)
        assert ok(world.ada.get(f"/reservations/{booking['reference']}")) == booking
    assert "t_2" not in world.slots(world.anon)["19:00"], \
        "and its occupancy is unchanged"


def test_an_amendment_may_take_a_freed_table(world):
    hers = ok(world.bob.book(table_id="t_3", starts_at_local=world.at("19:00")), 201)
    mine = ok(world.ada.book(table_id="t_2", starts_at_local=world.at("19:00")), 201)
    failed(world.ada.patch(f"/reservations/{mine['reference']}",
                           json={"table_id": "t_3"}), 409, "table_unavailable")
    ok(world.bob.post(f"/reservations/{hers['reference']}/cancel"))
    assert ok(world.ada.patch(f"/reservations/{mine['reference']}",
                              json={"table_id": "t_3"}))["table_id"] == "t_3"


def test_a_cancelled_booking_cannot_be_amended(world):
    booking = ok(world.ada.book(starts_at_local=world.at("19:00")), 201)
    ok(world.ada.post(f"/reservations/{booking['reference']}/cancel"))
    failed(world.ada.patch(f"/reservations/{booking['reference']}",
                           json={"party_size": 2}), 409, "reservation_cancelled")


def test_only_the_owner_sees_or_touches_a_booking(world):
    mine = ok(world.ada.book(starts_at_local=world.at("19:00")), 201)
    reference = mine["reference"]
    failed(world.bob.get(f"/reservations/{reference}"), 404, "not_found")
    failed(world.bob.patch(f"/reservations/{reference}", json={"party_size": 2}),
           404, "not_found")
    failed(world.bob.post(f"/reservations/{reference}/cancel"), 404, "not_found")
    failed(world.bob.moves([{"reference": reference, "party_size": 2}]),
           404, "not_found")
    failed(world.ada.get("/reservations/ZZZZZZ"), 404, "not_found")


def test_the_list_is_the_callers_own_newest_first_and_includes_cancellations(world):
    made = [ok(world.ada.book(table_id="t_2", starts_at_local=world.at(at)), 201)
            for at in ("18:00", "21:00", "19:30")]
    ok(world.bob.book(table_id="t_3", starts_at_local=world.at("19:00")), 201)
    ok(world.ada.post(f"/reservations/{made[0]['reference']}/cancel"))

    listed = ok(world.ada.get("/reservations"))["reservations"]
    assert [r["starts_at_local"] for r in listed] == [
        world.at("21:00"), world.at("19:30"), world.at("18:00")]
    assert {r["status"] for r in listed} == {"confirmed", "cancelled"}
    assert all(r["reference"] != "" for r in listed)
    assert len(listed) == 3, "Bob's booking is not Ada's business"


def test_an_empty_list_is_an_empty_list(world):
    assert ok(world.ada.get("/reservations")) == {"reservations": []}


def test_references_are_unique_and_well_formed(world):
    references = set()
    for i in range(12):
        table = ("t_2", "t_3")[i % 2]
        at = ("18:00", "19:30", "21:00")[i % 3]
        resp = world.ada.book(table_id=table, starts_at_local=world.at(at),
                              party_size=2)
        if resp.status_code != 201:
            continue
        reference = resp.json()["reference"]
        assert REFERENCE.match(reference), reference
        references.add(reference)
    assert len(references) == 6, references


def test_the_restaurant_detail_echoes_the_fixture(world):
    body = ok(world.anon.get(f"/restaurants/{world.rid}"))
    for field in ("id", "name", "timezone", "slot_minutes",
                  "reservation_duration_minutes", "cancellation_cutoff_minutes"):
        assert body[field] == world.restaurant[field], field
    assert body["tables"] == world.restaurant["tables"]
    assert body["opening_hours"] == world.restaurant["opening_hours"]


def test_the_restaurant_list_carries_the_three_public_fields(api, reset):
    reset(fx.fixture(restaurants=[fx.restaurant("r_one", name="One"),
                                  fx.restaurant("r_two", name="Two",
                                                timezone="America/New_York")]))
    body = ok(api().get("/restaurants"))
    assert body["restaurants"] == [
        {"id": "r_one", "name": "One", "timezone": "Europe/Berlin"},
        {"id": "r_two", "name": "Two", "timezone": "America/New_York"},
    ]


def test_an_unknown_restaurant_detail_is_404(world):
    failed(world.anon.get("/restaurants/r_nope"), 404, "not_found")


def test_the_create_response_carries_every_documented_field(world):
    body = ok(world.ada.book(table_id="t_2", starts_at_local=world.at("19:00"),
                             party_size=4), 201)
    assert set(body) == {"reservation_id", "reference", "restaurant_id", "table_id",
                         "party_size", "status", "starts_at_local", "starts_at",
                         "ends_at", "created_at"}
    assert body["status"] == "confirmed"
    assert len(body["reservation_id"]) <= 64
    assert body["starts_at"] == f"{world.date}T19:00:00+02:00" or \
        body["starts_at"] == f"{world.date}T19:00:00+01:00"
    assert body["ends_at"].startswith(f"{world.date}T20:30:00")
    for field in ("starts_at", "ends_at", "created_at"):
        assert re.search(r"[+-]\d{2}:\d{2}$", body[field]), (field, body[field])
    assert ok(world.ada.get(f"/reservations/{body['reference']}")) == body


def test_a_seeded_booking_behaves_exactly_like_one_made_through_the_api(reset, api):
    date = fx.booking_date()
    seeded = {"id": "res_seed", "reference": "SEED01", "user_id": "u_ada",
              "restaurant_id": "r_anker", "table_id": "t_2",
              "starts_at_local": fx.local(date, "19:00"), "party_size": 4}
    reset(fx.fixture(reservations=[seeded]))
    ada = api().authenticated(ADA["email"], ADA["password"])
    bob = api().authenticated("bob@example.com", "correct horse")

    body = ok(ada.get("/reservations/SEED01"))
    assert body["reservation_id"] == "res_seed" and body["status"] == "confirmed"
    assert body["starts_at"].startswith(f"{date}T19:00:00")
    assert body["ends_at"].startswith(f"{date}T20:30:00")
    failed(bob.book(table_id="t_2", starts_at_local=fx.local(date, "19:00")),
           409, "table_unavailable")
    moved = ok(ada.moves([{"reference": "SEED01", "table_id": "t_3"}]), 201)
    assert moved["reservations"][0]["table_id"] == "t_3"
    ok(bob.book(table_id="t_2", starts_at_local=fx.local(date, "19:00")), 201)


def test_the_cutoff_is_measured_from_now_to_the_current_start(api, reset):
    """Wide enough that a booking a week out is inside it, so both cancel and amend
    are refused -- and the refusal names the cutoff rather than anything else."""
    locked = fx.restaurant(cancellation_cutoff_minutes=60 * 24 * 3650)
    reset(fx.fixture(restaurants=[locked]))
    ada = api().authenticated(ADA["email"], ADA["password"])
    date = fx.booking_date()
    booking = ok(ada.book(table_id="t_2", starts_at_local=fx.local(date, "19:00")), 201)
    failed(ada.post(f"/reservations/{booking['reference']}/cancel"),
           409, "cutoff_passed")
    failed(ada.patch(f"/reservations/{booking['reference']}", json={"party_size": 2}),
           409, "cutoff_passed")
    failed(ada.moves([{"reference": booking["reference"], "party_size": 2}]),
           409, "cutoff_passed")
    assert ok(ada.get(f"/reservations/{booking['reference']}")) == booking


def test_a_zero_cutoff_still_refuses_a_booking_that_has_started(api, reset):
    """§8: within the cutoff of the start, *or later*."""
    open_policy = fx.restaurant(cancellation_cutoff_minutes=0)
    date = (dt.date.today() - dt.timedelta(days=3)).isoformat()
    seeded = {"id": "res_past", "reference": "PAST01", "user_id": "u_ada",
              "restaurant_id": "r_anker", "table_id": "t_2",
              "starts_at_local": fx.local(date, "19:00"), "party_size": 4}
    reset(fx.fixture(restaurants=[open_policy], reservations=[seeded]))
    ada = api().authenticated(ADA["email"], ADA["password"])
    failed(ada.post("/reservations/PAST01/cancel"), 409, "cutoff_passed")
    failed(ada.patch("/reservations/PAST01", json={"party_size": 2}),
           409, "cutoff_passed")


def test_a_booking_just_outside_the_cutoff_is_still_changeable(api, reset):
    rest = fx.restaurant(cancellation_cutoff_minutes=60,
                         opening_hours=fx.all_week("00:00", "23:30"))
    reset(fx.fixture(restaurants=[rest]))
    ada = api().authenticated(ADA["email"], ADA["password"])
    date = fx.booking_date()
    booking = ok(ada.book(table_id="t_2", starts_at_local=fx.local(date, "19:00")), 201)
    ok(ada.patch(f"/reservations/{booking['reference']}", json={"party_size": 2}))
    ok(ada.post(f"/reservations/{booking['reference']}/cancel"))


def test_an_idempotency_key_is_not_required_to_amend_or_cancel(world):
    booking = ok(world.ada.book(starts_at_local=world.at("19:00")), 201)
    ok(world.ada.patch(f"/reservations/{booking['reference']}",
                       json={"party_size": 3}))
    ok(world.ada.post(f"/reservations/{booking['reference']}/cancel"))


def test_unknown_body_fields_are_ignored_everywhere(world):
    booking = ok(world.ada.book(table_id="t_2", starts_at_local=world.at("19:00"),
                                souvenir="yes", status="cancelled",
                                reference="MINE01"), 201)
    assert booking["status"] == "confirmed", "a client cannot dictate the status"
    assert booking["reference"] != "MINE01", "nor the reference"
    amended = ok(world.ada.patch(f"/reservations/{booking['reference']}", json={
        "party_size": 2, "status": "cancelled", "reference": "MINE01",
        "reservation_id": "res_mine", "created_at": "2000-01-01T00:00:00+00:00"}))
    assert amended["status"] == "confirmed"
    assert amended["reference"] == booking["reference"]
    assert amended["reservation_id"] == booking["reservation_id"]
    assert amended["created_at"] == booking["created_at"]


def test_a_booking_cannot_be_moved_to_another_restaurant(api, reset):
    """§8 lists table, time and party size as the amendable fields."""
    reset(fx.fixture(restaurants=[fx.restaurant("r_one"), fx.restaurant("r_two")]))
    ada = api().authenticated(ADA["email"], ADA["password"])
    date = fx.booking_date()
    booking = ok(ada.book(restaurant_id="r_one", table_id="t_2",
                          starts_at_local=fx.local(date, "19:00")), 201)
    amended = ok(ada.patch(f"/reservations/{booking['reference']}",
                           json={"restaurant_id": "r_two"}))
    assert amended["restaurant_id"] == "r_one"
