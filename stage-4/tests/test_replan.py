"""Stage 4: previewing and applying a seating plan after a table closure.

The optimiser is the risk here, so most of this file does not trust it: it works out
the answer independently, by enumerating every feasible arrangement in Python and
ranking them by the stated objective, and then insists the service agrees -- including
on scenarios built so that the first, the second and the third tie-breaker each decide.
"""
from __future__ import annotations

import datetime as dt
import itertools
import random

import conftest as fx
from conftest import ADA, BOB, failed, new_key, ok
import pytest

BERLIN = "Europe/Berlin"


# ---- the world ------------------------------------------------------------

def tables(*sizes):
    return [{"id": f"t_{i + 1}", "label": str(i + 1), "capacity": size}
            for i, size in enumerate(sizes)]


@pytest.fixture
def house(api, reset):
    """A restaurant Ada manages, seeded with bookings described declaratively.

    Each seeded booking is (reference, table ids, HH:MM, party size); they are placed
    through the fixture so the test knows the whole truth about the restaurant.
    """
    def _make(*, sizes=(2, 4, 6), combinable=None, bookings=(), slot_minutes=30,
              duration=90, opens="18:00", closes="23:00", owner="u_ada"):
        date = fx.booking_date()
        restaurant = {
            "id": "r_anker", "name": "Zum Anker", "timezone": BERLIN,
            "slot_minutes": slot_minutes, "reservation_duration_minutes": duration,
            "cancellation_cutoff_minutes": 120,
            "opening_hours": fx.all_week(opens, closes),
            "tables": tables(*sizes),
            "combinable": [] if combinable is None else combinable,
            "manager_user_ids": ["u_ada"],
        }
        seeded = []
        for index, (reference, table_ids, at, party) in enumerate(bookings):
            seeded.append({
                "id": f"res_{index}", "reference": reference, "user_id": owner,
                "restaurant_id": "r_anker", "table_ids": list(table_ids),
                "starts_at_local": fx.local(date, at), "party_size": party,
            })
        reset(fx.fixture(restaurants=[restaurant], reservations=seeded))

        class House:
            pass

        house = House()
        house.date = date
        house.restaurant = restaurant
        house.ada = api().authenticated(ADA["email"], ADA["password"])
        house.bob = api().authenticated(BOB["email"], BOB["password"])
        house.anon = api()
        house.detail = ok(house.anon.get("/restaurants/r_anker"))
        house.instant = lambda at: instant_at(date, at)
        return house
    return _make


def instant_at(date: str, at: str) -> str:
    hour, minute = (int(part) for part in at.split(":"))
    from zoneinfo import ZoneInfo
    return dt.datetime.combine(dt.date.fromisoformat(date), dt.time(hour, minute),
                               tzinfo=ZoneInfo(BERLIN)).isoformat()


def preview(house, table_id, from_at, to_at, *, who=None, key=None):
    return (who or house.ada).post("/restaurants/r_anker/replans", json={
        "table_id": table_id, "from": house.instant(from_at),
        "to": house.instant(to_at)}, key=new_key() if key is None else key)


def apply_plan(house, plan_id, *, who=None, key=None):
    return (who or house.ada).post(
        f"/restaurants/r_anker/replans/{plan_id}/apply", json={},
        key=new_key() if key is None else key)


# ---- an independent optimiser --------------------------------------------

def moment(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text)


def brute_force(house, closure_table, from_at, to_at):
    """Work the plan out from scratch, by enumeration, and return its shape.

    This deliberately shares no code with the service: it reads the restaurant and the
    bookings back over the API, enumerates every assignment of every considered booking
    to every option, discards the infeasible ones, and ranks what is left by the
    requirements' own objective.
    """
    detail = house.detail
    capacities = {table["id"]: table["capacity"] for table in detail["tables"]}
    options = [[table["id"]] for table in detail["tables"]]
    options += [list(pair) for pair in detail.get("combinable", [])]

    window = (moment(house.instant(from_at)), moment(house.instant(to_at)))
    bookings = [ok(house.ada.get(f"/reservations/{reference}"))
                for reference in sorted(all_references(house))]
    considered, fixed = [], []
    for booking in bookings:
        if booking["status"] != "confirmed":
            continue
        span = (moment(booking["starts_at"]), moment(booking["ends_at"]))
        if span[0] < window[1] and window[0] < span[1]:
            considered.append(booking)
        else:
            fixed.append(booking)
    considered.sort(key=lambda booking: booking["reference"])

    def overlaps(first, second):
        return first[0] < second[1] and second[0] < first[1]

    def feasible_for(booking):
        span = (moment(booking["starts_at"]), moment(booking["ends_at"]))
        seats = booking["accepted_terms"]["capacities"]
        open_options = []
        for rank, option in enumerate(options):
            if sum(seats[table] for table in option) < booking["party_size"]:
                continue
            if closure_table in option and overlaps(span, window):
                continue
            clash = False
            for other in fixed:
                other_span = (moment(other["starts_at"]), moment(other["ends_at"]))
                if set(option) & set(other["table_ids"]) and overlaps(span, other_span):
                    clash = True
                    break
            if not clash:
                open_options.append((rank, option))
        return open_options

    choices = [feasible_for(booking) for booking in considered]
    best = None
    for combination in itertools.product(*choices):
        legal = True
        for i, (_, option) in enumerate(combination):
            span = (moment(considered[i]["starts_at"]), moment(considered[i]["ends_at"]))
            for j in range(i + 1, len(combination)):
                other_span = (moment(considered[j]["starts_at"]),
                              moment(considered[j]["ends_at"]))
                if set(option) & set(combination[j][1]) and overlaps(span, other_span):
                    legal = False
                    break
            if not legal:
                break
        if not legal:
            continue
        moved = sum(1 for i, (_, option) in enumerate(combination)
                    if set(option) != set(considered[i]["table_ids"]))
        unused = sum(
            sum(considered[i]["accepted_terms"]["capacities"][table] for table in option)
            - considered[i]["party_size"]
            for i, (_, option) in enumerate(combination))
        ranks = [rank for rank, _ in combination]
        cost = (moved, unused, ranks)
        if best is None or cost < best[0]:
            best = (cost, combination)
    if best is None:
        return None
    (moved, unused, ranks), combination = best
    return {
        "moved_count": moved,
        "unused_seats": unused,
        "assignments": [
            {"reference": considered[i]["reference"], "table_ids": list(option),
             "changed": set(option) != set(considered[i]["table_ids"])}
            for i, (_, option) in enumerate(combination)],
    }


def all_references(house):
    """Every reference at the restaurant, read back through the owner's own list."""
    return [booking["reference"]
            for booking in ok(house.ada.get("/reservations"))["reservations"]]


def assert_matches_brute_force(house, table_id, from_at, to_at):
    expected = brute_force(house, table_id, from_at, to_at)
    resp = preview(house, table_id, from_at, to_at)
    if expected is None:
        failed(resp, 409, "no_feasible_plan")
        return None
    plan = ok(resp, 201)
    assert plan["moved_count"] == expected["moved_count"], \
        f"moved: {plan['moved_count']} vs {expected['moved_count']}"
    assert plan["unused_seats"] == expected["unused_seats"], \
        f"unused: {plan['unused_seats']} vs {expected['unused_seats']}"
    assert plan["assignments"] == expected["assignments"], \
        f"assignments:\n  service {plan['assignments']}\n  brute   {expected['assignments']}"
    return plan


# ---- the objective, level by level ---------------------------------------

def test_a_closure_with_nothing_to_move(house):
    world = house()
    plan = ok(preview(world, "t_2", "18:00", "23:00"), 201)
    assert plan["assignments"] == [] and plan["moved_count"] == 0
    assert plan["unused_seats"] == 0
    assert plan["closure"] == {"table_id": "t_2",
                               "from": world.instant("18:00"),
                               "to": world.instant("23:00")}


def test_a_booking_on_the_closing_table_moves(house):
    world = house(bookings=[("BOOK01", ["t_2"], "19:00", 4)])
    plan = assert_matches_brute_force(world, "t_2", "18:00", "23:00")
    assert plan["assignments"] == [
        {"reference": "BOOK01", "table_ids": ["t_3"], "changed": True}]
    assert plan["moved_count"] == 1 and plan["unused_seats"] == 2


def test_a_booking_outside_the_window_is_not_considered(house):
    """The closure runs 18:00-20:00; a 20:30 booking is untouched and not reported."""
    world = house(bookings=[("BOOK01", ["t_2"], "20:30", 4)])
    plan = ok(preview(world, "t_2", "18:00", "20:00"), 201)
    assert plan["assignments"] == []


def test_fewest_moves_wins_over_tidier_seating(house):
    """Level 1 decides first, even when moving more would waste fewer seats.

    t_1 seats 2, t_2 seats 2, t_3 seats 6. Closing t_1 forces its party of 2 somewhere;
    leaving the party of 2 on t_2 where it already sits costs one move, while shuffling
    both bookings would cost two however neat the result.
    """
    world = house(sizes=(2, 2, 6),
                  bookings=[("BOOK01", ["t_1"], "19:00", 2),
                            ("BOOK02", ["t_2"], "19:00", 2)])
    plan = assert_matches_brute_force(world, "t_1", "18:00", "23:00")
    assert plan["moved_count"] == 1
    assert plan["assignments"] == [
        {"reference": "BOOK01", "table_ids": ["t_3"], "changed": True},
        {"reference": "BOOK02", "table_ids": ["t_2"], "changed": False}]


def test_least_unused_seats_breaks_a_tie_on_moves(house):
    """Level 2. Closing t_1 moves exactly one booking either way; the plan that wastes
    fewer seats puts the party of 2 on the table that seats 3, not the one that seats 6.
    """
    world = house(sizes=(2, 3, 6), bookings=[("BOOK01", ["t_1"], "19:00", 2)])
    plan = assert_matches_brute_force(world, "t_1", "18:00", "23:00")
    assert plan["assignments"] == [
        {"reference": "BOOK01", "table_ids": ["t_2"], "changed": True}]
    assert plan["unused_seats"] == 1


def test_the_rank_vector_breaks_a_tie_on_both(house):
    """Level 3. Two tables of the same size are equally good by every earlier measure,
    so the earlier one in fixture order wins.
    """
    world = house(sizes=(4, 4, 4), bookings=[("BOOK01", ["t_3"], "19:00", 4)])
    plan = assert_matches_brute_force(world, "t_3", "18:00", "23:00")
    assert plan["assignments"] == [
        {"reference": "BOOK01", "table_ids": ["t_1"], "changed": True}]


def test_the_rank_vector_is_read_in_reference_order(house):
    """Level 3 again, with two bookings to place and two identical tables: the booking
    whose reference sorts first takes the lower-ranked table.
    """
    world = house(sizes=(4, 4, 4, 4),
                  bookings=[("BOOK02", ["t_3"], "19:00", 4),
                            ("BOOK01", ["t_4"], "19:00", 4)])
    plan = assert_matches_brute_force(world, "t_3", "18:00", "23:00")
    assert [assignment["reference"] for assignment in plan["assignments"]] == \
        ["BOOK01", "BOOK02"], "assignments are in reference order"


def test_singles_outrank_pairs(house):
    """A pair is ranked after every single, so a single is preferred on level 3 when
    the earlier levels tie."""
    world = house(sizes=(3, 3, 3), combinable=[["t_1", "t_2"]],
                  bookings=[("BOOK01", ["t_3"], "19:00", 3)])
    plan = assert_matches_brute_force(world, "t_3", "18:00", "23:00")
    assert plan["assignments"][0]["table_ids"] == ["t_1"]


def test_a_pair_is_used_when_no_single_fits(house):
    world = house(sizes=(2, 4, 3), combinable=[["t_1", "t_2"], ["t_2", "t_3"]],
                  bookings=[("BOOK01", ["t_3"], "19:00", 3)])
    # Closing t_3: a party of 3 fits t_2 (4 seats) or a pair; t_2 wastes the least.
    plan = assert_matches_brute_force(world, "t_3", "18:00", "23:00")
    assert plan["assignments"][0]["table_ids"] == ["t_2"]


def test_a_pair_is_used_when_the_only_single_is_taken(house):
    """With t_2 held by someone else, the party of 3 has only pairs left -- and both
    of them need t_2 as well, so there is no feasible plan at all. The enumeration and
    the service have to agree on that too.
    """
    world = house(sizes=(2, 4, 3), combinable=[["t_1", "t_2"], ["t_2", "t_3"]],
                  bookings=[("BOOK01", ["t_3"], "19:00", 3),
                            ("BOOK02", ["t_2"], "19:00", 4)])
    assert assert_matches_brute_force(world, "t_3", "18:00", "23:00") is None


def test_a_pair_really_is_offered_when_it_is_the_way_out(house):
    """t_1 and t_2 seat 2 each and t_3 seats 3. Closing t_3 leaves the party of 3 no
    single table, but the declared pair seats 4.
    """
    world = house(sizes=(2, 2, 3), combinable=[["t_1", "t_2"]],
                  bookings=[("BOOK01", ["t_3"], "19:00", 3)])
    plan = assert_matches_brute_force(world, "t_3", "18:00", "23:00")
    assert plan["assignments"] == [
        {"reference": "BOOK01", "table_ids": ["t_1", "t_2"], "changed": True}]
    assert plan["unused_seats"] == 1


def test_a_neighbour_may_be_moved_to_make_room(house):
    """Every confirmed booking overlapping the window is considered, not only those on
    the closing table: sometimes the repair is to shuffle a neighbour.

    t_1 seats 6, t_2 seats 6, t_3 seats 2. Closing t_1 with a party of 6 on it and a
    party of 2 on t_2 means the party of 2 has to move to t_3 so the six can take t_2.
    """
    world = house(sizes=(6, 6, 2),
                  bookings=[("BOOK01", ["t_1"], "19:00", 6),
                            ("BOOK02", ["t_2"], "19:00", 2)])
    plan = assert_matches_brute_force(world, "t_1", "18:00", "23:00")
    assert plan["moved_count"] == 2
    assert plan["assignments"] == [
        {"reference": "BOOK01", "table_ids": ["t_2"], "changed": True},
        {"reference": "BOOK02", "table_ids": ["t_3"], "changed": True}]


def test_no_feasible_plan_changes_nothing(house):
    world = house(sizes=(2, 4, 2), bookings=[("BOOK01", ["t_2"], "19:00", 4)])
    before = ok(world.ada.get("/reservations/BOOK01"))
    failed(preview(world, "t_2", "18:00", "23:00"), 409, "no_feasible_plan")
    assert ok(world.ada.get("/reservations/BOOK01")) == before
    assert ok(world.anon.get("/restaurants/r_anker"))["revision"] == \
        world.detail["revision"], "a refused preview moves no revision"


def test_cutoffs_do_not_stand_in_the_way_of_a_repair(api, reset):
    """A diner could not move this booking -- it is in the past -- but an operator
    repairing a closure may."""
    past = fx.days_from(fx.booking_date(), -30)
    restaurant = {
        "id": "r_anker", "name": "Zum Anker", "timezone": BERLIN, "slot_minutes": 30,
        "reservation_duration_minutes": 90, "cancellation_cutoff_minutes": 120,
        "opening_hours": fx.all_week(), "tables": tables(2, 4, 6),
        "manager_user_ids": ["u_ada"],
    }
    seeded = [{"id": "res_0", "reference": "BOOK01", "user_id": "u_ada",
               "restaurant_id": "r_anker", "table_ids": ["t_2"],
               "starts_at_local": fx.local(past, "19:00"), "party_size": 4}]
    reset(fx.fixture(restaurants=[restaurant], reservations=seeded))
    ada = api().authenticated(ADA["email"], ADA["password"])
    failed(ada.patch("/reservations/BOOK01", json={"table_id": "t_3"}),
           409, "cutoff_passed")

    plan = ok(ada.post("/restaurants/r_anker/replans", json={
        "table_id": "t_2", "from": instant_at(past, "18:00"),
        "to": instant_at(past, "23:00")}, key=new_key()), 201)
    assert plan["assignments"] == [
        {"reference": "BOOK01", "table_ids": ["t_3"], "changed": True}]
    ok(ada.post(f"/restaurants/r_anker/replans/{plan['plan_id']}/apply", json={},
                key=new_key()), 201)
    assert ok(ada.get("/reservations/BOOK01"))["table_ids"] == ["t_3"]


def test_planning_limits(house):
    """Stage 4 promises 6 tables, 4 declared pairs and 6 considered bookings, and
    allows a larger input to be refused rather than answered slowly."""
    seven_tables = house(sizes=(2, 2, 2, 2, 2, 2, 2))
    failed(preview(seven_tables, "t_1", "18:00", "23:00"), 422, "planning_limit")

    five_pairs = house(sizes=(2, 2, 2, 2, 2, 2),
                       combinable=[["t_1", "t_2"], ["t_2", "t_3"], ["t_3", "t_4"],
                                   ["t_4", "t_5"], ["t_5", "t_6"]])
    failed(preview(five_pairs, "t_1", "18:00", "23:00"), 422, "planning_limit")

    # Six considered bookings is inside the promise: two sittings on three tables,
    # with three more tables free to move into.
    six = house(sizes=(2, 2, 2, 2, 2, 2), bookings=[
        ("BOOK01", ["t_1"], "19:00", 2), ("BOOK02", ["t_2"], "19:00", 2),
        ("BOOK03", ["t_3"], "19:00", 2), ("BOOK04", ["t_1"], "21:00", 2),
        ("BOOK05", ["t_2"], "21:00", 2), ("BOOK06", ["t_3"], "21:00", 2)])
    plan = ok(preview(six, "t_1", "18:00", "23:00"), 201)
    assert len(plan["assignments"]) == 6 and plan["moved_count"] == 2

    seven = house(sizes=(2, 2, 2, 2, 2, 2), bookings=[
        ("BOOK01", ["t_1"], "19:00", 2), ("BOOK02", ["t_2"], "19:00", 2),
        ("BOOK03", ["t_3"], "19:00", 2), ("BOOK04", ["t_1"], "21:00", 2),
        ("BOOK05", ["t_2"], "21:00", 2), ("BOOK06", ["t_3"], "21:00", 2),
        ("BOOK07", ["t_4"], "19:00", 2)])
    failed(preview(seven, "t_1", "18:00", "23:00"), 422, "planning_limit")


# ---- a random comparison -------------------------------------------------

@pytest.mark.parametrize("seed", list(range(12)))
def test_the_optimiser_agrees_with_brute_force(house, seed):
    """Random restaurants and bookings, judged against the enumeration.

    This is the check that matters: not that the plan looks sensible, but that it is
    the one the stated objective names, whatever the shape of the problem.
    """
    rng = random.Random(seed)
    sizes = tuple(rng.choice([2, 2, 3, 4, 6]) for _ in range(rng.randint(3, 5)))
    pairs = []
    if len(sizes) >= 3 and rng.random() < 0.6:
        pairs.append(["t_1", "t_2"])
        if rng.random() < 0.5:
            pairs.append(["t_2", "t_3"])
    slots = ["18:00", "19:00", "19:30", "20:30"]
    bookings = []
    placed: set[tuple[str, str]] = set()
    for index in range(rng.randint(1, 4)):
        table = f"t_{rng.randint(1, len(sizes))}"
        at = rng.choice(slots)
        if (table, at) in placed:
            continue
        # Keep the seeded world legal: no two bookings may share a table at an
        # overlapping time, so one booking per table per evening here.
        if any(existing_table == table for existing_table, _ in placed):
            continue
        placed.add((table, at))
        capacity = sizes[int(table.split("_")[1]) - 1]
        bookings.append((f"BOOK{index:02d}", [table], at,
                         rng.randint(1, max(1, capacity))))
    world = house(sizes=sizes, combinable=pairs or None, bookings=bookings)
    closing = f"t_{rng.randint(1, len(sizes))}"
    assert_matches_brute_force(world, closing, "18:00", "23:00")
