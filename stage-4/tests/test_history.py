"""Stage 3: a reservation's own record, and the decision behind it."""
from __future__ import annotations

import conftest as fx
from conftest import ADA, failed, new_key, ok
import pytest


def entries_of(client, reference):
    body = ok(client.get(f"/reservations/{reference}/history"))
    assert body["reference"] == reference
    return body["entries"]


def fields_of(entry):
    return [change["field"] for change in entry["changes"]]


# ---- created -------------------------------------------------------------

def test_creation_names_all_three_fields_from_null(world):
    booking = ok(world.ada.book(table_id="t_2", starts_at_local=world.at("19:00"),
                                party_size=4), 201)
    entries = entries_of(world.ada, booking["reference"])
    assert len(entries) == 1
    created = entries[0]
    assert created["seq"] == 1 and created["event"] == "created"
    assert created["changes"] == [
        {"field": "table_id", "from": None, "to": "t_2"},
        {"field": "starts_at_local", "from": None, "to": world.at("19:00")},
        {"field": "party_size", "from": None, "to": 4},
    ]
    assert created["revision"] == booking["revision"] == 1
    assert created["accepted_terms"] == booking["accepted_terms"]
    assert created["at"], "every entry is stamped"


def test_a_seeded_booking_has_a_record_too(reset, api):
    date = fx.booking_date()
    seeded = {"id": "res_seed", "reference": "SEED01", "user_id": "u_ada",
              "restaurant_id": "r_anker", "table_id": "t_2",
              "starts_at_local": fx.local(date, "19:00"), "party_size": 4,
              "created_at": "2026-01-01T10:00:00+00:00"}
    reset(fx.fixture(reservations=[seeded]))
    ada = api().authenticated(ADA["email"], ADA["password"])
    entries = entries_of(ada, "SEED01")
    assert [entry["event"] for entry in entries] == ["created"]
    assert entries[0]["at"] == "2026-01-01T10:00:00+00:00", \
        "stamped with its own created_at, not with now"
    assert entries[0]["revision"] == 1
    assert entries[0]["accepted_terms"]["policy_version"] == 0


def test_a_replay_records_nothing(world):
    key = new_key()
    booking = ok(world.ada.book(table_id="t_2", starts_at_local=world.at("19:00"),
                                key=key), 201)
    ok(world.ada.book(table_id="t_2", starts_at_local=world.at("19:00"), key=key), 200)
    assert len(entries_of(world.ada, booking["reference"])) == 1, \
        "a replay returns the original response and does not re-run the operation"


# ---- changed -------------------------------------------------------------

def test_a_change_names_only_what_moved_with_its_old_value(world):
    booking = ok(world.ada.book(table_id="t_2", starts_at_local=world.at("19:00"),
                                party_size=4), 201)
    reference = booking["reference"]
    ok(world.ada.patch(f"/reservations/{reference}", json={"table_id": "t_3"}))
    entries = entries_of(world.ada, reference)
    assert [entry["seq"] for entry in entries] == [1, 2]
    assert entries[1]["event"] == "changed"
    assert entries[1]["changes"] == [
        {"field": "table_id", "from": "t_2", "to": "t_3"}]
    assert entries[1]["revision"] == 2


def test_several_fields_are_reported_in_the_stated_order(world):
    booking = ok(world.ada.book(table_id="t_2", starts_at_local=world.at("19:00"),
                                party_size=4), 201)
    reference = booking["reference"]
    ok(world.ada.patch(f"/reservations/{reference}", json={
        "party_size": 2, "starts_at_local": world.at("20:30"), "table_id": "t_1"}))
    changed = entries_of(world.ada, reference)[1]
    assert fields_of(changed) == ["table_id", "starts_at_local", "party_size"]
    assert changed["changes"][0] == {"field": "table_id", "from": "t_2", "to": "t_1"}
    assert changed["changes"][1] == {"field": "starts_at_local",
                                     "from": world.at("19:00"), "to": world.at("20:30")}
    assert changed["changes"][2] == {"field": "party_size", "from": 4, "to": 2}


def test_only_the_fields_that_really_moved_appear(world):
    booking = ok(world.ada.book(table_id="t_2", starts_at_local=world.at("19:00"),
                                party_size=4), 201)
    reference = booking["reference"]
    # table_id is sent but unchanged; only the party size moved.
    ok(world.ada.patch(f"/reservations/{reference}", json={
        "table_id": "t_2", "party_size": 3}))
    changed = entries_of(world.ada, reference)[1]
    assert fields_of(changed) == ["party_size"]


def test_a_patch_that_changes_nothing_records_no_entry(world):
    booking = ok(world.ada.book(table_id="t_2", starts_at_local=world.at("19:00"),
                                party_size=4), 201)
    reference = booking["reference"]
    for noop in ({}, {"party_size": 4}, {"table_id": "t_2"},
                 {"starts_at_local": world.at("19:00")},
                 {"table_id": "t_2", "starts_at_local": world.at("19:00"),
                  "party_size": 4}):
        same = ok(world.ada.patch(f"/reservations/{reference}", json=noop))
        assert same == booking, f"a no-op changes nothing at all: {noop}"
    assert len(entries_of(world.ada, reference)) == 1
    assert ok(world.ada.get(f"/reservations/{reference}"))["revision"] == 1


def test_a_failed_amendment_records_no_entry(world):
    booking = ok(world.ada.book(table_id="t_2", starts_at_local=world.at("19:00")), 201)
    reference = booking["reference"]
    ok(world.bob.book(table_id="t_3", starts_at_local=world.at("19:00")), 201)
    failed(world.ada.patch(f"/reservations/{reference}", json={"table_id": "t_3"}),
           409, "table_unavailable")
    failed(world.ada.patch(f"/reservations/{reference}", json={"party_size": 0}),
           422, "validation_failed")
    assert len(entries_of(world.ada, reference)) == 1
    assert ok(world.ada.get(f"/reservations/{reference}"))["revision"] == 1


def test_the_sequence_is_total_across_many_quick_writes(world):
    booking = ok(world.ada.book(table_id="t_2", starts_at_local=world.at("19:00"),
                                party_size=4), 201)
    reference = booking["reference"]
    for size in (3, 2, 1, 2, 3, 4):
        ok(world.ada.patch(f"/reservations/{reference}", json={"party_size": size}))
    entries = entries_of(world.ada, reference)
    assert [entry["seq"] for entry in entries] == list(range(1, 8))
    assert [entry["revision"] for entry in entries] == list(range(1, 8))
    assert entries == sorted(entries, key=lambda entry: (entry["at"], entry["seq"])), \
        "seq order is also at order"


# ---- cancelled -----------------------------------------------------------

def test_cancellation_is_the_last_word(world):
    booking = ok(world.ada.book(table_id="t_2", starts_at_local=world.at("19:00")), 201)
    reference = booking["reference"]
    ok(world.ada.patch(f"/reservations/{reference}", json={"party_size": 2}))
    ok(world.ada.post(f"/reservations/{reference}/cancel"))
    entries = entries_of(world.ada, reference)
    assert [entry["event"] for entry in entries] == ["created", "changed", "cancelled"]
    assert entries[-1]["changes"] == []
    assert entries[-1]["revision"] == 3

    # Cancelling again changes nothing, and nothing can follow a cancellation.
    ok(world.ada.post(f"/reservations/{reference}/cancel"))
    failed(world.ada.patch(f"/reservations/{reference}", json={"party_size": 4}),
           409, "reservation_cancelled")
    assert entries_of(world.ada, reference) == entries
    assert ok(world.ada.get(f"/reservations/{reference}"))["revision"] == 3


def test_a_cancelled_reservation_still_has_its_history_and_decision(world):
    booking = ok(world.ada.book(table_id="t_2", starts_at_local=world.at("19:00")), 201)
    reference = booking["reference"]
    ok(world.ada.post(f"/reservations/{reference}/cancel"))
    assert len(entries_of(world.ada, reference)) == 2
    decision = ok(world.ada.get(f"/reservations/{reference}/decision"))
    assert decision == {"reference": reference, "revision": 2,
                        "accepted_terms": booking["accepted_terms"]}


# ---- terms on entries ----------------------------------------------------

def test_an_old_entry_never_acquires_newer_terms(api, reset):
    reset(fx.fixture(restaurants=[fx.managed("u_ada")]))
    ada = api().authenticated(ADA["email"], ADA["password"])
    date = fx.booking_date()
    booking = ok(ada.book(table_id="t_2", starts_at_local=fx.local(date, "19:00")), 201)
    reference = booking["reference"]

    ok(fx.publish(ada, fx.policy(fx.days_from(date, -1),
                                 reservation_duration_minutes=60)), 201)
    amended = ok(ada.patch(f"/reservations/{reference}", json={"party_size": 2}))
    assert amended["accepted_terms"]["policy_version"] == 1, \
        "a real amendment adopts the policy for the resulting date"
    assert amended["ends_at"].startswith(f"{date}T20:00"), "and its end time with it"

    entries = entries_of(ada, reference)
    assert entries[0]["accepted_terms"]["policy_version"] == 0
    assert entries[0]["accepted_terms"]["reservation_duration_minutes"] == 90
    assert entries[1]["accepted_terms"]["policy_version"] == 1
    assert entries[1]["accepted_terms"]["reservation_duration_minutes"] == 60


def test_a_no_op_keeps_the_terms_it_had(api, reset):
    """Stage 3: a no-op retains terms, end time and revision, even when a newer
    policy would have applied to a real change."""
    reset(fx.fixture(restaurants=[fx.managed("u_ada")]))
    ada = api().authenticated(ADA["email"], ADA["password"])
    date = fx.booking_date()
    booking = ok(ada.book(table_id="t_2", starts_at_local=fx.local(date, "19:00"),
                          party_size=4), 201)
    ok(fx.publish(ada, fx.policy(fx.days_from(date, -1),
                                 reservation_duration_minutes=60)), 201)
    same = ok(ada.patch(f"/reservations/{booking['reference']}", json={"party_size": 4}))
    assert same == booking
    assert len(entries_of(ada, booking["reference"])) == 1


# ---- combinations --------------------------------------------------------

@pytest.fixture
def joined(api, reset):
    reset(fx.fixture(restaurants=[{**fx.managed("u_ada"),
                                   "combinable": [["t_1", "t_2"], ["t_2", "t_3"]]}]))

    class World:
        pass

    world = World()
    world.date = fx.booking_date()
    world.ada = api().authenticated(ADA["email"], ADA["password"])
    world.at = lambda hhmm="19:00": fx.local(world.date, hhmm)
    return world


def test_a_pair_is_created_as_table_ids(joined):
    booking = ok(joined.ada.post("/reservations", json={
        "restaurant_id": "r_anker", "table_ids": ["t_1", "t_2"],
        "starts_at_local": joined.at(), "party_size": 6}, key=new_key()), 201)
    created = entries_of(joined.ada, booking["reference"])[0]
    assert created["changes"][0] == {
        "field": "table_ids", "from": None, "to": ["t_1", "t_2"]}
    assert fields_of(created) == ["table_ids", "starts_at_local", "party_size"]


def test_a_change_involving_a_pair_uses_complete_lists(joined):
    booking = ok(joined.ada.book(table_id="t_1", party_size=2,
                                 starts_at_local=joined.at()), 201)
    reference = booking["reference"]
    ok(joined.ada.patch(f"/reservations/{reference}",
                        json={"table_ids": ["t_1", "t_2"], "party_size": 6}))
    grown = entries_of(joined.ada, reference)[1]
    assert grown["changes"][0] == {"field": "table_ids", "from": ["t_1"],
                                   "to": ["t_1", "t_2"]}

    ok(joined.ada.patch(f"/reservations/{reference}",
                        json={"table_ids": ["t_2", "t_3"]}))
    moved = entries_of(joined.ada, reference)[2]
    assert moved["changes"][0] == {"field": "table_ids", "from": ["t_1", "t_2"],
                                   "to": ["t_2", "t_3"]}

    ok(joined.ada.patch(f"/reservations/{reference}",
                        json={"table_id": "t_3", "party_size": 2}))
    shrunk = entries_of(joined.ada, reference)[3]
    assert shrunk["changes"][0] == {"field": "table_ids", "from": ["t_2", "t_3"],
                                    "to": ["t_3"]}


def test_a_single_to_single_change_keeps_table_id(joined):
    booking = ok(joined.ada.book(table_id="t_2", starts_at_local=joined.at()), 201)
    ok(joined.ada.patch(f"/reservations/{booking['reference']}",
                        json={"table_id": "t_3"}))
    changed = entries_of(joined.ada, booking["reference"])[1]
    assert changed["changes"] == [{"field": "table_id", "from": "t_2", "to": "t_3"}]


def test_a_reversed_pair_is_not_an_amendment(joined):
    booking = ok(joined.ada.post("/reservations", json={
        "restaurant_id": "r_anker", "table_ids": ["t_1", "t_2"],
        "starts_at_local": joined.at(), "party_size": 6}, key=new_key()), 201)
    same = ok(joined.ada.patch(f"/reservations/{booking['reference']}",
                               json={"table_ids": ["t_2", "t_1"]}))
    assert same == booking, "the same set named the other way round changes nothing"
    assert len(entries_of(joined.ada, booking["reference"])) == 1


# ---- who may read it -----------------------------------------------------

@pytest.mark.parametrize("path", ["history", "decision"])
def test_a_record_is_the_owners_alone(world, path):
    booking = ok(world.ada.book(starts_at_local=world.at("19:00")), 201)
    reference = booking["reference"]
    ok(world.ada.get(f"/reservations/{reference}/{path}"))
    # Stage 3 resolves the exception explicitly: no token is 404 here, not 401.
    failed(world.anon.get(f"/reservations/{reference}/{path}", token=None),
           404, "not_found")
    failed(world.anon.get(f"/reservations/{reference}/{path}", token="not-a-token"),
           404, "not_found")
    failed(world.bob.get(f"/reservations/{reference}/{path}"), 404, "not_found")
    failed(world.ada.get(f"/reservations/ZZZZZZ/{path}"), 404, "not_found")


def test_the_decision_is_the_current_one(world):
    booking = ok(world.ada.book(table_id="t_2", starts_at_local=world.at("19:00")), 201)
    reference = booking["reference"]
    first = ok(world.ada.get(f"/reservations/{reference}/decision"))
    assert first == {"reference": reference, "revision": 1,
                     "accepted_terms": booking["accepted_terms"]}
    ok(world.ada.patch(f"/reservations/{reference}", json={"party_size": 2}))
    second = ok(world.ada.get(f"/reservations/{reference}/decision"))
    assert second["revision"] == 2
