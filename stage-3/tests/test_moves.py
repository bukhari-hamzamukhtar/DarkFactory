"""§11: several amendments in one request, all or nothing."""
from __future__ import annotations

import conftest as fx
from conftest import ADA, failed, new_key, ok
import pytest


@pytest.fixture
def pair(world):
    """Two of Ada's bookings at 19:00, on t_2 and t_3."""
    first = ok(world.ada.book(table_id="t_2", starts_at_local=world.at("19:00")), 201)
    second = ok(world.ada.book(table_id="t_3", starts_at_local=world.at("19:00")), 201)
    return first, second


def test_a_swap_that_no_sequence_of_amendments_could_make(world, pair):
    """The reason batches exist: each half of a swap is illegal on its own.

    t_2 and t_3 are both taken, so moving either booking to the other's table one at
    a time is a 409. Together they are a permutation and must commit.
    """
    first, second = pair
    failed(world.ada.patch(f"/reservations/{first['reference']}",
                           json={"table_id": "t_3"}), 409, "table_unavailable")

    result = ok(world.ada.moves([
        {"reference": first["reference"], "table_id": "t_3"},
        {"reference": second["reference"], "table_id": "t_2"}]), 201)

    assert [r["reference"] for r in result["reservations"]] == \
        [first["reference"], second["reference"]], "input order"
    assert [r["table_id"] for r in result["reservations"]] == ["t_3", "t_2"]
    assert ok(world.ada.get(f"/reservations/{first['reference']}"))["table_id"] == "t_3"
    assert ok(world.ada.get(f"/reservations/{second['reference']}"))["table_id"] == "t_2"


def test_a_batch_keeps_identity_owner_and_creation_time(world, pair):
    first, _ = pair
    moved = ok(world.ada.moves([
        {"reference": first["reference"], "table_id": "t_1", "party_size": 2}]),
        201)["reservations"][0]
    assert moved["reservation_id"] == first["reservation_id"]
    assert moved["reference"] == first["reference"]
    assert moved["created_at"] == first["created_at"]
    assert moved["status"] == "confirmed"
    failed(world.bob.get(f"/reservations/{first['reference']}"), 404, "not_found")


def test_omitted_fields_are_retained_and_unknown_fields_ignored(world, pair):
    first, _ = pair
    moved = ok(world.ada.moves([{"reference": first["reference"], "souvenir": "yes"}]),
               201)["reservations"][0]
    for field in ("table_id", "party_size", "starts_at_local", "starts_at", "ends_at",
                  "status", "created_at", "reservation_id", "reference"):
        assert moved[field] == first[field], field


def test_a_batch_may_change_time_table_and_party_size_together(world, pair):
    first, _ = pair
    moved = ok(world.ada.moves([{
        "reference": first["reference"], "table_id": "t_1",
        "starts_at_local": world.at("20:30"), "party_size": 2}]),
        201)["reservations"][0]
    assert moved["table_id"] == "t_1" and moved["party_size"] == 2
    assert moved["starts_at_local"] == world.at("20:30")
    assert moved["ends_at"].startswith(f"{world.date}T22:00:00")


def test_eight_moves_are_allowed_and_nine_are_not(api, reset):
    """§11: a batch holds 1..8 objects."""
    tables = [{"id": f"t_{i}", "label": str(i), "capacity": 4} for i in range(20)]
    reset(fx.fixture(restaurants=[fx.restaurant(tables=tables)]))
    ada = api().authenticated(ADA["email"], ADA["password"])
    date = fx.booking_date()
    booked = [ok(ada.book(table_id=f"t_{i}", starts_at_local=fx.local(date, "19:00")), 201)
              for i in range(9)]

    eight = [{"reference": booked[i]["reference"], "table_id": f"t_{i + 10}"}
             for i in range(8)]
    result = ok(ada.moves(eight), 201)
    assert [r["table_id"] for r in result["reservations"]] ==         [f"t_{i + 10}" for i in range(8)]

    nine = [{"reference": booked[i]["reference"], "table_id": f"t_{i}"}
            for i in range(9)]
    failed(ada.moves(nine), 422, "validation_failed")
    assert ok(ada.get(f"/reservations/{booked[0]['reference']}"))["table_id"] == "t_10",         "the rejected batch changed nothing"


@pytest.mark.parametrize("body", [
    {"moves": []},
    {"moves": "BOOK01"},
    {"moves": {}},
    {"moves": [[]]},
    {"moves": ["BOOK01"]},
    {"moves": [{}]},
    {"moves": [{"reference": 7}]},
    {"moves": [{"reference": ""}]},
    {},
])
def test_an_invalid_batch_shape_is_422(world, pair, body):
    resp = world.ada.post("/reservation-moves", json=body, key=new_key())
    failed(resp, 422, "validation_failed")


def test_duplicate_references_are_422(world, pair):
    first, _ = pair
    failed(world.ada.moves([
        {"reference": first["reference"], "table_id": "t_1"},
        {"reference": first["reference"], "party_size": 2}]),
        422, "validation_failed")
    assert ok(world.ada.get(f"/reservations/{first['reference']}"))["table_id"] == "t_2"


def test_an_unknown_reference_is_404(world, pair):
    first, _ = pair
    failed(world.ada.moves([{"reference": first["reference"], "table_id": "t_1"},
                            {"reference": "ZZZZZZ", "table_id": "t_2"}]),
           404, "not_found")
    assert ok(world.ada.get(f"/reservations/{first['reference']}"))["table_id"] == "t_2", \
        "nothing commits when one item fails"


def test_someone_elses_reference_is_404(world, pair):
    first, _ = pair
    hers = ok(world.bob.book(table_id="t_1", party_size=2,
                             starts_at_local=world.at("19:00")), 201)
    failed(world.ada.moves([{"reference": first["reference"], "party_size": 2},
                            {"reference": hers["reference"], "table_id": "t_1"}]),
           404, "not_found")
    assert ok(world.bob.get(f"/reservations/{hers['reference']}"))["party_size"] == 2


def test_bookings_at_two_restaurants_cannot_be_batched(api, reset):
    other = fx.restaurant("r_other", name="Other")
    reset(fx.fixture(restaurants=[fx.restaurant(), other]))
    ada = api().authenticated(ADA["email"], ADA["password"])
    date = fx.booking_date()
    here = ok(ada.book(restaurant_id="r_anker", table_id="t_2",
                       starts_at_local=fx.local(date, "19:00")), 201)
    there = ok(ada.book(restaurant_id="r_other", table_id="t_2",
                        starts_at_local=fx.local(date, "19:00")), 201)
    failed(ada.moves([{"reference": here["reference"], "party_size": 2},
                      {"reference": there["reference"], "party_size": 2}]),
           422, "validation_failed")
    assert ok(ada.get(f"/reservations/{here['reference']}"))["party_size"] == 4
    assert ok(ada.get(f"/reservations/{there['reference']}"))["party_size"] == 4


def test_a_cancelled_booking_in_a_batch_is_409(world, pair):
    first, second = pair
    ok(world.ada.post(f"/reservations/{second['reference']}/cancel"))
    failed(world.ada.moves([{"reference": first["reference"], "table_id": "t_1",
                             "party_size": 2},
                            {"reference": second["reference"], "table_id": "t_3"}]),
           409, "reservation_cancelled")
    assert ok(world.ada.get(f"/reservations/{first['reference']}"))["table_id"] == "t_2"


def test_errors_take_precedence_in_input_order(world, pair):
    """§11: non-occupancy errors are reported in input order. The first item's
    unknown table is a 404 even though the second item is a cancelled booking."""
    first, second = pair
    ok(world.ada.post(f"/reservations/{second['reference']}/cancel"))
    failed(world.ada.moves([{"reference": first["reference"], "table_id": "t_nope"},
                            {"reference": second["reference"], "party_size": 2}]),
           404, "not_found")


def test_a_bookings_cutoff_precedes_what_it_was_asked_to_change(api, reset):
    """§11: for a given booking, the cutoff is decided before its other changes."""
    locked = fx.restaurant(cancellation_cutoff_minutes=60 * 24 * 3650)
    reset(fx.fixture(restaurants=[locked]))
    ada = api().authenticated(ADA["email"], ADA["password"])
    date = fx.booking_date()
    booking = ok(ada.book(table_id="t_2", starts_at_local=fx.local(date, "19:00")), 201)
    # The item is also asking for an unknown table; the cutoff answers first.
    failed(ada.moves([{"reference": booking["reference"], "table_id": "t_nope"}]),
           409, "cutoff_passed")


def test_ordinary_amendment_codes_are_used_inside_a_batch(world, pair):
    first, _ = pair
    for change, status, code in (
        ({"starts_at_local": world.at("19:15")}, 422, "not_on_slot_grid"),
        ({"starts_at_local": world.at("17:00")}, 422, "outside_opening_hours"),
        ({"starts_at_local": world.at("22:00")}, 422, "outside_opening_hours"),
        ({"table_id": "t_1"}, 422, "party_exceeds_capacity"),
        ({"party_size": 0}, 422, "validation_failed"),
        ({"party_size": "four"}, 422, "validation_failed"),
        ({"starts_at_local": "not-a-time"}, 422, "validation_failed"),
        ({"table_id": "t_nope"}, 404, "not_found"),
    ):
        failed(world.ada.moves([{"reference": first["reference"], **change}]),
               status, code)
    assert ok(world.ada.get(f"/reservations/{first['reference']}"))["table_id"] == "t_2"


def test_a_batch_that_would_overlap_an_unlisted_booking_is_409(world, pair):
    first, second = pair
    failed(world.ada.moves([{"reference": first["reference"], "table_id": "t_3"}]),
           409, "table_unavailable")
    assert ok(world.ada.get(f"/reservations/{first['reference']}"))["table_id"] == "t_2"
    assert ok(world.ada.get(f"/reservations/{second['reference']}"))["table_id"] == "t_3"


def test_a_batch_that_would_overlap_itself_is_409(world, pair):
    """Both moves aim at the same free table and time."""
    first, second = pair
    failed(world.ada.moves([{"reference": first["reference"], "table_id": "t_1",
                             "party_size": 2},
                            {"reference": second["reference"], "table_id": "t_1",
                             "party_size": 2}]),
           409, "table_unavailable")
    assert ok(world.ada.get(f"/reservations/{first['reference']}"))["table_id"] == "t_2"


def test_unchanged_listed_bookings_keep_their_occupancy(world, pair):
    """§11: an unchanged item still holds its table, so another item cannot take it."""
    first, second = pair
    failed(world.ada.moves([{"reference": first["reference"]},
                            {"reference": second["reference"], "table_id": "t_2"}]),
           409, "table_unavailable")


def test_a_rotation_of_three_commits_at_once(world):
    first = ok(world.ada.book(table_id="t_1", party_size=2,
                              starts_at_local=world.at("19:00")), 201)
    second = ok(world.ada.book(table_id="t_2", party_size=2,
                               starts_at_local=world.at("19:00")), 201)
    third = ok(world.ada.book(table_id="t_3", party_size=2,
                              starts_at_local=world.at("19:00")), 201)
    result = ok(world.ada.moves([
        {"reference": first["reference"], "table_id": "t_2"},
        {"reference": second["reference"], "table_id": "t_3"},
        {"reference": third["reference"], "table_id": "t_1"}]), 201)
    assert [r["table_id"] for r in result["reservations"]] == ["t_2", "t_3", "t_1"]


def test_a_failed_batch_leaves_the_key_reusable(world, pair):
    first, _ = pair
    key = new_key()
    failed(world.ada.moves([{"reference": first["reference"], "table_id": "t_nope"}],
                           key=key), 404, "not_found")
    ok(world.ada.moves([{"reference": first["reference"], "party_size": 2}], key=key),
       201)


def test_a_batch_replay_returns_the_original_after_a_cancellation(world, pair):
    first, _ = pair
    key = new_key()
    batch = [{"reference": first["reference"], "table_id": "t_1", "party_size": 2}]
    created = ok(world.ada.moves(batch, key=key), 201)
    ok(world.ada.post(f"/reservations/{first['reference']}/cancel"))
    replay = ok(world.ada.moves(batch, key=key), 200)
    assert replay == created
    assert ok(world.ada.get(f"/reservations/{first['reference']}"))["status"] == \
        "cancelled", "the replay changed nothing"


def test_a_batch_with_a_changed_body_is_a_key_reuse(world, pair):
    first, _ = pair
    key = new_key()
    ok(world.ada.moves([{"reference": first["reference"], "party_size": 2}], key=key), 201)
    failed(world.ada.moves([{"reference": first["reference"], "party_size": 3}], key=key),
           409, "idempotency_key_reuse")


def test_a_batch_without_a_token_is_401(world, pair):
    first, _ = pair
    resp = world.anon.post("/reservation-moves", json={
        "moves": [{"reference": first["reference"], "table_id": "t_1"}]}, key=new_key())
    failed(resp, 401, "unauthenticated")


def test_a_malformed_batch_body_is_400(world):
    failed(world.ada.post("/reservation-moves", content="{not json", key=new_key()),
           400, "malformed_request")


def test_a_no_op_batch_returns_every_field_unchanged(world, pair):
    first, second = pair
    result = ok(world.ada.moves([{"reference": first["reference"]},
                                 {"reference": second["reference"]}]), 201)
    assert result["reservations"] == [first, second]
