"""§5 and §6: which error, in which order, and who is allowed in."""
from __future__ import annotations

import datetime as dt

import conftest as fx
from conftest import ADA, failed, new_key, ok
import httpx
import pytest


# ---- the envelope and the conventions ------------------------------------

def test_responses_are_json_with_a_charset(world):
    for resp in (world.anon.get("/restaurants"),
                 world.anon.get("/restaurants/r_nope"),
                 world.ada.get("/reservations")):
        assert resp.headers["content-type"] == "application/json; charset=utf-8", \
            fx.described(resp)


def test_an_unknown_path_is_a_404_with_the_error_envelope(world):
    failed(world.anon.get("/nope"), 404, "not_found")
    failed(world.ada.get("/reservations/ABC123/extra"), 404, "not_found")


def test_an_unoffered_method_is_a_4xx_with_the_error_envelope(world):
    """Nothing in §5 names a code for this, so what matters is that it is a 4xx
    carrying the envelope rather than a crash or a bare framework page."""
    for resp in (world.ada.request("DELETE", "/reservations"),
                 world.ada.request("PUT", f"/reservations/ABC123")):
        assert 400 <= resp.status_code < 500, fx.described(resp)
        assert isinstance(resp.json()["error"]["code"], str), fx.described(resp)


# ---- authentication ------------------------------------------------------

PROTECTED = [
    ("GET", "/reservations"),
    ("GET", "/reservations/ABC123"),
    ("PATCH", "/reservations/ABC123"),
    ("POST", "/reservations/ABC123/cancel"),
]


@pytest.mark.parametrize("method,path", PROTECTED)
# A trailing-space credential cannot be sent by a conforming client, so the
# cases here are the reachable ones: no header, junk, and a plausible-looking
# token that was never issued.
@pytest.mark.parametrize("token", [None, "not-a-real-token", "0" * 48])
def test_protected_endpoints_need_a_real_token(world, method, path, token):
    body = {"party_size": 2} if method == "PATCH" else None
    resp = world.anon.request(method, path, token=token, json=body)
    failed(resp, 401, "unauthenticated")


@pytest.mark.parametrize("header", ["", "Bearer", "Token abc", "bearer",
                                    "Basic YWxhZGRpbjpvcGVu"])
def test_a_malformed_authorization_header_is_401(world, header):
    resp = world.anon.get("/reservations", headers={"Authorization": header},
                          token=None)
    failed(resp, 401, "unauthenticated")


def test_the_bearer_scheme_is_matched_case_insensitively(world):
    """HTTP auth schemes are case-insensitive, so a client that sends `bearer` is
    sending a valid credential."""
    resp = world.anon.get("/reservations", token=None,
                          headers={"Authorization": f"bearer {world.ada.token}"})
    ok(resp)


def test_write_paths_need_a_token_too(world):
    body = {"restaurant_id": world.rid, "table_id": "t_2",
            "starts_at_local": world.at("19:00"), "party_size": 4}
    failed(world.anon.post("/reservations", json=body, key=new_key(), token=None),
           401, "unauthenticated")
    failed(world.anon.post("/reservation-moves", json={"moves": [
        {"reference": "ABC123", "table_id": "t_2"}]}, key=new_key(), token=None),
        401, "unauthenticated")


@pytest.mark.parametrize("path,params", [
    ("/health", None),
    ("/restaurants", None),
    ("/restaurants/r_anker", None),
    ("/availability", {"restaurant_id": "r_anker", "party_size": 2}),
])
def test_the_public_endpoints_need_no_token(world, path, params):
    if params is not None:
        params = dict(params, date=world.date)
    ok(world.anon.get(path, params=params, token=None))


def test_an_account_may_hold_several_concurrent_tokens(world, api):
    second = ok(api().login(ADA["email"], ADA["password"]))["token"]
    assert second != world.ada.token
    ok(world.anon.get("/reservations", token=second))
    ok(world.anon.get("/reservations", token=world.ada.token))


def test_signup_returns_a_usable_token_and_the_display_name(world, api):
    body = ok(world.anon.signup("carol@example.com", "correct horse", "Carol"), 201)
    assert body["display_name"] == "Carol" and body["user_id"] and body["token"]
    assert len(body["user_id"]) <= 64
    ok(api(body["token"]).get("/reservations"))
    ok(api().login("carol@example.com", "correct horse"))


def test_signup_rejections(world):
    failed(world.anon.signup(ADA["email"], "correct horse", "X"), 409, "email_taken")
    failed(world.anon.signup("short@example.com", "1234567", "X"),
           422, "validation_failed")
    ok(world.anon.signup("exactly8@example.com", "12345678", "X"), 201)
    for email in ("not-an-email", "a@", "@example.com", "a b@example.com",
                  "two@at@example.com", ""):
        failed(world.anon.signup(email, "correct horse", "X"),
               422, "validation_failed")


@pytest.mark.parametrize("body,status,code", [
    ({"email": 17, "password": "correct horse", "display_name": "X"},
     400, "malformed_request"),
    ({"email": "a@b.com", "password": True, "display_name": "X"},
     400, "malformed_request"),
    ({"email": "a@b.com", "password": "correct horse", "display_name": []},
     400, "malformed_request"),
    ({"password": "correct horse", "display_name": "X"}, 422, "validation_failed"),
    ({"email": "a@b.com", "display_name": "X"}, 422, "validation_failed"),
    ({"email": "a@b.com", "password": "correct horse"}, 422, "validation_failed"),
])
def test_signup_field_errors(world, body, status, code):
    failed(world.anon.post("/auth/signup", json=body, token=None), status, code)


def test_login_failures_are_401(world):
    failed(world.anon.login(ADA["email"], "wrong password"), 401, "unauthenticated")
    failed(world.anon.login("nobody@example.com", "correct horse"),
           401, "unauthenticated")


@pytest.mark.parametrize("body,status,code", [
    ({"email": 17, "password": "correct horse"}, 400, "malformed_request"),
    ({"email": "a@b.com", "password": 17}, 400, "malformed_request"),
    ({"password": "correct horse"}, 422, "validation_failed"),
    ({"email": "a@b.com"}, 422, "validation_failed"),
])
def test_login_field_errors(world, body, status, code):
    failed(world.anon.post("/auth/login", json=body, token=None), status, code)


def test_a_malformed_auth_body_is_400(world, base_url):
    for path in ("/auth/signup", "/auth/login"):
        resp = httpx.post(f"{base_url}{path}", content="{not json",
                          headers={"Content-Type": "application/json"}, timeout=5)
        failed(resp, 400, "malformed_request")


def test_a_body_that_is_not_an_object_is_400(world, base_url):
    for content in ("[]", '"a string"', "7", "null"):
        resp = httpx.post(f"{base_url}/auth/login", content=content,
                          headers={"Content-Type": "application/json"}, timeout=5)
        failed(resp, 400, "malformed_request")


# ---- creating a booking --------------------------------------------------

def base_body(world):
    return {"restaurant_id": world.rid, "table_id": "t_2",
            "starts_at_local": world.at("19:00"), "party_size": 4}


@pytest.mark.parametrize("field", ["restaurant_id", "table_id", "starts_at_local",
                                   "party_size"])
def test_a_missing_required_field_is_422(world, field):
    body = base_body(world)
    del body[field]
    failed(world.ada.post("/reservations", json=body, key=new_key()),
           422, "validation_failed")


@pytest.mark.parametrize("field,value", [
    ("restaurant_id", 5),
    ("restaurant_id", True),
    ("restaurant_id", {}),
    ("table_id", 5),
    ("table_id", []),
    ("starts_at_local", 5),
    ("starts_at_local", False),
])
def test_a_field_of_the_wrong_json_type_is_400(world, field, value):
    """§5 reserves malformed_request for a body that does not parse or a field of
    the wrong type -- except for the two fields it names explicitly."""
    body = base_body(world)
    body[field] = value
    failed(world.ada.post("/reservations", json=body, key=new_key()),
           400, "malformed_request")


@pytest.mark.parametrize("value", ["4", True, 1.5, 4.0, 0, -1, None,
                                   10 ** 12, "", "four"])
def test_an_invalid_party_size_is_422(world, value):
    """§5 names `party_size` as an endpoint-specific field: every unusable value,
    including a string or a boolean, is a validation failure rather than a 400."""
    body = dict(base_body(world), party_size=value)
    failed(world.ada.post("/reservations", json=body, key=new_key()),
           422, "validation_failed")


@pytest.mark.parametrize("value", [
    "2026-09-24T19:00:00+02:00", "2026-09-24T19:00Z", "2026-09-24T19:00:00",
    "2026-09-24 19:00", "2026-09-24T19:00:00.000", "2026-13-01T19:00",
    "2026-02-30T19:00", "2026-09-24T24:00", "2026-09-24T19:60", "not-a-time", "",
])
def test_an_invalid_starts_at_local_string_is_422(world, value):
    body = dict(base_body(world), starts_at_local=value)
    failed(world.ada.post("/reservations", json=body, key=new_key()),
           422, "validation_failed")


@pytest.mark.parametrize("field", ["restaurant_id", "table_id"])
def test_an_id_longer_than_64_characters_is_422(world, field):
    body = dict(base_body(world))
    body[field] = "x" * 65
    failed(world.ada.post("/reservations", json=body, key=new_key()),
           422, "validation_failed")


def test_an_unknown_restaurant_or_table_is_404(world):
    failed(world.ada.post("/reservations", json=dict(
        base_body(world), restaurant_id="r_nope"), key=new_key()), 404, "not_found")
    failed(world.ada.post("/reservations", json=dict(
        base_body(world), table_id="t_nope"), key=new_key()), 404, "not_found")


def test_a_table_of_another_restaurant_is_404(api, reset):
    other = fx.restaurant("r_other", tables=[{"id": "t_x", "label": "X",
                                              "capacity": 4}])
    reset(fx.fixture(restaurants=[fx.restaurant(), other]))
    ada = api().authenticated(ADA["email"], ADA["password"])
    date = fx.booking_date()
    failed(ada.book(restaurant_id="r_anker", table_id="t_x",
                    starts_at_local=fx.local(date, "19:00")), 404, "not_found")


@pytest.mark.parametrize("at,code", [
    ("19:15", "not_on_slot_grid"),
    ("18:01", "not_on_slot_grid"),
    ("18:15", "not_on_slot_grid"),
    ("17:00", "outside_opening_hours"),
    ("00:00", "outside_opening_hours"),
    ("22:00", "outside_opening_hours"),
    ("23:00", "outside_opening_hours"),
])
def test_the_slot_rules_have_their_own_codes(world, at, code):
    failed(world.ada.book(starts_at_local=world.at(at)), 422, code)


def test_the_last_slot_that_still_ends_by_closing_is_bookable(world):
    """21:30 plus 90 minutes is exactly 23:00, the closing time."""
    body = ok(world.ada.book(starts_at_local=world.at("21:30")), 201)
    assert body["ends_at"].startswith(f"{world.date}T23:00:00")


def test_a_closed_day_is_outside_opening_hours(api, reset):
    date = fx.booking_date()
    weekday = fx.WEEKDAYS[dt.date.fromisoformat(date).weekday()]
    closed = [h for h in fx.all_week() if h["weekday"] != weekday]
    reset(fx.fixture(restaurants=[fx.restaurant(opening_hours=closed)]))
    ada = api().authenticated(ADA["email"], ADA["password"])
    failed(ada.book(starts_at_local=fx.local(date, "19:00")),
           422, "outside_opening_hours")


def test_a_party_larger_than_the_table_is_422(world):
    failed(world.ada.book(table_id="t_1", party_size=3,
                          starts_at_local=world.at("19:00")),
           422, "party_exceeds_capacity")


# ---- availability parameters ---------------------------------------------

def availability(client, **params):
    return client.get("/availability", params=params)


@pytest.mark.parametrize("drop", ["restaurant_id", "date", "party_size"])
def test_availability_requires_all_three_parameters(world, drop):
    params = {"restaurant_id": world.rid, "date": world.date, "party_size": 2}
    params.pop(drop)
    failed(availability(world.anon, **params), 422, "validation_failed")


@pytest.mark.parametrize("date", ["2026-02-30", "not-a-date", "24-09-2026",
                                  "2026-9-24", "2026-09-24T19:00", "20260924", ""])
def test_availability_rejects_an_unparseable_date(world, date):
    failed(availability(world.anon, restaurant_id=world.rid, date=date,
                        party_size=2), 422, "validation_failed")


@pytest.mark.parametrize("party", ["0", "-1", "abc", "1e9", "4.0", "+4", " 4", "",
                                    "0x4", "4 ", "999999999999999999999"])
def test_availability_rejects_a_party_size_that_is_not_plain_digits(world, party):
    """§5: an integer query parameter is plain decimal digits, whatever the value."""
    failed(availability(world.anon, restaurant_id=world.rid, date=world.date,
                        party_size=party), 422, "validation_failed")


def test_availability_accepts_a_party_nobody_can_seat(world):
    body = ok(availability(world.anon, restaurant_id=world.rid, date=world.date,
                           party_size=999999))
    assert body["slots"], "the slots still exist"
    assert all(slot["available_table_ids"] == [] for slot in body["slots"])


def test_availability_rejects_an_overlong_restaurant_id(world):
    failed(availability(world.anon, restaurant_id="r" * 65, date=world.date,
                        party_size=2), 422, "validation_failed")


def test_availability_of_an_unknown_restaurant_is_404(world):
    failed(availability(world.anon, restaurant_id="r_nope", date=world.date,
                        party_size=2), 404, "not_found")


def test_unknown_query_parameters_are_ignored(world):
    ok(availability(world.anon, restaurant_id=world.rid, date=world.date,
                    party_size=2, sort="whatever", page="3"))


# ---- the reset fixture ---------------------------------------------------

def seeded(date, **over):
    return {"id": "res_seed", "reference": "SEED01", "user_id": "u_ada",
            "restaurant_id": "r_anker", "table_id": "t_2",
            "starts_at_local": fx.local(date, "19:00"), "party_size": 4, **over}


@pytest.mark.parametrize("build,status,code", [
    (lambda d: fx.fixture(users=[{**ADA, "id": "u" * 65}]), 422, "validation_failed"),
    (lambda d: fx.fixture(users=[{**ADA, "id": ""}]), 422, "validation_failed"),
    (lambda d: fx.fixture(users=[ADA, {**ADA, "email": "other@example.com"}]),
     422, "validation_failed"),
    (lambda d: fx.fixture(users=[ADA, {**ADA, "id": "u_two"}]),
     422, "validation_failed"),
    (lambda d: fx.fixture(restaurants=[fx.restaurant(timezone="Mars/Olympus")]),
     422, "validation_failed"),
    (lambda d: fx.fixture(restaurants=[fx.restaurant(slot_minutes=0)]),
     422, "validation_failed"),
    (lambda d: fx.fixture(restaurants=[fx.restaurant(
        reservation_duration_minutes=0)]), 422, "validation_failed"),
    (lambda d: fx.fixture(restaurants=[fx.restaurant(
        cancellation_cutoff_minutes=-1)]), 422, "validation_failed"),
    (lambda d: fx.fixture(restaurants=[fx.restaurant(opening_hours=[
        {"weekday": "funday", "opens": "18:00", "closes": "23:00"}])]),
     422, "validation_failed"),
    (lambda d: fx.fixture(restaurants=[fx.restaurant(opening_hours=[
        {"weekday": "thu", "opens": "23:00", "closes": "18:00"}])]),
     422, "validation_failed"),
    (lambda d: fx.fixture(restaurants=[fx.restaurant(opening_hours=[
        {"weekday": "thu", "opens": "6pm", "closes": "23:00"}])]),
     422, "validation_failed"),
    (lambda d: fx.fixture(restaurants=[fx.restaurant(tables=[
        {"id": "t_1", "label": "1", "capacity": 0}])]), 422, "validation_failed"),
    (lambda d: fx.fixture(restaurants=[fx.restaurant(tables=[
        {"id": "x" * 65, "label": "1", "capacity": 2}])]), 422, "validation_failed"),
    (lambda d: fx.fixture(restaurants=[fx.restaurant(), fx.restaurant()]),
     422, "validation_failed"),
    (lambda d: fx.fixture(reservations=[seeded(d, reference="lower1")]),
     422, "validation_failed"),
    (lambda d: fx.fixture(reservations=[seeded(d, reference="x")]),
     422, "validation_failed"),
    (lambda d: fx.fixture(reservations=[seeded(d, reference="TOO-LONG-DASH")]),
     422, "validation_failed"),
    (lambda d: fx.fixture(reservations=[seeded(d, user_id="u_nobody")]),
     422, "validation_failed"),
    (lambda d: fx.fixture(reservations=[seeded(d, table_id="t_nope")]),
     422, "validation_failed"),
    (lambda d: fx.fixture(reservations=[seeded(d, restaurant_id="r_nope")]),
     422, "validation_failed"),
    (lambda d: fx.fixture(reservations=[seeded(d, party_size=0)]),
     422, "validation_failed"),
    (lambda d: fx.fixture(reservations=[seeded(d, starts_at_local="19:00")]),
     422, "validation_failed"),
    (lambda d: fx.fixture(reservations=[seeded(d), seeded(d, id="res_two",
                                                          reference="SEED02")]),
     422, "validation_failed"),
    (lambda d: fx.fixture(reservations=[seeded(d), seeded(d, id="res_two",
                                                          reference="SEED01")]),
     422, "validation_failed"),
])
def test_an_invalid_fixture_is_rejected(reset, build, status, code):
    date = fx.booking_date()
    failed(reset(build(date), raw=True), status, code)


@pytest.mark.parametrize("fixture", [
    {"users": "ada", "restaurants": [], "reservations": []},
    {"users": [], "restaurants": {}, "reservations": []},
    {"users": [], "restaurants": [], "reservations": 7},
    {"users": [{"id": 7, "email": "a@b.com", "password": "correct horse"}],
     "restaurants": [], "reservations": []},
])
def test_a_fixture_of_the_wrong_json_type_is_400(reset, fixture):
    failed(reset(fixture, raw=True), 400, "malformed_request")


def test_an_empty_fixture_is_accepted(reset, world):
    reset({"users": [], "restaurants": [], "reservations": []})
    assert ok(world.anon.get("/restaurants")) == {"restaurants": []}
    failed(world.anon.login(ADA["email"], ADA["password"]), 401, "unauthenticated")


def test_a_fixture_may_omit_its_optional_lists(reset, world):
    reset({"restaurants": [fx.restaurant()]})
    assert [r["id"] for r in ok(world.anon.get("/restaurants"))["restaurants"]] == \
        ["r_anker"]


def test_a_rejected_fixture_leaves_the_previous_state_in_place(world):
    booking = ok(world.ada.book(starts_at_local=world.at("19:00")), 201)
    resp = world.anon.post("/_test/reset", json=fx.fixture(
        restaurants=[fx.restaurant(timezone="Mars/Olympus")]),
        timeout=fx.CONTROL_TIMEOUT)
    failed(resp, 422, "validation_failed")
    assert ok(world.ada.get(f"/reservations/{booking['reference']}")) == booking


def test_a_seeded_cancelled_booking_does_not_occupy_its_table(reset, world):
    date = fx.booking_date()
    reset(fx.fixture(reservations=[seeded(date, status="cancelled")]))
    free = world.slots(world.anon, party_size=4, date=date)
    assert "t_2" in free["19:00"]
