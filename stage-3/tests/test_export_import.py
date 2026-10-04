"""§10: export and import, and what must survive the round trip."""
from __future__ import annotations

import copy

import conftest as fx
from conftest import ADA, BOB, Api, failed, new_key, ok
import httpx
import pytest


def export(client) -> dict:
    return ok(client.get("/_test/export", timeout=fx.CONTROL_TIMEOUT))


def do_import(client, snapshot, *, expect=204):
    resp = client.post("/_test/import", json=snapshot, timeout=fx.CONTROL_TIMEOUT)
    assert resp.status_code == expect, fx.described(resp)
    return resp


def test_export_has_the_documented_envelope(world):
    snapshot = export(world.anon)
    assert snapshot["track"] == "tablekeeper"
    assert snapshot["format_version"] == 1
    assert isinstance(snapshot["state"], dict)


def test_export_and_import_need_no_authentication(world):
    snapshot = export(world.anon)
    do_import(world.anon, snapshot)


def test_a_round_trip_restores_bookings_identically(world):
    booking = ok(world.ada.book(table_id="t_2", starts_at_local=world.at("19:00")), 201)
    snapshot = export(world.anon)

    world.anon.post("/_test/reset", json=fx.fixture(), timeout=fx.CONTROL_TIMEOUT)
    do_import(world.anon, snapshot)

    restored = ok(world.ada.get(f"/reservations/{booking['reference']}"))
    assert restored == booking, "identities, statuses and timestamps are not regenerated"


def test_a_round_trip_keeps_existing_bearer_tokens_valid(world):
    """§10: existing bearer tokens must still work afterwards."""
    token = world.ada.token
    ok(world.ada.book(starts_at_local=world.at("19:00")), 201)
    snapshot = export(world.anon)

    world.anon.post("/_test/reset", json=fx.fixture(), timeout=fx.CONTROL_TIMEOUT)
    failed(world.anon.get("/reservations", token=token), 401, "unauthenticated")

    do_import(world.anon, snapshot)
    assert len(ok(world.anon.get("/reservations", token=token))["reservations"]) == 1


def test_a_round_trip_keeps_hashed_password_login_working(world, api):
    snapshot = export(world.anon)
    world.anon.post("/_test/reset", json=fx.fixture(users=[BOB]),
                    timeout=fx.CONTROL_TIMEOUT)
    do_import(world.anon, snapshot)
    ok(api().login(ADA["email"], ADA["password"]))


def test_a_round_trip_keeps_idempotency_receipts(world):
    """§10: all completed idempotent request bodies and original responses."""
    key = new_key()
    created = ok(world.ada.book(table_id="t_2", starts_at_local=world.at("19:00"),
                                key=key), 201)
    snapshot = export(world.anon)
    world.anon.post("/_test/reset", json=fx.fixture(), timeout=fx.CONTROL_TIMEOUT)
    do_import(world.anon, snapshot)

    replay = ok(world.ada.book(table_id="t_2", starts_at_local=world.at("19:00"),
                               key=key), 200)
    assert replay == created
    assert len(ok(world.ada.get("/reservations"))["reservations"]) == 1, \
        "the replay after import must not book a second table"
    failed(world.ada.book(table_id="t_2", starts_at_local=world.at("19:00"),
                          party_size=2, key=key), 409, "idempotency_key_reuse")


def test_a_round_trip_keeps_batch_receipts(world):
    """§10: export/import preserves successful batch receipts as well."""
    booking = ok(world.ada.book(table_id="t_2", starts_at_local=world.at("19:00")), 201)
    key = new_key()
    batch = [{"reference": booking["reference"], "table_id": "t_3"}]
    created = ok(world.ada.moves(batch, key=key), 201)
    snapshot = export(world.anon)
    world.anon.post("/_test/reset", json=fx.fixture(), timeout=fx.CONTROL_TIMEOUT)
    do_import(world.anon, snapshot)

    assert ok(world.ada.moves(batch, key=key), 200) == created
    assert ok(world.ada.get(f"/reservations/{booking['reference']}"))["table_id"] == "t_3"


def test_a_round_trip_keeps_failed_keys_reusable(world):
    """§10: failed request keys remain reusable."""
    key = new_key()
    failed(world.ada.book(starts_at_local=world.at("19:00"), party_size=0, key=key),
           422, "validation_failed")
    snapshot = export(world.anon)
    world.anon.post("/_test/reset", json=fx.fixture(), timeout=fx.CONTROL_TIMEOUT)
    do_import(world.anon, snapshot)
    ok(world.ada.book(starts_at_local=world.at("19:00"), key=key), 201)


def test_a_round_trip_keeps_the_fixture_configuration(world):
    before = ok(world.anon.get(f"/restaurants/{world.rid}"))
    snapshot = export(world.anon)
    world.anon.post("/_test/reset", json=fx.fixture(
        restaurants=[fx.restaurant("r_other", name="Other")]),
        timeout=fx.CONTROL_TIMEOUT)
    do_import(world.anon, snapshot)
    assert ok(world.anon.get(f"/restaurants/{world.rid}")) == before
    assert [r["id"] for r in ok(world.anon.get("/restaurants"))["restaurants"]] == \
        [world.rid]


def test_import_replaces_and_does_not_merge(world, api):
    snapshot = export(world.anon)
    ok(world.ada.book(starts_at_local=world.at("19:00")), 201)
    newcomer = ok(world.anon.signup("carol@example.com", "correct horse", "Carol"), 201)

    do_import(world.anon, snapshot)

    assert ok(world.ada.get("/reservations"))["reservations"] == [], \
        "the destination's later booking is gone"
    failed(world.anon.get("/reservations", token=newcomer["token"]),
           401, "unauthenticated")
    failed(api().login("carol@example.com", "correct horse"), 401, "unauthenticated")


def test_importing_twice_restores_without_duplicating(world):
    ok(world.ada.book(table_id="t_2", starts_at_local=world.at("19:00")), 201)
    snapshot = export(world.anon)
    do_import(world.anon, snapshot)
    first = ok(world.ada.get("/reservations"))["reservations"]
    do_import(world.anon, snapshot)
    second = ok(world.ada.get("/reservations"))["reservations"]
    assert first == second and len(first) == 1
    assert [r["id"] for r in ok(world.anon.get("/restaurants"))["restaurants"]] == \
        [world.rid]


def test_an_export_is_a_snapshot_that_later_writes_do_not_change(world):
    """§10: export is atomic and read-only; subsequent source writes do not change it."""
    first = ok(world.ada.book(table_id="t_2", starts_at_local=world.at("19:00")), 201)
    snapshot = export(world.anon)
    frozen = copy.deepcopy(snapshot)

    later = ok(world.ada.book(table_id="t_3", starts_at_local=world.at("19:00")), 201)
    assert snapshot == frozen, "the exported value must not be live"

    do_import(world.anon, snapshot)
    references = {r["reference"] for r in ok(world.ada.get("/reservations"))["reservations"]}
    assert references == {first["reference"]}, \
        f"the snapshot predates the second booking, got {references}"
    failed(world.ada.get(f"/reservations/{later['reference']}"), 404, "not_found")


def test_reset_clears_imported_state(world, api):
    ok(world.ada.book(starts_at_local=world.at("19:00")), 201)
    snapshot = export(world.anon)
    do_import(world.anon, snapshot)
    world.anon.post("/_test/reset", json=fx.fixture(), timeout=fx.CONTROL_TIMEOUT)
    ada = api().authenticated(ADA["email"], ADA["password"])
    assert ok(ada.get("/reservations"))["reservations"] == []


# ---- rejected imports ----------------------------------------------------

def unchanged(world, reference):
    assert ok(world.ada.get(f"/reservations/{reference}"))["reference"] == reference


@pytest.mark.parametrize("mangle", [
    lambda s: {k: v for k, v in s.items() if k != "track"},
    lambda s: {k: v for k, v in s.items() if k != "format_version"},
    lambda s: {k: v for k, v in s.items() if k != "state"},
    lambda s: {**s, "track": "pocketful"},
    lambda s: {**s, "track": 7},
    lambda s: {**s, "format_version": 2},
    lambda s: {**s, "format_version": "1"},
    lambda s: {**s, "state": "nope"},
    lambda s: {**s, "state": []},
], ids=["no track", "no version", "no state", "wrong track", "track not a string",
        "version 2", "version as string", "state not an object", "state as array"])
def test_a_bad_envelope_is_422_and_changes_nothing(world, mangle):
    booking = ok(world.ada.book(starts_at_local=world.at("19:00")), 201)
    snapshot = export(world.anon)
    resp = world.anon.post("/_test/import", json=mangle(snapshot),
                           timeout=fx.CONTROL_TIMEOUT)
    failed(resp, 422, "validation_failed")
    unchanged(world, booking["reference"])


def test_an_invalid_state_is_422_and_changes_nothing(world):
    booking = ok(world.ada.book(table_id="t_2", starts_at_local=world.at("19:00")), 201)
    snapshot = export(world.anon)
    broken = copy.deepcopy(snapshot)
    # A reference that this service would never mint, so the state could not have
    # come from it.
    reservations = broken["state"]["reservations"]
    assert reservations, "the snapshot should carry the booking"
    reservations[0]["reference"] = "lower01"
    resp = world.anon.post("/_test/import", json=broken, timeout=fx.CONTROL_TIMEOUT)
    failed(resp, 422, "validation_failed")
    unchanged(world, booking["reference"])


def test_a_state_that_double_books_a_table_is_refused(world):
    """The invariant of §1 is a property of the state, not only of the API: a state
    that no sequence of requests could have produced must not be installed."""
    ok(world.ada.book(table_id="t_2", starts_at_local=world.at("19:00")), 201)
    snapshot = export(world.anon)
    doubled = copy.deepcopy(snapshot)
    original = doubled["state"]["reservations"][0]
    clone = dict(original, id="res_clone", reference="CLONE1")
    doubled["state"]["reservations"].append(clone)
    resp = world.anon.post("/_test/import", json=doubled, timeout=fx.CONTROL_TIMEOUT)
    failed(resp, 422, "validation_failed")


def test_malformed_import_json_is_400(world, base_url):
    booking = ok(world.ada.book(starts_at_local=world.at("19:00")), 201)
    resp = httpx.post(f"{base_url}/_test/import", content="{not json",
                      headers={"Content-Type": "application/json"},
                      timeout=fx.CONTROL_TIMEOUT)
    failed(resp, 400, "malformed_request")
    unchanged(world, booking["reference"])


def test_a_malformed_reset_fixture_is_rejected_and_changes_nothing(world):
    booking = ok(world.ada.book(starts_at_local=world.at("19:00")), 201)
    resp = world.anon.post("/_test/reset", json=fx.fixture(
        users=[{**ADA, "id": "u" * 65}]), timeout=fx.CONTROL_TIMEOUT)
    failed(resp, 422, "validation_failed")
    unchanged(world, booking["reference"])


# ---- credentials in the snapshot -----------------------------------------

def test_the_export_carries_hashes_rather_than_passwords(world):
    """§6 forbids plaintext storage, and the export is the whole stored state, so it
    is the place where that can actually be checked from outside."""
    body = world.anon.get("/_test/export", timeout=fx.CONTROL_TIMEOUT).text
    assert ADA["password"] not in body, "a stored password must not be recoverable"
    snapshot = ok(world.anon.get("/_test/export", timeout=fx.CONTROL_TIMEOUT))
    hashes = [u.get("password_hash") for u in snapshot["state"]["users"]]
    assert hashes and all(h.startswith("$2") and len(h) > 50 for h in hashes), hashes


# ---- a second container --------------------------------------------------

def test_a_snapshot_imports_into_a_separate_service(world, second_service):
    """§10: no dependency on the source process, port or address.

    Run with --second-base-url pointing at a second container of this image, which
    the harness starts on a different port.
    """
    other = second_service
    booking = ok(world.ada.book(table_id="t_2", starts_at_local=world.at("19:00")), 201)
    key = new_key()
    ok(world.ada.book(table_id="t_3", starts_at_local=world.at("19:00"), key=key), 201)
    token = world.ada.token
    snapshot = export(world.anon)

    other.post("/_test/reset", json=fx.fixture(users=[BOB]), timeout=fx.CONTROL_TIMEOUT)
    do_import(other, snapshot)
    assert ok(other.get(f"/reservations/{booking['reference']}", token=token)) == booking
    replayed = ok(other.post("/reservations", json={
        "restaurant_id": world.rid, "table_id": "t_3",
        "starts_at_local": world.at("19:00"), "party_size": 4},
        key=key, token=token), 200)
    assert replayed["reference"]
    ok(other.post("/auth/login", json={
        "email": ADA["email"], "password": ADA["password"]}, token=None))
