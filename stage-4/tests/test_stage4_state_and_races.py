"""Stage 4 under concurrency, and across an export.

Two questions this file answers: can two callers racing on one plan leave the
restaurant half-repaired, and does everything stage 4 adds survive a round trip and
an import from an earlier stage.
"""
from __future__ import annotations

import copy
import datetime as dt
from zoneinfo import ZoneInfo

import conftest as fx
from conftest import (ADA, BOB, Api, assert_no_double_booking, burst, failed, new_key,
                      no_5xx, ok, tally)
import pytest

BERLIN = ZoneInfo("Europe/Berlin")


def instant_at(date: str, at: str) -> str:
    hour, minute = (int(part) for part in at.split(":"))
    return dt.datetime.combine(dt.date.fromisoformat(date), dt.time(hour, minute),
                               tzinfo=BERLIN).isoformat()


@pytest.fixture
def repair(api, reset):
    """A managed restaurant with bookings to repair, and Ada signed in as manager."""
    def _make(*, sizes=(2, 4, 6), bookings=(), combinable=None, restaurants=None):
        date = fx.booking_date()
        tables = [{"id": f"t_{i + 1}", "label": str(i + 1), "capacity": size}
                  for i, size in enumerate(sizes)]
        house = {"id": "r_anker", "name": "Zum Anker", "timezone": "Europe/Berlin",
                 "slot_minutes": 30, "reservation_duration_minutes": 90,
                 "cancellation_cutoff_minutes": 120,
                 "opening_hours": fx.all_week(), "tables": tables,
                 "combinable": combinable or [], "manager_user_ids": ["u_ada"]}
        seeded = [{"id": f"res_{i}", "reference": reference, "user_id": "u_ada",
                   "restaurant_id": "r_anker", "table_ids": list(table_ids),
                   "starts_at_local": fx.local(date, at), "party_size": party}
                  for i, (reference, table_ids, at, party) in enumerate(bookings)]
        reset(fx.fixture(restaurants=[house] + list(restaurants or []),
                         reservations=seeded))

        class World:
            pass

        world = World()
        world.date = date
        world.ada = api().authenticated(ADA["email"], ADA["password"])
        world.bob = api().authenticated(BOB["email"], BOB["password"])
        world.anon = api()
        world.instant = lambda at: instant_at(date, at)
        world.revision = lambda rid="r_anker": ok(
            world.anon.get(f"/restaurants/{rid}"))["revision"]
        return world
    return _make


def preview(world, table_id="t_2", from_at="18:00", to_at="23:00", key=None):
    return world.ada.post("/restaurants/r_anker/replans", json={
        "table_id": table_id, "from": world.instant(from_at),
        "to": world.instant(to_at)}, key=new_key() if key is None else key)


# ---- races ---------------------------------------------------------------

def test_two_applications_of_one_plan_leave_one_repair(repair, pool):
    """Stage 4: concurrent applications must not leave partially moved bookings.

    Twenty callers apply the same plan at once with different keys. Exactly one may
    commit; the rest must be told the plan is already applied, and the bookings must
    read as one complete repair.
    """
    world = repair(sizes=(2, 4, 6, 6),
                   bookings=[("BOOK01", ["t_2"], "19:00", 4),
                             ("BOOK02", ["t_3"], "19:00", 5)])
    plan = ok(preview(world), 201)
    token = world.ada.token

    responses = burst(lambda i: Api(pool, token).post(
        f"/restaurants/r_anker/replans/{plan['plan_id']}/apply", json={},
        key=new_key()), 20)

    no_5xx(responses)
    counts = tally(responses)
    assert counts.get(201) == 1, counts
    assert counts.get(409) == 19, counts
    for resp in responses:
        if resp.status_code == 409:
            assert resp.json()["error"]["code"] == "plan_already_applied", resp.text

    # Exactly the plan, applied once.
    for assignment in plan["assignments"]:
        booking = ok(world.ada.get(f"/reservations/{assignment['reference']}"))
        assert booking["table_ids"] == assignment["table_ids"]
        assert booking["revision"] == (2 if assignment["changed"] else 1), \
            "one revision for a moved booking, none for an unmoved one"
        entries = ok(world.ada.get(
            f"/reservations/{assignment['reference']}/history"))["entries"]
        reassigned = [entry for entry in entries if entry["event"] == "reassigned"]
        assert len(reassigned) == (1 if assignment["changed"] else 0)
    assert_no_double_booking([world.ada])


def test_the_same_key_applying_twice_is_one_repair(repair, pool):
    """The ordinary idempotent race: one key, many callers, one effect."""
    world = repair(sizes=(2, 4, 6), bookings=[("BOOK01", ["t_2"], "19:00", 4)])
    plan = ok(preview(world), 201)
    token = world.ada.token
    key = new_key()

    responses = burst(lambda i: Api(pool, token).post(
        f"/restaurants/r_anker/replans/{plan['plan_id']}/apply", json={}, key=key), 20)
    no_5xx(responses)
    counts = tally(responses)
    assert counts.get(201) == 1, counts
    assert counts.get(200) == 19, counts
    bodies = {resp.text for resp in responses}
    assert len(bodies) == 1, "every answer is the same response"
    assert ok(world.ada.get("/reservations/BOOK01"))["revision"] == 2


def test_a_booking_between_preview_and_apply_makes_the_plan_stale(repair):
    world = repair(sizes=(2, 4, 6), bookings=[("BOOK01", ["t_2"], "19:00", 4)])
    plan = ok(preview(world), 201)
    ok(world.bob.book(table_id="t_1", party_size=2,
                      starts_at_local=fx.local(world.date, "21:00")), 201)
    failed(world.ada.post(f"/restaurants/r_anker/replans/{plan['plan_id']}/apply",
                          json={}, key=new_key()), 409, "stale_plan")
    assert ok(world.ada.get("/reservations/BOOK01"))["table_ids"] == ["t_2"], \
        "a refused application changes nothing"


def test_a_change_at_another_restaurant_does_not_invalidate_a_plan(repair):
    """Stage 4 says so explicitly: the revision is per restaurant."""
    other = {"id": "r_other", "name": "Other", "timezone": "Europe/Berlin",
             "slot_minutes": 30, "reservation_duration_minutes": 90,
             "cancellation_cutoff_minutes": 120, "opening_hours": fx.all_week(),
             "tables": [{"id": "t_1", "label": "1", "capacity": 4},
                        {"id": "t_2", "label": "2", "capacity": 4}],
             "manager_user_ids": ["u_ada"]}
    world = repair(sizes=(2, 4, 6), bookings=[("BOOK01", ["t_2"], "19:00", 4)],
                   restaurants=[other])
    plan = ok(preview(world), 201)

    # A booking, a policy and a whole closure elsewhere.
    ok(world.bob.book(restaurant_id="r_other", table_id="t_1", party_size=4,
                      starts_at_local=fx.local(world.date, "19:00")), 201)
    ok(world.ada.post("/restaurants/r_other/policies",
                      json=fx.policy(world.date, capacities={"t_1": 4, "t_2": 4}),
                      key=new_key()), 201)
    elsewhere = ok(world.ada.post("/restaurants/r_other/replans", json={
        "table_id": "t_1", "from": world.instant("18:00"),
        "to": world.instant("23:00")}, key=new_key()), 201)
    assert ok(world.ada.post(
        f"/restaurants/r_other/replans/{elsewhere['plan_id']}/apply", json={},
        key=new_key()), 201)

    assert world.revision("r_other") > 0
    ok(world.ada.post(f"/restaurants/r_anker/replans/{plan['plan_id']}/apply",
                      json={}, key=new_key()), 201)


def test_a_plan_from_another_restaurant_is_404(repair):
    other = {"id": "r_other", "name": "Other", "timezone": "Europe/Berlin",
             "slot_minutes": 30, "reservation_duration_minutes": 90,
             "cancellation_cutoff_minutes": 120, "opening_hours": fx.all_week(),
             "tables": [{"id": "t_1", "label": "1", "capacity": 4}],
             "manager_user_ids": ["u_ada"]}
    world = repair(sizes=(2, 4, 6), restaurants=[other])
    plan = ok(preview(world), 201)
    failed(world.ada.post(f"/restaurants/r_other/replans/{plan['plan_id']}/apply",
                          json={}, key=new_key()), 404, "not_found")


def test_bookings_racing_a_closure_never_land_on_a_closed_table(repair, pool):
    """Fifty diners try to take the table while a repair closes it. Whatever the
    interleaving, no confirmed booking may end up on a closed table."""
    users = [dict(ADA, id=f"u_{i}", email=f"diner{i}@example.com") for i in range(50)]
    date = fx.booking_date()
    tables = [{"id": f"t_{i + 1}", "label": str(i + 1), "capacity": 4} for i in range(3)]
    house = {"id": "r_anker", "name": "Zum Anker", "timezone": "Europe/Berlin",
             "slot_minutes": 30, "reservation_duration_minutes": 90,
             "cancellation_cutoff_minutes": 120, "opening_hours": fx.all_week(),
             "tables": tables, "manager_user_ids": ["u_0"]}
    resp = Api(pool).post("/_test/reset", json=fx.fixture(
        users=users, restaurants=[house]), timeout=fx.CONTROL_TIMEOUT)
    assert resp.status_code == 204, fx.described(resp)
    clients = [Api(pool).authenticated(user["email"], user["password"])
               for user in users]
    manager = clients[0]
    plan = ok(manager.post("/restaurants/r_anker/replans", json={
        "table_id": "t_2", "from": instant_at(date, "18:00"),
        "to": instant_at(date, "23:00")}, key=new_key()), 201)

    def act(index):
        if index == 0:
            return manager.post(
                f"/restaurants/r_anker/replans/{plan['plan_id']}/apply", json={},
                key=new_key())
        return clients[index].book(table_id="t_2", party_size=4,
                                   starts_at_local=fx.local(date, "19:00"))

    responses = burst(act, 50)
    no_5xx(responses)
    for client in clients:
        for booking in ok(client.get("/reservations"))["reservations"]:
            if booking["status"] != "confirmed":
                continue
            if booking["table_ids"] == ["t_2"] and \
                    booking["starts_at_local"].endswith("19:00"):
                # Only legal if the closure never took effect.
                assert plan and responses[0].status_code != 201, \
                    "a booking survived on a table the applied closure covers"
    assert_no_double_booking(clients)


# ---- across an export ----------------------------------------------------

def test_plans_closures_and_receipts_survive_a_round_trip(repair):
    world = repair(sizes=(2, 4, 6), bookings=[("BOOK01", ["t_2"], "19:00", 4)])
    preview_key, apply_key = new_key(), new_key()
    plan = ok(preview(world, key=preview_key), 201)
    applied = ok(world.ada.post(
        f"/restaurants/r_anker/replans/{plan['plan_id']}/apply", json={},
        key=apply_key), 201)
    revision_before = world.revision()

    snapshot = ok(world.anon.get("/_test/export", timeout=fx.CONTROL_TIMEOUT))
    assert "plans" in snapshot["state"] and "closures" in snapshot["state"]
    world.anon.post("/_test/reset", json=fx.fixture(), timeout=fx.CONTROL_TIMEOUT)
    resp = world.anon.post("/_test/import", json=snapshot, timeout=fx.CONTROL_TIMEOUT)
    assert resp.status_code == 204, fx.described(resp)

    # The repair is still in place, including its history and the revision.
    booking = ok(world.ada.get("/reservations/BOOK01"))
    assert booking["table_ids"] == ["t_3"] and booking["revision"] == 2
    entries = ok(world.ada.get("/reservations/BOOK01/history"))["entries"]
    assert entries[-1]["event"] == "reassigned"
    assert entries[-1]["plan_id"] == plan["plan_id"]
    assert world.revision() == revision_before

    # Both receipts still replay the original responses.
    assert ok(world.ada.post("/restaurants/r_anker/replans", json={
        "table_id": "t_2", "from": world.instant("18:00"),
        "to": world.instant("23:00")}, key=preview_key), 200) == plan
    assert ok(world.ada.post(
        f"/restaurants/r_anker/replans/{plan['plan_id']}/apply", json={},
        key=apply_key), 200) == applied

    # And the closure still bites.
    failed(world.bob.book(table_id="t_2", party_size=4,
                          starts_at_local=fx.local(world.date, "19:00")),
           409, "table_unavailable")
    assert "t_2" not in ok(world.anon.get("/availability", params={
        "restaurant_id": "r_anker", "date": world.date,
        "party_size": 4}))["slots"][0]["available_table_ids"]


def test_a_plan_cannot_be_applied_twice_across_a_round_trip(repair):
    world = repair(sizes=(2, 4, 6), bookings=[("BOOK01", ["t_2"], "19:00", 4)])
    plan = ok(preview(world), 201)
    ok(world.ada.post(f"/restaurants/r_anker/replans/{plan['plan_id']}/apply",
                      json={}, key=new_key()), 201)
    snapshot = ok(world.anon.get("/_test/export", timeout=fx.CONTROL_TIMEOUT))
    world.anon.post("/_test/import", json=snapshot, timeout=fx.CONTROL_TIMEOUT)
    failed(world.ada.post(f"/restaurants/r_anker/replans/{plan['plan_id']}/apply",
                          json={}, key=new_key()), 409, "plan_already_applied")


def test_an_unapplied_plan_survives_and_can_still_be_applied(repair):
    world = repair(sizes=(2, 4, 6), bookings=[("BOOK01", ["t_2"], "19:00", 4)])
    plan = ok(preview(world), 201)
    snapshot = ok(world.anon.get("/_test/export", timeout=fx.CONTROL_TIMEOUT))
    world.anon.post("/_test/import", json=snapshot, timeout=fx.CONTROL_TIMEOUT)
    ok(world.ada.post(f"/restaurants/r_anker/replans/{plan['plan_id']}/apply",
                      json={}, key=new_key()), 201)
    assert ok(world.ada.get("/reservations/BOOK01"))["table_ids"] == ["t_3"]


def test_a_crafted_state_with_an_impossible_closure_is_refused(repair):
    world = repair(sizes=(2, 4, 6))
    snapshot = ok(world.anon.get("/_test/export", timeout=fx.CONTROL_TIMEOUT))
    for bad in ({"restaurant_id": "r_nope", "table_id": "t_2",
                 "from": world.instant("18:00"), "to": world.instant("23:00")},
                {"restaurant_id": "r_anker", "table_id": "t_nope",
                 "from": world.instant("18:00"), "to": world.instant("23:00")},
                {"restaurant_id": "r_anker", "table_id": "t_2",
                 "from": world.instant("23:00"), "to": world.instant("18:00")},
                {"restaurant_id": "r_anker", "table_id": "t_2",
                 "from": "nonsense", "to": world.instant("23:00")}):
        crafted = copy.deepcopy(snapshot)
        crafted["state"]["closures"] = [bad]
        resp = world.anon.post("/_test/import", json=crafted,
                               timeout=fx.CONTROL_TIMEOUT)
        failed(resp, 422, "validation_failed")


# ---- an imported agreement ----------------------------------------------

def test_an_imported_series_keeps_its_exceptions_and_cancellations(api, reset):
    """Stage 4: these operations must support imported series, including moved and
    cancelled occurrences."""
    reset(fx.fixture(restaurants=[fx.managed("u_ada")]))
    ada = api().authenticated(ADA["email"], ADA["password"])
    anon = api()
    date = fx.booking_date()
    anchor = ok(ada.book(table_id="t_2", starts_at_local=fx.local(date, "19:00")), 201)
    series = ok(ada.post("/series", json={"anchor_reference": anchor["reference"],
                                          "count": 3, "interval_weeks": 1},
                         key=new_key()), 201)
    sid = series["series_id"]
    moved = series["occurrences"][1]["reference"]
    cancelled = series["occurrences"][2]["reference"]
    ok(ada.patch(f"/reservations/{moved}", json={"table_id": "t_3"}))
    ok(ada.post(f"/reservations/{cancelled}/cancel"))
    before = ok(ada.get(f"/series/{sid}"))

    snapshot = ok(anon.get("/_test/export", timeout=fx.CONTROL_TIMEOUT))
    anon.post("/_test/reset", json=fx.fixture(), timeout=fx.CONTROL_TIMEOUT)
    resp = anon.post("/_test/import", json=snapshot, timeout=fx.CONTROL_TIMEOUT)
    assert resp.status_code == 204, fx.described(resp)

    after = ok(ada.get(f"/series/{sid}"))
    assert after == before, "the agreement comes back exactly as it was"
    assert [occurrence["exception"] for occurrence in after["occurrences"]] == \
        [False, True, False]
    assert after["occurrences"][2]["reservation"]["status"] == "cancelled"

    # And an amendment on the imported agreement behaves: the exception and the
    # cancellation are skipped, the anchor moves.
    result = ok(ada.post(f"/series/{sid}/amend", json={
        "expected_revision": after["revision"], "from_index": 0,
        "local_time": "20:00"}, key=new_key()), 201)
    times = [occurrence["reservation"]["starts_at_local"].split("T")[1]
             for occurrence in result["occurrences"]]
    assert times == ["20:00", "19:00", "19:00"]


def test_a_repair_may_move_an_imported_series_occurrence(api, reset):
    """A seating repair preserves an occurrence's exception flag, scheduled date,
    identity and accepted terms, and moves the agreement's revision once."""
    reset(fx.fixture(restaurants=[fx.managed("u_ada")]))
    ada = api().authenticated(ADA["email"], ADA["password"])
    anon = api()
    date = fx.booking_date()
    anchor = ok(ada.book(table_id="t_2", starts_at_local=fx.local(date, "19:00")), 201)
    series = ok(ada.post("/series", json={"anchor_reference": anchor["reference"],
                                          "count": 2, "interval_weeks": 1},
                         key=new_key()), 201)
    sid = series["series_id"]
    snapshot = ok(anon.get("/_test/export", timeout=fx.CONTROL_TIMEOUT))
    anon.post("/_test/import", json=snapshot, timeout=fx.CONTROL_TIMEOUT)

    before = ok(ada.get(f"/series/{sid}"))
    plan = ok(ada.post("/restaurants/r_anker/replans", json={
        "table_id": "t_2", "from": instant_at(date, "18:00"),
        "to": instant_at(date, "23:00")}, key=new_key()), 201)
    assert [assignment["reference"] for assignment in plan["assignments"]] == \
        [anchor["reference"]], "only the first occurrence is inside the window"
    ok(ada.post(f"/restaurants/r_anker/replans/{plan['plan_id']}/apply", json={},
                key=new_key()), 201)

    after = ok(ada.get(f"/series/{sid}"))
    assert after["revision"] == before["revision"] + 1, \
        "one revision for the agreement whose member moved"
    first = after["occurrences"][0]["reservation"]
    assert first["table_ids"] == ["t_3"]
    assert first["starts_at_local"] == anchor["starts_at_local"], "the date is kept"
    assert first["accepted_terms"] == anchor["accepted_terms"], "and the terms"
    assert first["reservation_id"] == anchor["reservation_id"], "and the identity"
    assert after["occurrences"][0]["exception"] is False, \
        "a repair is not a diner's exception"
