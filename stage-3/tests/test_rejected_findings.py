"""The defects a review found in stage 1, pinned so they cannot come back.

Each test names the finding it covers and asserts the behaviour the requirements
ask for, not merely that the old symptom is gone.
"""
from __future__ import annotations

import copy
import datetime as dt
import time

import conftest as fx
from conftest import ADA, BOB, Api, failed, new_key, ok
import httpx
import pytest

BERLIN_SPRING = "2026-03-29"
NY_FALL = "2026-11-01"


# ---- F1: an import must refuse a state this service could not have made ----

@pytest.fixture
def furnished(world):
    """A destination with something to lose: an account, a session and a booking."""
    world.booking = ok(world.ada.book(table_id="t_2",
                                      starts_at_local=world.at("19:00")), 201)
    return world


def assert_destination_intact(world):
    assert [r["id"] for r in ok(world.anon.get("/restaurants"))["restaurants"]] == \
        [world.rid], "the restaurants are still there"
    assert ok(world.anon.get("/reservations", token=world.ada.token))["reservations"], \
        "the session still works and the booking is still there"
    assert ok(world.ada.get(f"/reservations/{world.booking['reference']}")) == \
        world.booking


@pytest.mark.parametrize("state", [{}, {"bogus": 1}, {"users": []}],
                         ids=["empty", "unrecognisable", "partial"])
def test_an_import_of_a_state_that_is_not_one_is_refused(furnished, state):
    """§10: an invalid state is 422 and does not change the destination.

    An empty object is not an empty service -- it is a payload that says nothing.
    Accepting it destroyed the destination's accounts, tokens and bookings while
    answering 204.
    """
    resp = furnished.anon.post("/_test/import", json={
        "track": "tablekeeper", "format_version": 1, "state": state},
        timeout=fx.CONTROL_TIMEOUT)
    failed(resp, 422, "validation_failed")
    assert_destination_intact(furnished)


@pytest.mark.parametrize("member", ["users", "tokens", "restaurants",
                                    "reservations", "receipts"])
def test_an_import_missing_any_member_of_a_real_export_is_refused(furnished, member):
    snapshot = ok(furnished.anon.get("/_test/export", timeout=fx.CONTROL_TIMEOUT))
    broken = copy.deepcopy(snapshot)
    del broken["state"][member]
    resp = furnished.anon.post("/_test/import", json=broken, timeout=fx.CONTROL_TIMEOUT)
    failed(resp, 422, "validation_failed")
    assert_destination_intact(furnished)


def test_an_import_with_a_member_of_the_wrong_kind_is_refused(furnished):
    snapshot = ok(furnished.anon.get("/_test/export", timeout=fx.CONTROL_TIMEOUT))
    for member, wrong in (("users", {}), ("tokens", []), ("restaurants", "none"),
                          ("reservations", 7), ("receipts", "none")):
        broken = copy.deepcopy(snapshot)
        broken["state"][member] = wrong
        resp = furnished.anon.post("/_test/import", json=broken,
                                   timeout=fx.CONTROL_TIMEOUT)
        failed(resp, 422, "validation_failed")
    assert_destination_intact(furnished)


def test_an_import_carrying_an_unknown_member_is_refused(furnished):
    snapshot = ok(furnished.anon.get("/_test/export", timeout=fx.CONTROL_TIMEOUT))
    broken = copy.deepcopy(snapshot)
    broken["state"]["surprise"] = {"nope": True}
    resp = furnished.anon.post("/_test/import", json=broken, timeout=fx.CONTROL_TIMEOUT)
    failed(resp, 422, "validation_failed")
    assert_destination_intact(furnished)


def test_an_empty_service_still_exports_and_imports(world):
    """The other half of the rule: a state that really is empty is legitimate, and
    the strictness must not refuse this service's own export of it."""
    world.anon.post("/_test/reset", json={"users": [], "restaurants": [],
                                          "reservations": []},
                    timeout=fx.CONTROL_TIMEOUT)
    snapshot = ok(world.anon.get("/_test/export", timeout=fx.CONTROL_TIMEOUT))
    assert snapshot["state"]["users"] == [] and snapshot["state"]["tokens"] == {}
    resp = world.anon.post("/_test/import", json=snapshot, timeout=fx.CONTROL_TIMEOUT)
    assert resp.status_code == 204, fx.described(resp)
    assert ok(world.anon.get("/restaurants")) == {"restaurants": []}


# ---- F2: closing time is an instant, not a wall-clock reading --------------

@pytest.fixture
def transition(api, reset):
    """A restaurant open across a transition, with the closing time as the subject."""
    def _make(timezone, opens, closes, duration=90, slot=30):
        rest = fx.restaurant(timezone=timezone, slot_minutes=slot,
                             reservation_duration_minutes=duration,
                             opening_hours=fx.all_week(opens, closes))
        reset(fx.fixture(restaurants=[rest]))
        return api().authenticated(ADA["email"], ADA["password"]), api()
    return _make


def times_on(client, date):
    return [slot["starts_at_local"].split("T")[1] for slot in ok(client.get(
        "/availability", params={"restaurant_id": "r_anker", "date": date,
                                 "party_size": 4}))["slots"]]


def test_a_booking_whose_real_end_is_after_closing_is_refused(transition):
    """§8 and §9 together: 90 minutes from 01:30 on the spring-forward night ends at
    04:00 by the clock, and a restaurant that closes at 03:30 is shut by then.

    Counting wall-clock minutes makes 01:30 + 90 read 03:00 and sells a table the
    restaurant cannot seat.
    """
    ada, anon = transition("Europe/Berlin", "00:00", "03:30")
    failed(ada.book(table_id="t_2", starts_at_local=f"{BERLIN_SPRING}T01:30"),
           422, "outside_opening_hours")
    assert times_on(anon, BERLIN_SPRING) == ["00:00", "00:30", "01:00"]
    # 01:00 is the last slot that really ends by 03:30: 01:00 CET + 90 real minutes
    # is 03:30 CEST exactly, and the half-open rule lets it stand.
    booking = ok(ada.book(table_id="t_2", starts_at_local=f"{BERLIN_SPRING}T01:00"), 201)
    assert booking["ends_at"] == f"{BERLIN_SPRING}T03:30:00+02:00"


def test_a_booking_whose_real_end_is_before_closing_is_allowed(transition):
    """The same rule in the other direction: 90 minutes from 01:30 on the fall-back
    night in New York ends at 01:30 EST -- before a 02:30 close -- so it must be
    bookable and must appear in availability. Wall-clock minutes make it read 03:00
    and refuse a table the restaurant can seat.
    """
    ada, anon = transition("America/New_York", "00:00", "02:30")
    booking = ok(ada.book(table_id="t_2", starts_at_local=f"{NY_FALL}T01:30"), 201)
    assert booking["starts_at"] == f"{NY_FALL}T01:30:00-04:00"
    # 01:30 EDT is 05:30Z; 90 real minutes later is 07:00Z, which reads 02:00 EST.
    assert booking["ends_at"] == f"{NY_FALL}T02:00:00-05:00"
    assert times_on(anon, NY_FALL) == ["00:00", "00:30", "01:00", "01:30"]


def test_the_same_rule_applies_to_an_amendment_and_to_a_batch(transition):
    """The amended booking is in the future, so its cutoff is clear and the only thing
    left to object to is where the amendment would end.

    New York closes at 02:30 on the fall-back night. 02:00 EST is 07:00Z and 90 real
    minutes later is 08:30Z, half an hour past closing, so the amendment is refused --
    through PATCH and through a batch alike.
    """
    ada, _ = transition("America/New_York", "00:00", "02:30")
    booking = ok(ada.book(table_id="t_2", starts_at_local=f"{NY_FALL}T00:30"), 201)
    failed(ada.patch(f"/reservations/{booking['reference']}",
                     json={"starts_at_local": f"{NY_FALL}T02:00"}),
           422, "outside_opening_hours")
    failed(ada.moves([{"reference": booking["reference"],
                       "starts_at_local": f"{NY_FALL}T02:00"}]),
           422, "outside_opening_hours")
    assert ok(ada.get(f"/reservations/{booking['reference']}"))["starts_at_local"] ==         f"{NY_FALL}T00:30"
    # And the amendment that does end in time is allowed.
    ok(ada.patch(f"/reservations/{booking['reference']}",
                 json={"starts_at_local": f"{NY_FALL}T01:00"}))


def test_a_closing_time_inside_the_skipped_hour_still_closes(transition):
    """A restaurant may declare a closing time that the clock never reads. The
    moment it closes is then the transition itself, which is 01:00 UTC that night:
    00:30 CET plus 90 minutes is exactly that, and 01:00 CET plus 90 is past it.
    """
    ada, anon = transition("Europe/Berlin", "00:00", "02:30")
    booking = ok(ada.book(table_id="t_2", starts_at_local=f"{BERLIN_SPRING}T00:30"), 201)
    assert booking["ends_at"] == f"{BERLIN_SPRING}T03:00:00+02:00"
    failed(ada.book(table_id="t_3", starts_at_local=f"{BERLIN_SPRING}T01:00"),
           422, "outside_opening_hours")
    assert times_on(anon, BERLIN_SPRING) == ["00:00", "00:30"]


def test_an_ordinary_day_is_unaffected(transition):
    """The rule has to be invisible when no transition is involved."""
    ada, anon = transition("Europe/Berlin", "18:00", "23:00")
    date = fx.booking_date()
    assert times_on(anon, date) == ["18:00", "18:30", "19:00", "19:30", "20:00",
                                    "20:30", "21:00", "21:30"]
    ok(ada.book(table_id="t_2", starts_at_local=fx.local(date, "21:30")), 201)
    failed(ada.book(table_id="t_3", starts_at_local=fx.local(date, "22:00")),
           422, "outside_opening_hours")


# ---- F3: the caller is established before the key is looked at ------------

@pytest.mark.parametrize("path,body", [
    ("/reservations", {"restaurant_id": "r_anker", "table_id": "t_2",
                       "party_size": 4}),
    ("/reservation-moves", {"moves": [{"reference": "ABC123"}]}),
])
@pytest.mark.parametrize("key", [None, "", "k" * 300, "fine"],
                         ids=["no key", "empty key", "overlong key", "valid key"])
def test_an_unknown_token_is_401_whatever_the_key_says(world, path, body, key):
    """§6 makes an unknown token a 401, and §7 resolves idempotency only after the
    caller is authenticated. A stranger must not be able to tell a missing header
    from an over-long one -- and must certainly not be told their key is the problem.
    """
    if "restaurant_id" in body:
        body = dict(body, starts_at_local=world.at("19:00"))
    resp = world.anon.post(path, json=body, token="not-a-real-token",
                           key=key if key else None)
    failed(resp, 401, "unauthenticated")


@pytest.mark.parametrize("path,body", [
    ("/reservations", {"table_id": "t_2", "party_size": 4}),
    ("/reservation-moves", {"moves": [{"reference": "ABC123"}]}),
])
def test_a_real_caller_still_gets_the_key_rules(world, path, body):
    """The ordering must not swallow the key rules for a caller who is known."""
    if "table_id" in body:
        body = dict(body, restaurant_id=world.rid, starts_at_local=world.at("19:00"))
    failed(world.ada.post(path, json=body), 400, "missing_idempotency_key")
    failed(world.ada.post(path, json=body, key="k" * 256), 422, "validation_failed")
    # And a key at the stated limit is accepted, so the rule is a bound and not a ban.
    accepted = world.ada.post(path, json=body, key="k" * 255)
    assert accepted.status_code in (201, 404), fx.described(accepted)


# ---- the smaller findings -------------------------------------------------

def test_a_malformed_field_is_answered_before_a_missing_resource(world):
    """Field formats are settled from the body alone, so they are answered first:
    a 404 about the restaurant would be a confusing answer to a request that was
    never well-formed. Documented in RUN.md, and now pinned here."""
    failed(world.ada.post("/reservations", json={
        "restaurant_id": "r_nope", "table_id": "t_2",
        "starts_at_local": "garbage", "party_size": 4}, key=new_key()),
        422, "validation_failed")
    failed(world.ada.post("/reservations", json={
        "restaurant_id": "r_nope", "table_id": "t_2",
        "starts_at_local": world.at("19:00"), "party_size": 0}, key=new_key()),
        422, "validation_failed")
    # And a well-formed request about something that is not there is still a 404.
    failed(world.ada.post("/reservations", json={
        "restaurant_id": "r_nope", "table_id": "t_2",
        "starts_at_local": world.at("19:00"), "party_size": 4}, key=new_key()),
        404, "not_found")


@pytest.mark.parametrize("created_at", [
    "2026-10-01T10:00:00Z",
    "2026-10-01T10:00:00+00:00",
    "2026-10-01T12:00:00+02:00",
    "2026-10-01T10:00:00.250Z",
])
def test_a_seeded_timestamp_in_any_rfc_3339_spelling_is_accepted(api, reset, created_at):
    """A fixture that writes Z is writing valid RFC 3339. Refusing it failed the
    whole reset, which costs every check after it."""
    date = fx.booking_date()
    seeded = {"id": "res_seed", "reference": "SEED01", "user_id": "u_ada",
              "restaurant_id": "r_anker", "table_id": "t_2",
              "starts_at_local": fx.local(date, "19:00"), "party_size": 4,
              "created_at": created_at}
    reset(fx.fixture(reservations=[seeded]))
    ada = api().authenticated(ADA["email"], ADA["password"])
    booking = ok(ada.get("/reservations/SEED01"))
    # §3.4: a response carries an explicit numeric offset, whatever the fixture used.
    assert booking["created_at"].endswith(("+00:00", "+02:00")), booking["created_at"]
    assert dt.datetime.fromisoformat(booking["created_at"]) == \
        dt.datetime.fromisoformat(created_at.replace("Z", "+00:00"))


def test_seeding_many_accounts_stays_inside_the_reset_budget(pool, reset):
    """§2 gives reset 10 seconds. Hashing every seeded password at a fixed cost grew
    past that as fixtures grew; the work factor now scales with the fixture.

    The assertion is the budget itself, with a margin, and the accounts must still
    be able to log in with the password they were seeded with.
    """
    for count in (300, 500):
        users = [dict(ADA, id=f"u_{i}", email=f"diner{i}@example.com")
                 for i in range(count)]
        started = time.monotonic()
        reset(fx.fixture(users=users))
        elapsed = time.monotonic() - started
        assert elapsed < 7.0, f"{count} seeded accounts took {elapsed:.1f}s of 10"
        ok(Api(pool).login(users[0]["email"], ADA["password"]))
        ok(Api(pool).login(users[-1]["email"], ADA["password"]))
        failed(Api(pool).login(users[0]["email"], "wrong password"),
               401, "unauthenticated")


def test_a_stored_password_is_never_recoverable(world):
    """Whatever the work factor, the stored form is a bcrypt hash and the password
    itself is nowhere in the state."""
    body = world.anon.get("/_test/export", timeout=fx.CONTROL_TIMEOUT).text
    assert ADA["password"] not in body
    snapshot = ok(world.anon.get("/_test/export", timeout=fx.CONTROL_TIMEOUT))
    for account in snapshot["state"]["users"]:
        assert account["password_hash"].startswith("$2"), account["password_hash"]
        assert len(account["password_hash"]) >= 55


def test_a_login_during_a_reset_never_lands_in_another_account(pool, reset):
    """The token is issued to the record whose password was verified, not to
    whatever answers to that id afterwards. Reset between the two and the login
    must fail rather than hand out a session for a different account.
    """
    for _ in range(12):
        reset(fx.fixture(users=[dict(ADA, id="u_shared", display_name="Ada")]))
        results = fx.burst(lambda i: (
            Api(pool).login(ADA["email"], ADA["password"]) if i == 0
            else pool.post("/_test/reset", json=fx.fixture(users=[
                dict(BOB, id="u_shared", display_name="Bob")]),
                timeout=fx.CONTROL_TIMEOUT)), 2)
        login = results[0]
        assert login.status_code in (200, 401), fx.described(login)
        if login.status_code != 200:
            continue
        body = login.json()
        # Whoever the session belongs to, it must be the account that authenticated.
        assert body["display_name"] == "Ada", body
        whoami = pool.get("/reservations",
                          headers={"Authorization": "Bearer " + body["token"]})
        assert whoami.status_code in (200, 401), fx.described(whoami)


# ---- the upgrade from stage 1 --------------------------------------------

def test_a_stage_1_export_imports_and_keeps_working(previous_service, world, api):
    """§ existing clients after an upgrade, at the API level.

    The stage-1 service is driven through a booking, a lost-response retry key and a
    session; its export is then imported here and every one of those must still be
    good. Run with --previous-base-url pointing at this team's stage-1 service.
    """
    date = fx.booking_date()
    fixture = fx.fixture()
    assert previous_service.post("/_test/reset", json=fixture,
                                 timeout=fx.CONTROL_TIMEOUT).status_code == 204
    older = previous_service
    older.authenticated(ADA["email"], ADA["password"])
    stage1_token = older.token

    booking = ok(older.post("/reservations", json={
        "restaurant_id": "r_anker", "table_id": "t_2",
        "starts_at_local": fx.local(date, "19:00"), "party_size": 4},
        key=new_key()), 201)
    # A request whose response the client never saw: the receipt exists, the client
    # still holds the key and the body.
    pending_key = new_key()
    pending_body = {"restaurant_id": "r_anker", "table_id": "t_3",
                    "starts_at_local": fx.local(date, "19:00"), "party_size": 4}
    pending = ok(older.post("/reservations", json=pending_body, key=pending_key), 201)
    snapshot = ok(older.get("/_test/export", timeout=fx.CONTROL_TIMEOUT))
    assert snapshot["track"] == "tablekeeper" and snapshot["format_version"] == 1

    resp = world.anon.post("/_test/import", json=snapshot, timeout=fx.CONTROL_TIMEOUT)
    assert resp.status_code == 204, fx.described(resp)

    # The session still works here.
    assert ok(world.anon.get("/reservations", token=stage1_token))["reservations"]
    # The reference still works, with stage 2's shape added and nothing regenerated.
    restored = ok(world.anon.get(f"/reservations/{booking['reference']}",
                                 token=stage1_token))
    assert restored["reservation_id"] == booking["reservation_id"]
    assert restored["created_at"] == booking["created_at"]
    assert restored["starts_at"] == booking["starts_at"]
    assert restored["table_id"] == "t_2" and restored["table_ids"] == ["t_2"]
    # The pending retry settles as a replay of the original response.
    replay = ok(world.anon.post("/reservations", json=pending_body, key=pending_key,
                                token=stage1_token), 200)
    assert replay["reference"] == pending["reference"]
    assert ok(world.anon.post("/auth/login", json={
        "email": ADA["email"], "password": ADA["password"]}, token=None))["user_id"] == \
        "u_ada"
