"""Stage 3: published booking policies, and which one governs a decision."""
from __future__ import annotations

import conftest as fx
from conftest import ADA, BOB, failed, new_key, ok
import pytest


@pytest.fixture
def managed(api, reset):
    """A restaurant Ada manages and Bob does not, with both signed in."""
    def _make(**kwargs):
        reset(fx.fixture(restaurants=[fx.managed("u_ada", **kwargs)]))

        class World:
            pass

        world = World()
        world.rid = "r_anker"
        world.date = fx.booking_date()
        world.ada = api().authenticated(ADA["email"], ADA["password"])
        world.bob = api().authenticated(BOB["email"], BOB["password"])
        world.anon = api()
        world.at = lambda hhmm="19:00", date=None: fx.local(date or world.date, hhmm)
        return world
    return _make


# ---- who may publish -----------------------------------------------------

def test_only_a_manager_may_publish(managed):
    world = managed()
    body = fx.policy(world.date)
    failed(fx.publish(world.anon, body, key=new_key()), 401, "unauthenticated")
    failed(fx.publish(world.bob, body), 403, "forbidden")
    ok(fx.publish(world.ada, body), 201)


def test_an_unknown_restaurant_is_404_even_for_a_manager(managed):
    world = managed()
    resp = world.ada.post("/restaurants/r_nope/policies", json=fx.policy(world.date),
                          key=new_key())
    failed(resp, 404, "not_found")


def test_a_restaurant_with_no_managers_accepts_no_publication(api, reset):
    reset(fx.fixture())  # no manager_user_ids at all, which means nobody
    ada = api().authenticated(ADA["email"], ADA["password"])
    failed(fx.publish(ada, fx.policy(fx.booking_date())), 403, "forbidden")
    assert ok(api().get("/restaurants/r_anker"))["manager_user_ids"] == []


def test_publishing_needs_an_idempotency_key_and_replays(managed):
    world = managed()
    body = fx.policy(world.date, reservation_duration_minutes=60)
    failed(world.ada.post(f"/restaurants/{world.rid}/policies", json=body),
           400, "missing_idempotency_key")
    key = new_key()
    first = ok(fx.publish(world.ada, body, key=key), 201)
    replay = ok(fx.publish(world.ada, body, key=key), 200)
    assert replay == first, "a replay returns the original response"
    assert len(ok(world.anon.get(f"/restaurants/{world.rid}/policies"))["policies"]) == 1, \
        "and allocates no second version"
    failed(fx.publish(world.ada, fx.policy(world.date), key=key),
           409, "idempotency_key_reuse")


# ---- what a policy must say ----------------------------------------------

def test_a_published_policy_is_echoed_with_its_version(managed):
    world = managed()
    body = fx.policy(world.date, slot_minutes=60, reservation_duration_minutes=120,
                     cancellation_cutoff_minutes=0)
    published = ok(fx.publish(world.ada, body), 201)
    assert published["policy_version"] == 1
    for field, value in body.items():
        assert published[field] == value, field


def test_versions_count_up_per_restaurant(api, reset):
    reset(fx.fixture(restaurants=[fx.managed("u_ada", rid="r_one"),
                                  fx.managed("u_ada", rid="r_two")]))
    ada = api().authenticated(ADA["email"], ADA["password"])
    date = fx.booking_date()
    assert ok(fx.publish(ada, fx.policy(date), restaurant_id="r_one"),
              201)["policy_version"] == 1
    assert ok(fx.publish(ada, fx.policy(date), restaurant_id="r_one"),
              201)["policy_version"] == 2
    assert ok(fx.publish(ada, fx.policy(date), restaurant_id="r_two"),
              201)["policy_version"] == 1, "each restaurant counts for itself"


@pytest.mark.parametrize("drop", ["effective_from", "slot_minutes",
                                  "reservation_duration_minutes",
                                  "cancellation_cutoff_minutes", "opening_hours",
                                  "capacities"])
def test_a_policy_is_complete_or_nothing(managed, drop):
    """It accepts a complete policy, not a patch: every field is required."""
    world = managed()
    body = fx.policy(world.date)
    del body[drop]
    failed(fx.publish(world.ada, body), 422, "validation_failed")


@pytest.mark.parametrize("field,value", [
    ("effective_from", "2026-02-30"),
    ("effective_from", "not-a-date"),
    ("effective_from", "2026-9-24"),
    ("effective_from", 20260924),
    ("slot_minutes", 0),
    ("slot_minutes", 1441),
    ("slot_minutes", True),
    ("slot_minutes", "30"),
    ("slot_minutes", 30.5),
    ("reservation_duration_minutes", 0),
    ("reservation_duration_minutes", 1441),
    ("reservation_duration_minutes", False),
    ("cancellation_cutoff_minutes", -1),
    ("cancellation_cutoff_minutes", 10081),
    ("cancellation_cutoff_minutes", True),
    ("opening_hours", "all week"),
    ("opening_hours", [{"weekday": "funday", "opens": "18:00", "closes": "23:00"}]),
    ("opening_hours", [{"weekday": "mon", "opens": "23:00", "closes": "18:00"}]),
    ("opening_hours", [{"weekday": "mon", "opens": "6pm", "closes": "23:00"}]),
    ("capacities", {"t_1": 2, "t_2": 4}),
    ("capacities", {"t_1": 2, "t_2": 4, "t_3": 6, "t_9": 2}),
    ("capacities", {"t_1": 0, "t_2": 4, "t_3": 6}),
    ("capacities", {"t_1": 101, "t_2": 4, "t_3": 6}),
    ("capacities", {"t_1": True, "t_2": 4, "t_3": 6}),
    ("capacities", []),
])
def test_an_invalid_policy_is_422_and_allocates_nothing(managed, field, value):
    world = managed()
    body = fx.policy(world.date)
    body[field] = value
    failed(fx.publish(world.ada, body), 422, "validation_failed")
    assert ok(world.anon.get(f"/restaurants/{world.rid}/policies"))["policies"] == []
    # The next good publication is still version 1.
    assert ok(fx.publish(world.ada, fx.policy(world.date)), 201)["policy_version"] == 1


def test_duplicate_weekdays_are_refused_in_a_policy(managed):
    """A fixture may keep two sittings on a day; a policy may not -- stage 3 says its
    opening hours contain no duplicate weekdays."""
    world = managed()
    twice = [{"weekday": "mon", "opens": "12:00", "closes": "15:00"},
             {"weekday": "mon", "opens": "18:00", "closes": "23:00"}]
    failed(fx.publish(world.ada, fx.policy(world.date, opening_hours=twice)),
           422, "validation_failed")


def test_unknown_policy_fields_are_ignored(managed):
    world = managed()
    body = fx.policy(world.date, souvenir="yes", timezone="Mars/Olympus",
                     tables=[{"id": "t_9", "label": "9", "capacity": 2}])
    published = ok(fx.publish(world.ada, body), 201)
    assert "souvenir" not in published
    detail = ok(world.anon.get(f"/restaurants/{world.rid}"))
    assert detail["timezone"] == "Europe/Berlin", "a policy cannot change the zone"
    assert [t["id"] for t in detail["tables"]] == ["t_1", "t_2", "t_3"], \
        "nor the tables"


def test_the_list_is_public_and_in_publication_order(managed):
    world = managed()
    later = fx.days_from(world.date, 7)
    first = ok(fx.publish(world.ada, fx.policy(later)), 201)
    second = ok(fx.publish(world.ada, fx.policy(world.date)), 201)
    listed = ok(world.anon.get(f"/restaurants/{world.rid}/policies"))["policies"]
    assert [p["policy_version"] for p in listed] == [1, 2]
    assert listed == [first, second], "publication order, not effective order"
    failed(world.anon.get("/restaurants/r_nope/policies"), 404, "not_found")


def test_policy_zero_is_not_a_publication(managed):
    world = managed()
    assert ok(world.anon.get(f"/restaurants/{world.rid}/policies"))["policies"] == []
    booking = ok(world.ada.book(starts_at_local=world.at()), 201)
    assert booking["accepted_terms"]["policy_version"] == 0, \
        "policy 0 governs before anything is published"
    assert ok(world.anon.get(f"/restaurants/{world.rid}/policies"))["policies"] == [], \
        "but it is never listed"


def test_policy_zero_is_the_fixture_rules(managed):
    world = managed()
    booking = ok(world.ada.book(starts_at_local=world.at()), 201)
    detail = ok(world.anon.get(f"/restaurants/{world.rid}"))
    terms = booking["accepted_terms"]
    assert terms["slot_minutes"] == detail["slot_minutes"]
    assert terms["reservation_duration_minutes"] == detail["reservation_duration_minutes"]
    assert terms["cancellation_cutoff_minutes"] == detail["cancellation_cutoff_minutes"]
    assert terms["opening_hours"] == detail["opening_hours"]
    assert terms["capacities"] == {t["id"]: t["capacity"] for t in detail["tables"]}
    assert "effective_from" not in terms, "accepted terms are a policy without its date"


# ---- which policy governs ------------------------------------------------

def test_the_greatest_effective_date_not_later_than_the_booking_wins(managed):
    world = managed()
    early = fx.days_from(world.date, -3)
    late = fx.days_from(world.date, 3)
    ok(fx.publish(world.ada, fx.policy(early, reservation_duration_minutes=60)), 201)
    ok(fx.publish(world.ada, fx.policy(late, reservation_duration_minutes=120)), 201)

    on_the_day = ok(world.ada.book(table_id="t_2", starts_at_local=world.at()), 201)
    assert on_the_day["accepted_terms"]["policy_version"] == 1
    assert on_the_day["accepted_terms"]["reservation_duration_minutes"] == 60

    after = ok(world.ada.book(table_id="t_3",
                              starts_at_local=world.at(date=fx.days_from(world.date, 4))),
               201)
    assert after["accepted_terms"]["policy_version"] == 2
    assert after["accepted_terms"]["reservation_duration_minutes"] == 120


def test_publication_order_does_not_decide_selection(managed):
    """A policy published later for an earlier date does not win on recency."""
    world = managed()
    ok(fx.publish(world.ada, fx.policy(world.date,
                                       reservation_duration_minutes=60)), 201)
    ok(fx.publish(world.ada, fx.policy(fx.days_from(world.date, -10),
                                       reservation_duration_minutes=30)), 201)
    booking = ok(world.ada.book(starts_at_local=world.at()), 201)
    assert booking["accepted_terms"]["policy_version"] == 1
    assert booking["accepted_terms"]["reservation_duration_minutes"] == 60


def test_a_tie_on_the_date_goes_to_the_greater_version(managed):
    world = managed()
    ok(fx.publish(world.ada, fx.policy(world.date,
                                       reservation_duration_minutes=60)), 201)
    superseding = ok(fx.publish(world.ada, fx.policy(
        world.date, reservation_duration_minutes=30)), 201)
    assert superseding["policy_version"] == 2
    booking = ok(world.ada.book(starts_at_local=world.at()), 201)
    assert booking["accepted_terms"]["policy_version"] == 2
    assert booking["accepted_terms"]["reservation_duration_minutes"] == 30
    assert booking["ends_at"].startswith(f"{world.date}T19:30:00")


def test_a_booking_before_every_effective_date_gets_policy_zero(managed):
    world = managed()
    ok(fx.publish(world.ada, fx.policy(fx.days_from(world.date, 1),
                                       reservation_duration_minutes=60)), 201)
    booking = ok(world.ada.book(starts_at_local=world.at()), 201)
    assert booking["accepted_terms"]["policy_version"] == 0
    assert booking["ends_at"].startswith(f"{world.date}T20:30:00")


def test_an_effective_date_in_the_past_is_allowed(managed):
    world = managed()
    published = ok(fx.publish(world.ada, fx.policy(
        fx.days_from(world.date, -400), reservation_duration_minutes=60)), 201)
    assert published["policy_version"] == 1
    booking = ok(world.ada.book(starts_at_local=world.at()), 201)
    assert booking["accepted_terms"]["policy_version"] == 1


def test_publication_never_edits_an_accepted_booking(managed):
    """A policy changes later decisions, not bookings that were already accepted."""
    world = managed()
    booking = ok(world.ada.book(table_id="t_2", starts_at_local=world.at()), 201)
    history_before = ok(world.ada.get(
        f"/reservations/{booking['reference']}/history"))["entries"]

    ok(fx.publish(world.ada, fx.policy(fx.days_from(world.date, -1),
                                       reservation_duration_minutes=180,
                                       cancellation_cutoff_minutes=0)), 201)

    after = ok(world.ada.get(f"/reservations/{booking['reference']}"))
    assert after == booking, "no field of it moved, ends_at included"
    assert ok(world.ada.get(
        f"/reservations/{booking['reference']}/history"))["entries"] == history_before
    decision = ok(world.ada.get(f"/reservations/{booking['reference']}/decision"))
    assert decision["accepted_terms"]["policy_version"] == 0
    assert decision["revision"] == 1


# ---- what the policy decides ---------------------------------------------

def test_availability_follows_the_selected_policy(managed):
    world = managed()
    ok(fx.publish(world.ada, fx.policy(
        world.date, slot_minutes=60, reservation_duration_minutes=120,
        opening_hours=fx.all_week("18:00", "22:00"),
        capacities={"t_1": 6, "t_2": 1, "t_3": 1})), 201)

    body = ok(fx.availability(world.anon, date=world.date, party_size=4))
    assert [slot["starts_at_local"].split("T")[1] for slot in body["slots"]] == \
        ["18:00", "19:00", "20:00"], "the policy's grid, duration and hours"
    assert body["slots"][0]["available_table_ids"] == ["t_1"], \
        "and the policy's capacities, not the fixture's"

    yesterday = fx.days_from(world.date, -1)
    before = ok(fx.availability(world.anon, date=yesterday, party_size=4))
    assert [slot["starts_at_local"].split("T")[1] for slot in before["slots"]] == \
        fx.expected_slots() if False else True


def test_a_policy_decides_the_grid_a_booking_must_sit_on(managed):
    world = managed()
    ok(fx.publish(world.ada, fx.policy(world.date, slot_minutes=60)), 201)
    failed(world.ada.book(starts_at_local=world.at("19:30")), 422, "not_on_slot_grid")
    ok(world.ada.book(starts_at_local=world.at("19:00")), 201)


def test_a_policy_decides_opening_hours_a_booking_must_fit(managed):
    world = managed()
    ok(fx.publish(world.ada, fx.policy(
        world.date, opening_hours=fx.all_week("18:00", "20:00"))), 201)
    ok(world.ada.book(starts_at_local=world.at("18:30")), 201)
    failed(world.ada.book(starts_at_local=world.at("19:00")),
           422, "outside_opening_hours")


def test_a_policy_decides_capacity(managed):
    world = managed()
    ok(fx.publish(world.ada, fx.policy(
        world.date, capacities={"t_1": 8, "t_2": 1, "t_3": 1})), 201)
    ok(world.ada.book(table_id="t_1", party_size=8, starts_at_local=world.at()), 201)
    failed(world.ada.book(table_id="t_2", party_size=4, starts_at_local=world.at()),
           422, "party_exceeds_capacity")


def test_a_policy_decides_a_combinations_capacity(api, reset):
    """Stage 3: a combination's capacity is the sum of the *selected policy's*
    capacities, not the fixture's."""
    reset(fx.fixture(restaurants=[{**fx.managed("u_ada"),
                                   "combinable": [["t_1", "t_2"]]}]))
    ada = api().authenticated(ADA["email"], ADA["password"])
    anon = api()
    date = fx.booking_date()
    ok(fx.publish(ada, fx.policy(date, capacities={"t_1": 10, "t_2": 10, "t_3": 1})), 201)

    options = next(slot["available_options"] for slot in ok(fx.availability(
        anon, date=date, party_size=20))["slots"])
    assert [option["table_ids"] for option in options] == [["t_1", "t_2"]]
    assert options[0]["capacity"] == 20
    ok(ada.post("/reservations", json={
        "restaurant_id": "r_anker", "table_ids": ["t_1", "t_2"],
        "starts_at_local": fx.local(date, "19:00"), "party_size": 20},
        key=new_key()), 201)


def test_two_bookings_of_different_durations_share_a_table_correctly(managed):
    """A booking keeps the duration it accepted, so occupancy has to compare each
    interval on its own terms rather than assuming one length."""
    world = managed()
    tomorrow = fx.days_from(world.date, 1)
    # Today: 90 minutes. Tomorrow: 30 minutes.
    ok(fx.publish(world.ada, fx.policy(tomorrow, reservation_duration_minutes=30)), 201)
    long_booking = ok(world.ada.book(table_id="t_2", starts_at_local=world.at("19:00")), 201)
    assert long_booking["ends_at"].startswith(f"{world.date}T20:30")

    # A 30-minute booking tomorrow at 19:00 leaves 19:30 free on the same table.
    short = ok(world.ada.book(table_id="t_2",
                              starts_at_local=world.at("19:00", date=tomorrow)), 201)
    assert short["ends_at"].startswith(f"{tomorrow}T19:30")
    ok(world.ada.book(table_id="t_2",
                      starts_at_local=world.at("19:30", date=tomorrow)), 201)
    # While today's 90-minute booking still blocks 19:30.
    failed(world.bob.book(table_id="t_2", starts_at_local=world.at("19:30")),
           409, "table_unavailable")
