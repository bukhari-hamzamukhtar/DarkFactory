"""Stage 2's combined tables: declared pairs, and what they occupy."""
from __future__ import annotations

import conftest as fx
from conftest import (ADA, Api, assert_no_double_booking, burst, failed, new_key,
                      no_5xx, ok, tally)
import pytest

PAIRS = [["t_1", "t_2"], ["t_2", "t_3"]]


@pytest.fixture
def joined(api, reset):
    """The default restaurant, declaring t_1+t_2 and t_2+t_3 combinable.

    t_1 seats 2, t_2 seats 4 and t_3 seats 6, so the pairs seat 6 and 10. Note that
    t_1+t_3 is *not* declared, which is what makes non-transitivity testable.
    """
    reset(fx.fixture(restaurants=[fx.combinable(PAIRS)]))

    class World:
        pass

    world = World()
    world.rid = "r_anker"
    world.date = fx.booking_date()
    world.ada = api().authenticated(ADA["email"], ADA["password"])
    world.bob = api().authenticated(fx.BOB["email"], fx.BOB["password"])
    world.anon = api()
    world.at = lambda hhmm="19:00": fx.local(world.date, hhmm)
    world.options = lambda party, at="19:00": next(
        slot["available_options"] for slot in ok(world.anon.get("/availability", params={
            "restaurant_id": "r_anker", "date": world.date, "party_size": party
        }))["slots"] if slot["starts_at_local"].endswith(at))
    world.singles = lambda party, at="19:00": next(
        slot["available_table_ids"] for slot in ok(world.anon.get("/availability", params={
            "restaurant_id": "r_anker", "date": world.date, "party_size": party
        }))["slots"] if slot["starts_at_local"].endswith(at))
    return world


def ids_of(options):
    return [option["table_ids"] for option in options]


# ---- availability --------------------------------------------------------

def test_the_restaurant_detail_reports_its_pairs(joined):
    detail = ok(joined.anon.get("/restaurants/r_anker"))
    assert detail["combinable"] == PAIRS


def test_options_list_singles_in_fixture_order_then_pairs_in_declared_order(joined):
    assert ids_of(joined.options(2)) == [
        ["t_1"], ["t_2"], ["t_3"], ["t_1", "t_2"], ["t_2", "t_3"]]
    for option in joined.options(2):
        assert option["capacity"] == {
            "t_1": 2, "t_2": 4, "t_3": 6, "t_1+t_2": 6, "t_2+t_3": 10
        }["+".join(option["table_ids"])]


def test_a_pair_is_offered_for_a_party_no_single_table_can_seat(joined):
    assert joined.singles(6) == ["t_3"]
    assert ids_of(joined.options(6)) == [["t_3"], ["t_1", "t_2"], ["t_2", "t_3"]]
    assert ids_of(joined.options(7)) == [["t_2", "t_3"]]
    assert ids_of(joined.options(10)) == [["t_2", "t_3"]]
    assert ids_of(joined.options(11)) == []


def test_available_table_ids_still_means_single_tables(joined):
    """§ stage 2: `available_table_ids` stays exactly as it was."""
    assert joined.singles(6) == ["t_3"]
    assert joined.singles(2) == ["t_1", "t_2", "t_3"]


def test_an_undeclared_pair_is_never_offered(api, reset):
    """Combining is not transitive, and table sizes do not imply it either."""
    reset(fx.fixture(restaurants=[fx.combinable(PAIRS)]))
    anon = api()
    options = next(slot["available_options"] for slot in ok(anon.get("/availability", params={
        "restaurant_id": "r_anker", "date": fx.booking_date(), "party_size": 8
    }))["slots"])
    assert ["t_1", "t_3"] not in [option["table_ids"] for option in options]


def test_a_restaurant_with_no_pairs_offers_only_singles(world):
    options = next(slot["available_options"] for slot in ok(world.anon.get(
        "/availability", params={"restaurant_id": world.rid, "date": world.date,
                                 "party_size": 2}))["slots"])
    assert [option["table_ids"] for option in options] == [["t_1"], ["t_2"], ["t_3"]]


# ---- booking a pair ------------------------------------------------------

def test_a_combination_books_and_reports_both_tables(joined):
    booking = ok(joined.ada.post("/reservations", json={
        "restaurant_id": "r_anker", "table_ids": ["t_1", "t_2"],
        "starts_at_local": joined.at("19:00"), "party_size": 6}, key=new_key()), 201)
    assert booking["table_ids"] == ["t_1", "t_2"]
    assert "table_id" not in booking, \
        "a combination is not one table, so table_id is omitted"
    assert booking["party_size"] == 6 and booking["status"] == "confirmed"
    assert ok(joined.ada.get(f"/reservations/{booking['reference']}")) == booking


def test_a_combination_occupies_every_table_in_it(joined):
    ok(joined.ada.post("/reservations", json={
        "restaurant_id": "r_anker", "table_ids": ["t_1", "t_2"],
        "starts_at_local": joined.at("19:00"), "party_size": 6}, key=new_key()), 201)

    assert joined.singles(2) == ["t_3"], "both members are held"
    assert ids_of(joined.options(2)) == [["t_3"]], "and so is every pair using them"
    for taken in (["t_1"], ["t_2"], ["t_1", "t_2"], ["t_2", "t_3"]):
        body = {"restaurant_id": "r_anker", "starts_at_local": joined.at("19:00"),
                "party_size": 2}
        if len(taken) == 1:
            body["table_id"] = taken[0]
        else:
            body["table_ids"] = taken
        failed(joined.bob.post("/reservations", json=body, key=new_key()),
               409, "table_unavailable")
    ok(joined.bob.post("/reservations", json={
        "restaurant_id": "r_anker", "table_id": "t_3",
        "starts_at_local": joined.at("19:00"), "party_size": 2}, key=new_key()), 201)


def test_a_single_booking_blocks_the_pairs_that_use_that_table(joined):
    ok(joined.bob.book(table_id="t_2", starts_at_local=joined.at("19:00")), 201)
    assert ids_of(joined.options(2)) == [["t_1"], ["t_3"]]
    failed(joined.ada.post("/reservations", json={
        "restaurant_id": "r_anker", "table_ids": ["t_1", "t_2"],
        "starts_at_local": joined.at("19:00"), "party_size": 6}, key=new_key()),
        409, "table_unavailable")


def test_an_adjacent_combination_is_not_an_overlap(joined):
    ok(joined.ada.post("/reservations", json={
        "restaurant_id": "r_anker", "table_ids": ["t_1", "t_2"],
        "starts_at_local": joined.at("19:00"), "party_size": 6}, key=new_key()), 201)
    ok(joined.ada.post("/reservations", json={
        "restaurant_id": "r_anker", "table_ids": ["t_1", "t_2"],
        "starts_at_local": joined.at("20:30"), "party_size": 6}, key=new_key()), 201)


def test_cancelling_a_combination_frees_every_table(joined):
    booking = ok(joined.ada.post("/reservations", json={
        "restaurant_id": "r_anker", "table_ids": ["t_1", "t_2"],
        "starts_at_local": joined.at("19:00"), "party_size": 6}, key=new_key()), 201)
    ok(joined.ada.post(f"/reservations/{booking['reference']}/cancel"))
    assert joined.singles(2) == ["t_1", "t_2", "t_3"]
    assert ids_of(joined.options(6)) == [["t_3"], ["t_1", "t_2"], ["t_2", "t_3"]]
    ok(joined.bob.book(table_id="t_1", party_size=2,
                       starts_at_local=joined.at("19:00")), 201)


@pytest.mark.parametrize("body,status,code", [
    ({"table_ids": ["t_1", "t_3"], "party_size": 6}, 422, "combination_not_allowed"),
    ({"table_ids": ["t_1", "t_2", "t_3"], "party_size": 6}, 422, "combination_not_allowed"),
    ({"table_ids": ["t_1", "t_1"], "party_size": 2}, 422, "validation_failed"),
    ({"table_ids": [], "party_size": 2}, 422, "validation_failed"),
    ({"table_ids": ["t_1"], "table_id": "t_1", "party_size": 2}, 422, "validation_failed"),
    ({"table_ids": ["t_1", "t_2"], "party_size": 7}, 422, "party_exceeds_capacity"),
    ({"table_ids": ["t_2", "t_3"], "party_size": 11}, 422, "party_exceeds_capacity"),
    ({"table_ids": ["t_1", "t_nope"], "party_size": 6}, 404, "not_found"),
    ({"table_ids": ["t_1", "x" * 65], "party_size": 6}, 422, "validation_failed"),
    ({"table_ids": "t_1", "party_size": 2}, 400, "malformed_request"),
    ({"table_ids": [7], "party_size": 2}, 400, "malformed_request"),
    ({"party_size": 2}, 422, "validation_failed"),
])
def test_the_stated_combination_errors(joined, body, status, code):
    failed(joined.ada.post("/reservations", json={
        "restaurant_id": "r_anker", "starts_at_local": joined.at("19:00"), **body},
        key=new_key()), status, code)


def test_a_pair_is_echoed_in_declared_order_whichever_way_it_is_sent(joined):
    booking = ok(joined.ada.post("/reservations", json={
        "restaurant_id": "r_anker", "table_ids": ["t_2", "t_1"],
        "starts_at_local": joined.at("19:00"), "party_size": 6}, key=new_key()), 201)
    assert booking["table_ids"] == ["t_1", "t_2"], \
        "the restaurant declared the pair in this order, so the API reports it so"


def test_table_id_is_still_accepted_and_means_a_set_of_one(joined):
    booking = ok(joined.ada.post("/reservations", json={
        "restaurant_id": "r_anker", "table_id": "t_3",
        "starts_at_local": joined.at("19:00"), "party_size": 5}, key=new_key()), 201)
    assert booking["table_ids"] == ["t_3"] and booking["table_id"] == "t_3"


# ---- amending to and from a combination ----------------------------------

def test_an_amendment_may_take_a_pair_and_give_it_back(joined):
    booking = ok(joined.ada.book(table_id="t_1", party_size=2,
                                 starts_at_local=joined.at("19:00")), 201)
    grown = ok(joined.ada.patch(f"/reservations/{booking['reference']}", json={
        "table_ids": ["t_1", "t_2"], "party_size": 6}))
    assert grown["table_ids"] == ["t_1", "t_2"] and "table_id" not in grown
    assert grown["reference"] == booking["reference"]
    assert joined.singles(2) == ["t_3"]

    shrunk = ok(joined.ada.patch(f"/reservations/{booking['reference']}", json={
        "table_id": "t_1", "party_size": 2}))
    assert shrunk["table_ids"] == ["t_1"] and shrunk["table_id"] == "t_1"
    # t_2 is given back; t_1 is still held, by this very booking.
    assert joined.singles(2) == ["t_2", "t_3"]
    assert ids_of(joined.options(6)) == [["t_3"], ["t_2", "t_3"]]


def test_an_amendment_onto_an_undeclared_pair_is_refused(joined):
    booking = ok(joined.ada.book(table_id="t_1", party_size=2,
                                 starts_at_local=joined.at("19:00")), 201)
    failed(joined.ada.patch(f"/reservations/{booking['reference']}",
                            json={"table_ids": ["t_1", "t_3"]}),
           422, "combination_not_allowed")
    assert ok(joined.ada.get(f"/reservations/{booking['reference']}"))["table_ids"] == \
        ["t_1"], "a refused amendment changes nothing"


def test_an_amendment_keeps_its_own_tables_out_of_its_own_way(joined):
    """Growing t_2 into t_1+t_2 must not collide with the booking doing the growing."""
    booking = ok(joined.ada.book(table_id="t_2", party_size=4,
                                 starts_at_local=joined.at("19:00")), 201)
    grown = ok(joined.ada.patch(f"/reservations/{booking['reference']}",
                                json={"table_ids": ["t_1", "t_2"], "party_size": 6}))
    assert grown["table_ids"] == ["t_1", "t_2"]


def test_a_combination_cannot_be_amended_onto_a_held_table(joined):
    ok(joined.bob.book(table_id="t_3", party_size=2,
                       starts_at_local=joined.at("19:00")), 201)
    booking = ok(joined.ada.post("/reservations", json={
        "restaurant_id": "r_anker", "table_ids": ["t_1", "t_2"],
        "starts_at_local": joined.at("19:00"), "party_size": 6}, key=new_key()), 201)
    failed(joined.ada.patch(f"/reservations/{booking['reference']}",
                            json={"table_ids": ["t_2", "t_3"]}),
           409, "table_unavailable")
    assert ok(joined.ada.get(f"/reservations/{booking['reference']}"))["table_ids"] == \
        ["t_1", "t_2"]


# ---- batches -------------------------------------------------------------

def test_a_batch_may_move_a_booking_onto_a_pair(joined):
    booking = ok(joined.ada.book(table_id="t_1", party_size=2,
                                 starts_at_local=joined.at("19:00")), 201)
    result = ok(joined.ada.moves([{"reference": booking["reference"],
                                   "table_ids": ["t_1", "t_2"], "party_size": 6}]), 201)
    assert result["reservations"][0]["table_ids"] == ["t_1", "t_2"]


def test_a_batch_may_swap_a_pair_and_a_single(joined):
    """Neither half is legal alone: the pair holds t_2 and the single wants it."""
    pair = ok(joined.ada.post("/reservations", json={
        "restaurant_id": "r_anker", "table_ids": ["t_1", "t_2"],
        "starts_at_local": joined.at("19:00"), "party_size": 6}, key=new_key()), 201)
    single = ok(joined.ada.book(table_id="t_3", party_size=2,
                                starts_at_local=joined.at("19:00")), 201)
    failed(joined.ada.patch(f"/reservations/{single['reference']}",
                            json={"table_id": "t_2"}), 409, "table_unavailable")

    result = ok(joined.ada.moves([
        {"reference": pair["reference"], "table_ids": ["t_2", "t_3"]},
        {"reference": single["reference"], "table_id": "t_1", "party_size": 2}]), 201)
    assert [r["table_ids"] for r in result["reservations"]] == \
        [["t_2", "t_3"], ["t_1"]]


def test_a_batch_whose_results_share_a_table_is_refused(joined):
    first = ok(joined.ada.book(table_id="t_1", party_size=2,
                               starts_at_local=joined.at("19:00")), 201)
    second = ok(joined.ada.book(table_id="t_3", party_size=2,
                                starts_at_local=joined.at("19:00")), 201)
    failed(joined.ada.moves([
        {"reference": first["reference"], "table_ids": ["t_1", "t_2"], "party_size": 6},
        {"reference": second["reference"], "table_ids": ["t_2", "t_3"], "party_size": 6}]),
        409, "table_unavailable")
    assert ok(joined.ada.get(f"/reservations/{first['reference']}"))["table_ids"] == ["t_1"]
    assert ok(joined.ada.get(f"/reservations/{second['reference']}"))["table_ids"] == ["t_3"]


def test_a_batch_onto_an_undeclared_pair_is_refused(joined):
    booking = ok(joined.ada.book(table_id="t_1", party_size=2,
                                 starts_at_local=joined.at("19:00")), 201)
    failed(joined.ada.moves([{"reference": booking["reference"],
                              "table_ids": ["t_1", "t_3"]}]),
           422, "combination_not_allowed")


def test_a_batch_replay_returns_the_original_combination(joined):
    booking = ok(joined.ada.book(table_id="t_1", party_size=2,
                                 starts_at_local=joined.at("19:00")), 201)
    key = new_key()
    batch = [{"reference": booking["reference"], "table_ids": ["t_1", "t_2"],
              "party_size": 6}]
    created = ok(joined.ada.moves(batch, key=key), 201)
    ok(joined.ada.post(f"/reservations/{booking['reference']}/cancel"))
    assert ok(joined.ada.moves(batch, key=key), 200) == created


# ---- seeded combinations -------------------------------------------------

def test_a_seeded_combination_holds_both_tables(api, reset):
    date = fx.booking_date()
    seeded = {"id": "res_seed", "reference": "SEED01", "user_id": "u_ada",
              "restaurant_id": "r_anker", "table_ids": ["t_1", "t_2"],
              "starts_at_local": fx.local(date, "19:00"), "party_size": 6}
    reset(fx.fixture(restaurants=[fx.combinable(PAIRS)], reservations=[seeded]))
    ada = api().authenticated(ADA["email"], ADA["password"])
    bob = api().authenticated(fx.BOB["email"], fx.BOB["password"])

    booking = ok(ada.get("/reservations/SEED01"))
    assert booking["table_ids"] == ["t_1", "t_2"]
    failed(bob.book(table_id="t_2", starts_at_local=fx.local(date, "19:00")),
           409, "table_unavailable")
    ok(bob.book(table_id="t_3", party_size=2,
                starts_at_local=fx.local(date, "19:00")), 201)


def test_a_seeded_cancelled_combination_holds_nothing(api, reset):
    date = fx.booking_date()
    seeded = {"id": "res_seed", "reference": "SEED01", "user_id": "u_ada",
              "restaurant_id": "r_anker", "table_ids": ["t_1", "t_2"],
              "starts_at_local": fx.local(date, "19:00"), "party_size": 6,
              "status": "cancelled"}
    reset(fx.fixture(restaurants=[fx.combinable(PAIRS)], reservations=[seeded]))
    bob = api().authenticated(fx.BOB["email"], fx.BOB["password"])
    ok(bob.post("/reservations", json={
        "restaurant_id": "r_anker", "table_ids": ["t_1", "t_2"],
        "starts_at_local": fx.local(date, "19:00"), "party_size": 6},
        key=new_key()), 201)


@pytest.mark.parametrize("pairs", [
    [["t_1", "t_2", "t_3"]],
    [["t_1"]],
    [["t_1", "t_1"]],
    [["t_1", "t_nope"]],
])
def test_an_impossible_combinable_declaration_is_refused(reset, pairs):
    failed(reset(fx.fixture(restaurants=[fx.combinable(pairs)]), raw=True),
           422, "validation_failed")


# ---- under load ----------------------------------------------------------

def test_fifty_clients_racing_for_a_pair_and_its_members(pool, reset):
    """Overlapping seatings are mutually exclusive however they are spelled.

    t_1, t_2 and the pair t_1+t_2 all contend for the same tables, so at most two
    bookings can stand: the single t_1 and the single t_2, or the pair alone.
    """
    users = [dict(ADA, id=f"u_{i}", email=f"diner{i}@example.com") for i in range(50)]
    reset(fx.fixture(users=users, restaurants=[fx.combinable([["t_1", "t_2"]])]))
    clients = [Api(pool).authenticated(user["email"], user["password"]) for user in users]
    date = fx.booking_date()
    shapes = [{"table_id": "t_1", "party_size": 2},
              {"table_id": "t_2", "party_size": 4},
              {"table_ids": ["t_1", "t_2"], "party_size": 6}]

    responses = burst(lambda i: clients[i].post("/reservations", json={
        "restaurant_id": "r_anker", "starts_at_local": fx.local(date, "19:00"),
        **shapes[i % 3]}, key=new_key()), 50)

    no_5xx(responses)
    counts = tally(responses)
    won = [resp.json() for resp in responses if resp.status_code == 201]
    assert counts.get(409) == 50 - len(won), counts
    held = [table for booking in won for table in booking["table_ids"]]
    assert len(held) == len(set(held)), f"a table was sold twice: {held}"
    assert len(won) in (1, 2), [b["table_ids"] for b in won]
    assert_no_double_booking(clients)
