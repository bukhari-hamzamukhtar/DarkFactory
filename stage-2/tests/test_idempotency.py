"""§7: what a retry means, and in what order it is decided."""
from __future__ import annotations

import json

import conftest as fx
from conftest import ADA, failed, new_key, ok


def test_first_use_is_201_and_a_replay_is_200_with_the_same_body(world):
    key = new_key()
    created = ok(world.ada.book(starts_at_local=world.at("19:00"), key=key), 201)
    replay = ok(world.ada.book(starts_at_local=world.at("19:00"), key=key), 200)
    assert replay == created


def test_idempotency_is_resolved_before_field_validation(world):
    """§7 is explicit: a used key with a different body is 409 *even when that body
    would otherwise be invalid*. The key decides before the fields are looked at."""
    key = new_key()
    ok(world.ada.book(starts_at_local=world.at("19:00"), key=key), 201)
    for invalid in ({"party_size": 0}, {"party_size": "four"},
                    {"starts_at_local": "not-a-time"}, {"table_id": "t_nope"},
                    {"restaurant_id": "r_nope"}):
        body = {"restaurant_id": world.rid, "table_id": "t_2",
                "starts_at_local": world.at("19:00"), "party_size": 4, **invalid}
        resp = world.ada.post("/reservations", json=body, key=key)
        failed(resp, 409, "idempotency_key_reuse")


def test_idempotency_is_resolved_after_authentication(world):
    """The ordering runs the other way for credentials: §7 resolves the key only
    after the caller is authenticated, so a bad token is 401 and not a replay."""
    key = new_key()
    ok(world.ada.book(starts_at_local=world.at("19:00"), key=key), 201)
    body = {"restaurant_id": world.rid, "table_id": "t_2",
            "starts_at_local": world.at("19:00"), "party_size": 4}
    failed(world.ada.post("/reservations", json=body, key=key,
                          token="not-a-real-token"), 401, "unauthenticated")


def test_a_reused_key_after_a_4xx_is_a_first_use(world):
    key = new_key()
    failed(world.ada.book(starts_at_local=world.at("19:00"), party_size=0, key=key),
           422, "validation_failed")
    ok(world.ada.book(starts_at_local=world.at("19:00"), key=key), 201)


def test_a_reused_key_after_a_409_is_a_first_use(world):
    """A rejected booking never spent the key, whatever the reason for the 4xx."""
    ok(world.bob.book(table_id="t_2", starts_at_local=world.at("19:00")), 201)
    key = new_key()
    failed(world.ada.book(table_id="t_2", starts_at_local=world.at("19:00"), key=key),
           409, "table_unavailable")
    ok(world.ada.book(table_id="t_3", starts_at_local=world.at("19:00"), key=key), 201)


def test_the_same_key_on_a_different_path_is_a_different_request(world):
    """§7: same key, different path -- not a replay, and it must simply work."""
    key = "one-key-two-paths"
    created = ok(world.ada.book(table_id="t_2", starts_at_local=world.at("19:00"),
                                key=key), 201)
    moved = ok(world.ada.moves([{"reference": created["reference"], "table_id": "t_3"}],
                               key=key), 201)
    assert moved["reservations"][0]["table_id"] == "t_3"
    # And each path keeps its own replay.
    assert ok(world.ada.book(table_id="t_2", starts_at_local=world.at("19:00"),
                             key=key), 200) == created
    assert ok(world.ada.moves([{"reference": created["reference"], "table_id": "t_3"}],
                              key=key), 200) == moved


def test_a_key_is_scoped_to_the_user(world):
    key = "a-shared-key"
    ok(world.ada.book(table_id="t_2", starts_at_local=world.at("19:00"), key=key), 201)
    ok(world.bob.book(table_id="t_3", starts_at_local=world.at("19:00"), key=key), 201)
    mine = ok(world.ada.get("/reservations"))["reservations"]
    assert [r["table_id"] for r in mine] == ["t_2"]


def test_key_order_and_whitespace_do_not_make_a_different_body(world):
    key = new_key()
    created = ok(world.ada.book(table_id="t_2", starts_at_local=world.at("19:00"),
                                key=key), 201)
    shuffled = json.dumps({
        "party_size": 4,
        "starts_at_local": world.at("19:00"),
        "table_id": "t_2",
        "restaurant_id": world.rid,
    }, indent=4)
    replay = ok(world.ada.post("/reservations", content=shuffled, key=key), 200)
    assert replay == created


def test_an_extra_body_field_makes_a_different_body(world):
    """§3.4 ignores unknown fields when acting on a request; §7 compares the body as
    a JSON *value*, and a value with another member is a different value. So the
    booking is still made from the known fields, and a retry that adds a field is a
    reuse rather than a replay."""
    key = new_key()
    ok(world.ada.book(table_id="t_2", starts_at_local=world.at("19:00"), key=key,
                      souvenir="yes"), 201)
    failed(world.ada.book(table_id="t_2", starts_at_local=world.at("19:00"), key=key),
           409, "idempotency_key_reuse")


def test_a_replay_returns_the_original_after_the_booking_has_moved_on(world):
    key = new_key()
    created = ok(world.ada.book(table_id="t_2", starts_at_local=world.at("19:00"),
                                key=key), 201)
    ok(world.ada.patch(f"/reservations/{created['reference']}",
                       json={"table_id": "t_3", "party_size": 2}))
    replay = ok(world.ada.book(table_id="t_2", starts_at_local=world.at("19:00"),
                               key=key), 200)
    assert replay == created, "the response is the original one, not the current state"
    current = ok(world.ada.get(f"/reservations/{created['reference']}"))
    assert current["table_id"] == "t_3" and current["party_size"] == 2, \
        "and the replay changed nothing"


def test_a_replay_after_cancellation_makes_no_further_change(world):
    key = new_key()
    created = ok(world.ada.book(table_id="t_2", starts_at_local=world.at("19:00"),
                                key=key), 201)
    ok(world.ada.post(f"/reservations/{created['reference']}/cancel"))
    before = world.slots(world.anon)
    replay = ok(world.ada.book(table_id="t_2", starts_at_local=world.at("19:00"),
                               key=key), 200)
    assert replay == created and replay["status"] == "confirmed"
    assert ok(world.ada.get(f"/reservations/{created['reference']}"))["status"] == \
        "cancelled", "the booking stays cancelled"
    assert world.slots(world.anon) == before, "and the table stays free"
    assert len(ok(world.ada.get("/reservations"))["reservations"]) == 1


def test_a_missing_or_empty_key_is_400_on_both_write_paths(world):
    created = ok(world.ada.book(starts_at_local=world.at("19:00")), 201)
    failed(world.ada.post("/reservations", json={
        "restaurant_id": world.rid, "table_id": "t_3",
        "starts_at_local": world.at("19:00"), "party_size": 4}),
        400, "missing_idempotency_key")
    failed(world.ada.post("/reservations", json={
        "restaurant_id": world.rid, "table_id": "t_3",
        "starts_at_local": world.at("19:00"), "party_size": 4}, key=""),
        400, "missing_idempotency_key")
    failed(world.ada.post("/reservation-moves", json={
        "moves": [{"reference": created["reference"], "table_id": "t_3"}]}),
        400, "missing_idempotency_key")


def test_a_key_of_255_characters_is_accepted_and_256_is_not(world):
    ok(world.ada.book(table_id="t_2", starts_at_local=world.at("19:00"),
                      key="k" * 255), 201)
    failed(world.ada.book(table_id="t_3", starts_at_local=world.at("19:00"),
                          key="k" * 256), 422, "validation_failed")


def test_a_missing_key_is_decided_before_the_body_is_validated(world):
    """Both are 4xx; the point is that neither path ever 5xxs and the key rule is
    not skipped just because the body is also wrong."""
    resp = world.ada.post("/reservations", json={"party_size": 0})
    failed(resp, 400, "missing_idempotency_key")


def test_a_malformed_body_is_decided_before_the_key(world):
    """§7 parses the body first, so an unparseable body is malformed rather than a
    missing-key complaint."""
    failed(world.ada.post("/reservations", content="{not json"),
           400, "malformed_request")


def test_two_keys_for_the_same_booking_both_take_effect(world):
    """Nothing in §7 deduplicates by content: two different keys are two requests,
    and the second one collides on the table."""
    ok(world.ada.book(table_id="t_2", starts_at_local=world.at("19:00"),
                      key=new_key()), 201)
    failed(world.ada.book(table_id="t_2", starts_at_local=world.at("19:00"),
                          key=new_key()), 409, "table_unavailable")


def test_replays_survive_a_later_reset_only_as_far_as_the_state_does(api, reset):
    """A reset replaces all state (§3.3), receipts included, so the same key is a
    first use again afterwards."""
    reset(fx.fixture())
    ada = api().authenticated(ADA["email"], ADA["password"])
    date = fx.booking_date()
    key = new_key()
    ok(ada.book(starts_at_local=fx.local(date, "19:00"), key=key), 201)
    reset(fx.fixture())
    ada = api().authenticated(ADA["email"], ADA["password"])
    ok(ada.book(starts_at_local=fx.local(date, "19:00"), key=key), 201)
