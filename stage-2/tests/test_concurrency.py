"""§1, §2 and §7 under the stated load: 50 requests in flight.

The requirements put a number on this -- up to 50 concurrent requests, no 5xx, no
double booking, no duplicate or partial bookings from retries -- so these checks use
that number rather than a comfortable one.
"""
from __future__ import annotations

import conftest as fx
from conftest import (ADA, Api, assert_no_double_booking, burst, failed, new_key,
                      no_5xx, ok, tally)

IN_FLIGHT = 50


def crowd(pool, reset, *, tables=None, restaurants=None, size=IN_FLIGHT):
    """`size` separate accounts, each signed in on its own client."""
    users = [dict(ADA, id=f"u_{i}", email=f"diner{i}@example.com") for i in range(size)]
    if restaurants is None:
        restaurants = [fx.restaurant(tables=tables)] if tables else None
    reset(fx.fixture(users=users, restaurants=restaurants))
    return [Api(pool).authenticated(user["email"], user["password"]) for user in users]


def test_fifty_clients_racing_for_one_table_produce_one_booking(pool, reset):
    """Exactly one 201 and no 5xx: the table cannot be sold twice."""
    clients = crowd(pool, reset)
    date = fx.booking_date()
    body = {"restaurant_id": "r_anker", "table_id": "t_2",
            "starts_at_local": fx.local(date, "19:00"), "party_size": 4}

    responses = burst(lambda i: clients[i].post(
        "/reservations", json=body, key=new_key()), IN_FLIGHT)

    no_5xx(responses)
    counts = tally(responses)
    assert counts.get(201) == 1, counts
    assert counts.get(409) == IN_FLIGHT - 1, counts
    for resp in responses:
        if resp.status_code == 409:
            assert resp.json()["error"]["code"] == "table_unavailable"
    assert assert_no_double_booking(clients) == 1


def test_fifty_clients_racing_for_overlapping_intervals(pool, reset):
    """Overlap, not slot equality, is what is mutually exclusive (§1).

    The 90-minute duration on a 30-minute grid makes 18:00, 18:30, 19:00, 19:30 and
    20:00 collide on one table -- except that 18:00/19:30 and 18:30/20:00 are merely
    adjacent, so the most the crowd can win is two bookings and the least is one,
    depending on which request lands first. Anything else is a double booking.
    """
    clients = crowd(pool, reset)
    date = fx.booking_date()
    times = ["18:00", "18:30", "19:00", "19:30", "20:00"]

    responses = burst(lambda i: clients[i].book(
        table_id="t_2", starts_at_local=fx.local(date, times[i % len(times)])), IN_FLIGHT)

    no_5xx(responses)
    counts = tally(responses)
    won = counts.get(201, 0)
    assert won in (1, 2), counts
    assert counts.get(409) == IN_FLIGHT - won, counts
    for resp in responses:
        if resp.status_code == 409:
            assert resp.json()["error"]["code"] == "table_unavailable"
    assert assert_no_double_booking(clients) == won


def test_fifty_identical_retries_take_effect_once(pool, reset, api):
    """§7: one 201, the rest 200 with the same body, and one booking in the store."""
    reset(fx.fixture())
    ada = api().authenticated(ADA["email"], ADA["password"])
    date = fx.booking_date()
    key = new_key()
    body = {"restaurant_id": "r_anker", "table_id": "t_2",
            "starts_at_local": fx.local(date, "19:00"), "party_size": 4}

    responses = burst(lambda i: Api(pool, ada.token).post(
        "/reservations", json=body, key=key), IN_FLIGHT)

    no_5xx(responses)
    counts = tally(responses)
    assert counts.get(201) == 1, counts
    assert counts.get(200) == IN_FLIGHT - 1, counts
    created = next(r.json() for r in responses if r.status_code == 201)
    for resp in responses:
        assert resp.json() == created, "every replay returns the original response"
    assert len(ok(ada.get("/reservations"))["reservations"]) == 1, \
        "a retried create must not leave a second booking"


def test_fifty_amendments_onto_one_free_table_produce_one_winner(pool, reset):
    """A PATCH is a release and a take together, so it is exclusive too (§8)."""
    tables = [{"id": "t_free", "label": "free", "capacity": 4}]
    tables += [{"id": f"t_{i}", "label": str(i), "capacity": 4} for i in range(IN_FLIGHT)]
    clients = crowd(pool, reset, tables=tables)
    date = fx.booking_date()

    booked = [ok(clients[i].book(table_id=f"t_{i}",
                                 starts_at_local=fx.local(date, "19:00")), 201)
              for i in range(IN_FLIGHT)]

    responses = burst(lambda i: clients[i].patch(
        f"/reservations/{booked[i]['reference']}", json={"table_id": "t_free"}),
        IN_FLIGHT)

    no_5xx(responses)
    counts = tally(responses)
    assert counts.get(200) == 1, counts
    assert counts.get(409) == IN_FLIGHT - 1, counts
    assert assert_no_double_booking(clients) == IN_FLIGHT, \
        "a refused amendment must leave its booking exactly where it was"


def test_concurrent_cancel_and_rebook_never_double_books(pool, reset):
    """Half the crowd releases its table while the other half tries to take it.

    Whatever the interleaving, the one thing that may never happen is two confirmed
    bookings on one table at one time -- and a cancel must genuinely free the table
    rather than appear to (§8).
    """
    tables = [{"id": f"t_{i}", "label": str(i), "capacity": 4} for i in range(10)]
    clients = crowd(pool, reset, tables=tables, size=20)
    date = fx.booking_date()
    holders, takers = clients[:10], clients[10:]
    held = [ok(holders[i].book(table_id=f"t_{i}",
                               starts_at_local=fx.local(date, "19:00")), 201)
            for i in range(10)]

    def act(index):
        if index < 10:
            return holders[index].post(f"/reservations/{held[index]['reference']}/cancel")
        seat = index - 10
        return takers[seat].book(table_id=f"t_{seat}",
                                 starts_at_local=fx.local(date, "19:00"))

    responses = burst(act, 20)
    no_5xx(responses)
    for resp in responses:
        assert resp.status_code in (200, 201, 409), fx.described(resp)
    assert_no_double_booking(clients)


def test_concurrent_identical_move_batches_stay_atomic(pool, reset, api):
    """§11: every batch either swaps both bookings or changes nothing at all."""
    reset(fx.fixture())
    ada = api().authenticated(ADA["email"], ADA["password"])
    date = fx.booking_date()
    first = ok(ada.book(table_id="t_2", starts_at_local=fx.local(date, "19:00")), 201)
    second = ok(ada.book(table_id="t_3", starts_at_local=fx.local(date, "19:00")), 201)
    swap = [{"reference": first["reference"], "table_id": "t_3"},
            {"reference": second["reference"], "table_id": "t_2"}]

    responses = burst(lambda i: Api(pool, ada.token).moves(swap, key=new_key()),
                      IN_FLIGHT)

    no_5xx(responses)
    for resp in responses:
        assert resp.status_code in (201, 409), fx.described(resp)
        if resp.status_code == 201:
            tables = [r["table_id"] for r in resp.json()["reservations"]]
            assert tables == ["t_3", "t_2"], tables
    tables = {ok(ada.get(f"/reservations/{b['reference']}"))["table_id"]
              for b in (first, second)}
    assert tables == {"t_2", "t_3"}, \
        f"the two bookings must still hold two different tables, got {tables}"
    assert_no_double_booking([ada])


def test_a_mixed_fifty_request_load_never_returns_5xx(pool, reset):
    """§5: no request may produce a 5xx, valid or not, under load or not."""
    tables = [{"id": f"t_{i}", "label": str(i), "capacity": 4} for i in range(10)]
    clients = crowd(pool, reset, tables=tables, size=10)
    date = fx.booking_date()
    seeded = [ok(clients[i].book(table_id=f"t_{i}",
                                 starts_at_local=fx.local(date, "19:00")), 201)
              for i in range(5)]

    def act(index):
        client = clients[index % 10]
        choice = index % 10
        if choice == 0:
            return client.get("/availability", params={
                "restaurant_id": "r_anker", "date": date, "party_size": 4})
        if choice == 1:
            return client.book(table_id="t_7", starts_at_local=fx.local(date, "19:00"))
        if choice == 2:
            return client.book(table_id="t_nope", starts_at_local=fx.local(date, "19:00"))
        if choice == 3:
            return client.book(party_size="four", table_id="t_8",
                               starts_at_local=fx.local(date, "19:00"))
        if choice == 4:
            return client.post("/reservations", content="{not json", key=new_key())
        if choice == 5:
            return client.patch(f"/reservations/{seeded[index % 5]['reference']}",
                                json={"party_size": 2})
        if choice == 6:
            return client.post(f"/reservations/{seeded[index % 5]['reference']}/cancel")
        if choice == 7:
            return client.moves([{"reference": seeded[index % 5]["reference"],
                                  "table_id": "t_9"}])
        if choice == 8:
            return client.get("/reservations")
        return client.get("/restaurants", token=None)

    responses = burst(act, IN_FLIGHT)
    no_5xx(responses)
    assert_no_double_booking(clients)


def test_health_answers_while_the_service_is_under_load(pool, reset):
    """§3.2: health is readiness, not a lock on the store."""
    clients = crowd(pool, reset, size=10)
    date = fx.booking_date()

    def act(index):
        if index % 2 == 0:
            return clients[index % 10].get("/health", token=None)
        return clients[index % 10].book(starts_at_local=fx.local(date, "19:00"))

    responses = burst(act, IN_FLIGHT)
    no_5xx(responses)
    for resp in responses:
        if resp.request.url.path == "/health":
            assert ok(resp) == {"status": "ok"}
