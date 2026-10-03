"""Service-level checks written from the stage-1 requirements.

These run against a *running container*, never against the source: the submission is
an HTTP service, so every assertion here is about HTTP behaviour.

    docker build -t tablekeeper stage-1
    docker run -d --name tk -e PORT=8080 -p 127.0.0.1:18080:8080 tablekeeper
    python -m pytest stage-1/tests --base-url http://127.0.0.1:18080

One connection pool is shared by every client in a test, because a fresh TCP
connection to a published container port is expensive on some hosts and that cost
would otherwise dominate the run.
"""
from __future__ import annotations

import datetime as dt
import os
import uuid
from zoneinfo import ZoneInfo

import httpx
import pytest

REQUEST_TIMEOUT = 5.0       # the spec's per-request budget
CONTROL_TIMEOUT = 10.0      # the spec's budget for /_test/* control calls

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

ADA = {"id": "u_ada", "email": "ada@example.com",
       "password": "correct horse", "display_name": "Ada"}
BOB = {"id": "u_bob", "email": "bob@example.com",
       "password": "correct horse", "display_name": "Bob"}


def pytest_addoption(parser):
    parser.addoption("--base-url", default=os.environ.get(
        "TABLEKEEPER_BASE_URL", "http://127.0.0.1:18080"),
        help="Base URL of the running service under test")


def new_key() -> str:
    return uuid.uuid4().hex


class Api:
    """A client that speaks the API and asserts nothing by itself."""

    def __init__(self, pool: httpx.Client, token: str | None = None):
        self._pool = pool
        self.token = token

    def request(self, method: str, path: str, *, json=None, content=None,
                token: str | None = ..., key: str | None = None,
                params: dict | None = None, headers: dict | None = None,
                timeout: float = REQUEST_TIMEOUT) -> httpx.Response:
        sent = dict(headers or {})
        effective = self.token if token is ... else token
        if effective is not None:
            sent.setdefault("Authorization", f"Bearer {effective}")
        if key is not None:
            sent.setdefault("Idempotency-Key", key)
        kwargs = {"headers": sent, "timeout": timeout}
        if json is not None:
            kwargs["json"] = json
        if content is not None:
            kwargs["content"] = content
            sent.setdefault("Content-Type", "application/json")
        if params is not None:
            kwargs["params"] = params
        return self._pool.request(method, path, **kwargs)

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, **kw):
        return self.request("POST", path, **kw)

    def patch(self, path, **kw):
        return self.request("PATCH", path, **kw)

    def login(self, email: str, password: str) -> httpx.Response:
        return self.post("/auth/login", json={"email": email, "password": password},
                         token=None)

    def signup(self, email: str, password: str, display_name: str) -> httpx.Response:
        return self.post("/auth/signup", json={
            "email": email, "password": password, "display_name": display_name},
            token=None)

    def authenticated(self, email: str, password: str) -> "Api":
        resp = self.login(email, password)
        assert resp.status_code == 200, resp.text
        self.token = resp.json()["token"]
        return self

    def book(self, *, restaurant_id="r_anker", table_id="t_2", starts_at_local=None,
             party_size=4, key=None, **extra) -> httpx.Response:
        body = {"restaurant_id": restaurant_id, "table_id": table_id,
                "starts_at_local": starts_at_local, "party_size": party_size}
        body.update(extra)
        return self.post("/reservations", json=body,
                         key=new_key() if key is None else key)

    def moves(self, items, *, key=None) -> httpx.Response:
        return self.post("/reservation-moves", json={"moves": items},
                         key=new_key() if key is None else key)


@pytest.fixture(scope="session")
def base_url(pytestconfig) -> str:
    return pytestconfig.getoption("--base-url").rstrip("/")


@pytest.fixture(scope="session")
def pool(base_url):
    limits = httpx.Limits(max_connections=80, max_keepalive_connections=80)
    with httpx.Client(base_url=base_url, limits=limits) as client:
        yield client


@pytest.fixture
def api(pool):
    return lambda token=None: Api(pool, token)


@pytest.fixture
def anon(api):
    return api()


@pytest.fixture
def reset(pool):
    def _reset(fixture: dict, *, raw: bool = False) -> httpx.Response:
        resp = pool.post("/_test/reset", json=fixture, timeout=CONTROL_TIMEOUT)
        if not raw:
            assert resp.status_code == 204, resp.text
        return resp
    return _reset


# ---- fixture builders ----------------------------------------------------

def all_week(opens="18:00", closes="23:00"):
    return [{"weekday": day, "opens": opens, "closes": closes} for day in WEEKDAYS]


def restaurant(rid="r_anker", *, name="Zum Anker", timezone="Europe/Berlin",
               slot_minutes=30, reservation_duration_minutes=90,
               cancellation_cutoff_minutes=120, opening_hours=None, tables=None):
    return {
        "id": rid,
        "name": name,
        "timezone": timezone,
        "slot_minutes": slot_minutes,
        "reservation_duration_minutes": reservation_duration_minutes,
        "cancellation_cutoff_minutes": cancellation_cutoff_minutes,
        "opening_hours": all_week() if opening_hours is None else opening_hours,
        "tables": tables if tables is not None else [
            {"id": "t_1", "label": "1", "capacity": 2},
            {"id": "t_2", "label": "2", "capacity": 4},
            {"id": "t_3", "label": "3", "capacity": 6},
        ],
    }


def fixture(*, users=None, restaurants=None, reservations=None):
    return {
        "users": [ADA, BOB] if users is None else users,
        "restaurants": [restaurant()] if restaurants is None else restaurants,
        "reservations": reservations or [],
    }


def booking_date(timezone="Europe/Berlin", lead=7) -> str:
    today = dt.datetime.now(ZoneInfo(timezone)).date()
    return (today + dt.timedelta(days=lead)).isoformat()


def local(date: str, hhmm="19:00") -> str:
    return f"{date}T{hhmm}"


class World:
    """The default seeded world, with Ada and Bob already signed in."""

    def __init__(self, api, reset, **kwargs):
        self.fixture = fixture(**kwargs)
        reset(self.fixture)
        self.restaurant = self.fixture["restaurants"][0]
        self.rid = self.restaurant["id"]
        self.timezone = self.restaurant["timezone"]
        self.date = booking_date(self.timezone)
        self.ada = api().authenticated(ADA["email"], ADA["password"])
        self.bob = api().authenticated(BOB["email"], BOB["password"])
        self.anon = api()

    def at(self, hhmm="19:00") -> str:
        return local(self.date, hhmm)

    def slots(self, client, party_size=4, date=None, rid=None):
        resp = client.get("/availability", params={
            "restaurant_id": rid or self.rid, "date": date or self.date,
            "party_size": party_size})
        assert resp.status_code == 200, resp.text
        return {s["starts_at_local"].split("T")[1]: s["available_table_ids"]
                for s in resp.json()["slots"]}


@pytest.fixture
def world(api, reset):
    return World(api, reset)


# ---- assertions ----------------------------------------------------------

def described(resp: httpx.Response) -> str:
    return (f"{resp.request.method} {resp.request.url.path} -> "
            f"{resp.status_code} {resp.text[:400]!r}")


def ok(resp: httpx.Response, expected: int = 200) -> dict:
    assert resp.status_code == expected, described(resp)
    return resp.json()


def failed(resp: httpx.Response, status: int, code: str) -> dict:
    body = resp.json()
    assert resp.status_code == status, described(resp)
    assert isinstance(body.get("error"), dict), described(resp)
    assert body["error"].get("code") == code, described(resp)
    assert isinstance(body["error"].get("message"), str) and body["error"]["message"], \
        described(resp)
    return body


# ---- concurrency helpers -------------------------------------------------

def burst(call, count: int):
    """Fire `count` calls as close to simultaneously as a client can manage.

    A barrier holds every thread until all of them are ready, so the service really
    does see the requests in flight together rather than trickling in.
    """
    import concurrent.futures
    import threading

    gate = threading.Barrier(count)

    def run(index):
        gate.wait()
        return call(index)

    with concurrent.futures.ThreadPoolExecutor(max_workers=count) as pool:
        futures = [pool.submit(run, index) for index in range(count)]
        return [future.result() for future in futures]


def tally(responses) -> dict:
    counts: dict[int, int] = {}
    for resp in responses:
        counts[resp.status_code] = counts.get(resp.status_code, 0) + 1
    return counts


def no_5xx(responses) -> None:
    for resp in responses:
        assert resp.status_code < 500, described(resp)


def assert_no_double_booking(clients, duration_minutes=90) -> int:
    """The core invariant of §1, checked through the public API only.

    Every caller's own list is read and all confirmed bookings are compared as
    half-open intervals per table. Returns how many confirmed bookings were seen.
    """
    span = dt.timedelta(minutes=duration_minutes)
    held: dict[tuple, list] = {}
    seen = 0
    for client in clients:
        for booking in ok(client.get("/reservations"))["reservations"]:
            if booking["status"] != "confirmed":
                continue
            seen += 1
            start = dt.datetime.fromisoformat(booking["starts_at"])
            where = (booking["restaurant_id"], booking["table_id"])
            for other_reference, other_start in held.setdefault(where, []):
                assert not (start < other_start + span and other_start < start + span), (
                    f"{booking['reference']} and {other_reference} both hold "
                    f"{where} at overlapping times")
            held[where].append((booking["reference"], start))
    return seen
