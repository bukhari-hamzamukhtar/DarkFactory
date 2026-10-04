"""The browser product, in a real browser.

These are my own checks, separate from the supplied stage-2 suite: the competing-client
rules (a late search, a lost response, a refusal), the upgrade path with a pending
retry, a closure showing through the grid, and the layout holding at 375 CSS pixels as
well as on a desktop.

Skipped automatically when Playwright and a Chromium build are not available.
"""
from __future__ import annotations

import datetime as dt
import re
import time
from zoneinfo import ZoneInfo

import conftest as fx
from conftest import ADA, BOB, new_key, ok
import pytest

playwright_api = pytest.importorskip("playwright.sync_api",
                                     reason="needs playwright installed")

BERLIN = ZoneInfo("Europe/Berlin")
MOBILE = {"width": 375, "height": 667}
DESKTOP = {"width": 1280, "height": 800}


def sel(name: str) -> str:
    return f"[data-testid='{name}']"


@pytest.fixture(scope="session")
def browser(base_url):
    from urllib.parse import urlsplit
    parts = urlsplit(base_url)
    with playwright_api.sync_playwright() as driver:
        try:
            instance = driver.chromium.launch(
                channel="chromium",
                args=[f"--unsafely-treat-insecure-origin-as-secure="
                      f"{parts.scheme}://{parts.netloc}"])
        except Exception as problem:  # pragma: no cover - environment dependent
            pytest.skip(f"no Chromium available: {problem}")
        yield instance
        instance.close()


def open_page(browser, base_url, viewport):
    context = browser.new_context(base_url=base_url, viewport=viewport)
    context.set_default_timeout(15_000)
    return context, context.new_page()


@pytest.fixture
def page(browser, base_url):
    context, tab = open_page(browser, base_url, DESKTOP)
    yield tab
    context.close()


@pytest.fixture(params=[MOBILE, DESKTOP], ids=["375px", "1280px"])
def sized_page(browser, base_url, request):
    context, tab = open_page(browser, base_url, request.param)
    yield tab
    context.close()


@pytest.fixture
def seeded(reset, api):
    """The default world, plus a declared pair, and Ada's API client for the side door."""
    restaurant = {**fx.managed("u_ada"), "combinable": [["t_1", "t_2"]]}
    reset(fx.fixture(restaurants=[restaurant]))

    class World:
        pass

    world = World()
    world.date = fx.booking_date()
    world.ada = api().authenticated(ADA["email"], ADA["password"])
    world.bob = api().authenticated(BOB["email"], BOB["password"])
    world.anon = api()
    return world


# ---- helpers -------------------------------------------------------------

def sign_in(tab, email=None, password="correct horse"):
    tab.goto("/login")
    tab.fill(sel("login-email"), email or ADA["email"])
    tab.fill(sel("login-password"), password)
    tab.click(sel("login-submit"))
    tab.wait_for_selector(sel("current-user"))


def search(tab, date, party_size=4, restaurant="r_anker"):
    tab.goto("/")
    tab.select_option(sel("restaurant-select"), restaurant)
    tab.fill(sel("date-input"), date)
    tab.fill(sel("party-size-input"), str(party_size))
    tab.click(sel("search-button"))
    tab.wait_for_selector(f"{sel('availability-grid')}, {sel('no-slots')}")


def horizontal_overflow(tab) -> int:
    return tab.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth")


# ---- layout and basics at both widths ------------------------------------

@pytest.mark.parametrize("route,anchor", [
    ("/", "search-button"), ("/signup", "signup-submit"),
    ("/login", "login-submit"), ("/lookup", "lookup-submit"),
])
def test_every_route_fits_its_viewport(seeded, sized_page, route, anchor):
    sized_page.goto(route)
    sized_page.wait_for_selector(sel(anchor))
    assert horizontal_overflow(sized_page) <= 0, \
        f"{route} scrolls sideways at {sized_page.viewport_size}"


def test_the_grid_and_the_booking_form_fit_too(seeded, sized_page):
    sign_in(sized_page)
    search(sized_page, seeded.date, party_size=4)
    assert horizontal_overflow(sized_page) <= 0, "the grid scrolls sideways"
    sized_page.click(sel("slot-t_2-19:00"))
    sized_page.wait_for_selector(sel("booking-form"))
    assert horizontal_overflow(sized_page) <= 0, "the booking form scrolls sideways"
    sized_page.click(sel("booking-submit"))
    sized_page.wait_for_selector(sel("confirmation"))
    assert horizontal_overflow(sized_page) <= 0, "the confirmation scrolls sideways"


def test_every_input_has_a_visible_label(seeded, page):
    for route, fields in (("/signup", ["signup-email", "signup-password",
                                       "signup-display-name"]),
                          ("/login", ["login-email", "login-password"]),
                          ("/lookup", ["lookup-reference-input"]),
                          ("/", ["restaurant-select", "date-input",
                                 "party-size-input"])):
        page.goto(route)
        page.wait_for_selector(sel(fields[0]))
        for field in fields:
            labelled = page.evaluate(
                """(testid) => {
                    const node = document.querySelector(`[data-testid='${testid}']`);
                    if (!node) return false;
                    if (node.id && document.querySelector(`label[for='${node.id}']`))
                        return document.querySelector(`label[for='${node.id}']`).textContent.trim().length > 0;
                    return !!node.closest('label');
                }""", field)
            assert labelled, f"{field} on {route} has no visible label"


def test_keyboard_focus_is_apparent(seeded, page):
    page.goto("/")
    page.focus(sel("search-button"))
    outline = page.evaluate(
        """() => {
            const node = document.querySelector("[data-testid='search-button']");
            const style = getComputedStyle(node);
            return {width: style.outlineWidth, style: style.outlineStyle,
                    shadow: style.boxShadow};
        }""")
    visible = (outline["style"] != "none" and outline["width"] not in ("", "0px")) \
        or outline["shadow"] not in ("", "none")
    assert visible, f"focus is not visible: {outline}"


# ---- the grid ------------------------------------------------------------

def test_the_grid_agrees_with_the_api(seeded, page):
    search(page, seeded.date, party_size=4)
    slots = ok(seeded.anon.get("/availability", params={
        "restaurant_id": "r_anker", "date": seeded.date, "party_size": 4}))["slots"]
    for slot in slots:
        at = slot["starts_at_local"].split("T")[1]
        for table in ("t_1", "t_2", "t_3"):
            expected = "true" if table in slot["available_table_ids"] else "false"
            assert page.get_attribute(sel(f"slot-{table}-{at}"),
                                      "data-available") == expected, \
                f"slot-{table}-{at} disagrees with the API"
        # The declared pair has a cell of its own, with its own answer.
        pair_available = any(option["table_ids"] == ["t_1", "t_2"]
                             for option in slot["available_options"])
        assert page.get_attribute(sel(f"slot-t_1+t_2-{at}"), "data-available") == \
            ("true" if pair_available else "false")


def test_a_closure_shows_through_the_grid(seeded, page):
    """Stage 4: once a plan is applied, its closure must reach the screens."""
    def instant(at):
        hour, minute = (int(part) for part in at.split(":"))
        return dt.datetime.combine(dt.date.fromisoformat(seeded.date),
                                   dt.time(hour, minute), tzinfo=BERLIN).isoformat()

    plan = ok(seeded.ada.post("/restaurants/r_anker/replans", json={
        "table_id": "t_2", "from": instant("18:00"), "to": instant("23:00")},
        key=new_key()), 201)
    ok(seeded.ada.post(f"/restaurants/r_anker/replans/{plan['plan_id']}/apply",
                       json={}, key=new_key()), 201)

    sign_in(page)
    search(page, seeded.date, party_size=2)
    assert page.get_attribute(sel("slot-t_2-19:00"), "data-available") == "false"
    assert page.get_attribute(sel("slot-t_1+t_2-19:00"), "data-available") == "false", \
        "a pair containing a closed table is closed too"
    assert page.get_attribute(sel("slot-t_1-19:00"), "data-available") == "true"
    page.click(sel("slot-t_2-19:00"), force=True)
    page.wait_for_timeout(200)
    assert page.query_selector(sel("booking-form")) is None


def test_clicking_while_signed_out_asks_for_a_session(seeded, page):
    search(page, seeded.date, party_size=4)
    page.click(sel("slot-t_2-19:00"))
    page.wait_for_selector(f"{sel('auth-error')}, {sel('login-submit')}")
    assert page.query_selector(sel("booking-form")) is None


def test_a_bad_login_shows_an_error_and_a_good_one_does_not(seeded, page):
    page.goto("/login")
    assert page.query_selector(sel("auth-error")) is None
    page.fill(sel("login-email"), ADA["email"])
    page.fill(sel("login-password"), "wrong password")
    page.click(sel("login-submit"))
    page.wait_for_selector(sel("auth-error"))
    assert page.text_content(sel("auth-error")).strip()
    page.fill(sel("login-password"), "correct horse")
    page.click(sel("login-submit"))
    page.wait_for_selector(sel("current-user"))
    assert page.query_selector(sel("auth-error")) is None, \
        "the error goes away once the sign-in works"
    assert ADA["display_name"] in page.text_content(sel("current-user"))
    page.click(sel("logout-button"))
    page.wait_for_selector(sel("current-user"), state="detached")


# ---- booking -------------------------------------------------------------

def test_a_booking_reaches_a_confirmation(seeded, page):
    sign_in(page)
    search(page, seeded.date, party_size=4)
    page.click(sel("slot-t_2-19:00"))
    page.wait_for_selector(sel("booking-form"))
    assert "19:00" in page.text_content(sel("booking-summary"))
    assert "2" in page.text_content(sel("booking-summary"))
    assert page.input_value(sel("booking-party-size")) == "4"

    page.click(sel("booking-submit"))
    page.wait_for_selector(sel("confirmation"))
    reference = page.text_content(sel("confirmation-reference")).strip()
    assert re.fullmatch(r"[A-Z0-9]{6,12}", reference), reference
    details = page.text_content(sel("confirmation-details"))
    for expected in ("Zum Anker", "2", "19:00"):
        assert expected in details, f"{expected!r} missing from {details!r}"
    assert "2" in page.text_content(sel("confirmation-tables"))
    assert page.query_selector(sel("booking-form")) is not None, \
        "the form stays on screen after success"


def test_a_combination_books_and_names_both_tables(seeded, page):
    sign_in(page)
    search(page, seeded.date, party_size=6)
    page.click(sel("slot-t_1+t_2-19:00"))
    page.wait_for_selector(sel("booking-form"))
    summary = page.text_content(sel("booking-summary"))
    assert "1" in summary and "2" in summary, summary
    page.click(sel("booking-submit"))
    page.wait_for_selector(sel("confirmation"))
    tables = page.text_content(sel("confirmation-tables"))
    assert "1" in tables and "2" in tables, tables
    reference = page.text_content(sel("confirmation-reference")).strip()
    booking = ok(seeded.ada.get(f"/reservations/{reference}"))
    assert booking["table_ids"] == ["t_1", "t_2"]


def test_submitting_twice_replays_and_changing_a_field_does_not(seeded, page):
    sign_in(page)
    search(page, seeded.date, party_size=4)
    page.click(sel("slot-t_2-19:00"))
    page.wait_for_selector(sel("booking-form"))
    page.click(sel("booking-submit"))
    page.wait_for_selector(sel("confirmation"))
    first = page.text_content(sel("confirmation-reference")).strip()

    page.click(sel("booking-submit"))
    page.wait_for_timeout(600)
    assert page.query_selector(sel("booking-error")) is None
    assert page.text_content(sel("confirmation-reference")).strip() == first
    confirmed = [booking for booking in ok(seeded.ada.get("/reservations"))["reservations"]
                 if booking["status"] == "confirmed"]
    assert len(confirmed) == 1, "an unchanged form retries rather than books again"

    page.fill(sel("booking-party-size"), "2")
    page.click(sel("booking-submit"))
    page.wait_for_selector(sel("booking-error"))
    assert page.text_content(sel("booking-error")).strip()


def test_a_table_taken_after_the_form_opens_refuses_and_refreshes(seeded, page):
    sign_in(page)
    search(page, seeded.date, party_size=4)
    page.click(sel("slot-t_2-19:00"))
    page.wait_for_selector(sel("booking-form"))
    page.fill(sel("booking-party-size"), "3")

    ok(seeded.bob.book(table_id="t_2", party_size=4,
                       starts_at_local=fx.local(seeded.date, "19:00")), 201)

    page.click(sel("booking-submit"))
    page.wait_for_selector(sel("booking-error"))
    assert page.query_selector(sel("confirmation")) is None, \
        "a refused attempt shows no confirmation"
    assert page.query_selector(sel("booking-form")) is not None, \
        "the selected form is preserved"
    assert page.input_value(sel("booking-party-size")) == "3", \
        "and so are its inputs"
    page.wait_for_function(
        """() => {
            const cell = document.querySelector("[data-testid='slot-t_2-19:00']");
            return cell && cell.getAttribute('data-available') === 'false';
        }""")


def test_a_lost_response_is_uncertain_and_a_retry_settles_it(seeded, page):
    """The hardest rule: the booking commits but the browser never hears.

    The request is forwarded to the service and its response is then dropped, so the
    table really is held. The page must say the outcome is unknown -- not that it
    failed -- and the same form, unchanged, must settle it on the next press and show
    the original reference.
    """
    sign_in(page)
    search(page, seeded.date, party_size=4)
    page.click(sel("slot-t_2-19:00"))
    page.wait_for_selector(sel("booking-form"))

    swallowed = {"count": 0}

    def swallow(route):
        if route.request.method == "POST" and swallowed["count"] == 0:
            swallowed["count"] += 1
            route.fetch()          # the service commits
            route.abort()          # the browser never sees the answer
            return
        route.continue_()

    page.route("**/reservations", swallow)
    page.click(sel("booking-submit"))
    page.wait_for_selector(sel("booking-uncertain"))
    assert page.text_content(sel("booking-uncertain")).strip(), \
        "the uncertainty must say something"
    assert page.query_selector(sel("booking-error")) is None
    assert page.query_selector(sel("confirmation")) is None
    assert swallowed["count"] == 1

    confirmed = [booking for booking in ok(seeded.ada.get("/reservations"))["reservations"]
                 if booking["status"] == "confirmed"]
    assert len(confirmed) == 1, "the booking did commit"

    page.click(sel("booking-submit"))
    page.wait_for_selector(sel("confirmation"))
    assert page.text_content(sel("confirmation-reference")).strip() == \
        confirmed[0]["reference"], "the retry recovers the original reference"
    assert page.query_selector(sel("booking-uncertain")) is None
    assert page.query_selector(sel("booking-error")) is None
    still = [booking for booking in ok(seeded.ada.get("/reservations"))["reservations"]
             if booking["status"] == "confirmed"]
    assert len(still) == 1, "and books nothing further"


def test_a_pending_retry_survives_an_import_between_requests(seeded, page):
    """The upgrade path: the state is replaced while the browser waits, and the
    session and the pending retry both still work."""
    sign_in(page)
    search(page, seeded.date, party_size=4)
    page.click(sel("slot-t_2-19:00"))
    page.wait_for_selector(sel("booking-form"))

    swallowed = {"count": 0}

    def swallow(route):
        if route.request.method == "POST" and swallowed["count"] == 0:
            swallowed["count"] += 1
            route.fetch()
            route.abort()
            return
        route.continue_()

    page.route("**/reservations", swallow)
    page.click(sel("booking-submit"))
    page.wait_for_selector(sel("booking-uncertain"))

    # An export and an import land between the browser's requests.
    snapshot = ok(seeded.anon.get("/_test/export", timeout=fx.CONTROL_TIMEOUT))
    resp = seeded.anon.post("/_test/import", json=snapshot,
                            timeout=fx.CONTROL_TIMEOUT)
    assert resp.status_code == 204, fx.described(resp)

    page.click(sel("booking-submit"))
    page.wait_for_selector(sel("confirmation"))
    reference = page.text_content(sel("confirmation-reference")).strip()
    assert ok(seeded.ada.get(f"/reservations/{reference}"))["status"] == "confirmed"
    assert page.query_selector(sel("current-user")) is not None, \
        "the browser is still signed in after the upgrade"

    # And the reference still works through the lookup screen.
    page.goto("/lookup")
    page.fill(sel("lookup-reference-input"), reference)
    page.click(sel("lookup-submit"))
    page.wait_for_selector(sel("reservation-detail"))
    assert page.text_content(sel("reservation-status")).strip() == "confirmed"


def test_a_late_search_cannot_replace_a_newer_one(seeded, page):
    """Search A is held up until after search B has answered; B must stand."""
    sign_in(page)
    page.goto("/")
    page.select_option(sel("restaurant-select"), "r_anker")
    page.fill(sel("date-input"), seeded.date)

    held = {"count": 0}

    def hold_first(route):
        if held["count"] == 0:
            held["count"] += 1
            time.sleep(2.5)
        route.continue_()

    page.route("**/availability**", hold_first)

    # A: a party of 2, which every table can seat.
    page.fill(sel("party-size-input"), "2")
    page.click(sel("search-button"))
    # B: a party of 6, which only t_3 and the pair can seat.
    page.fill(sel("party-size-input"), "6")
    page.click(sel("search-button"))

    page.wait_for_selector(sel("availability-grid"))
    page.wait_for_timeout(3500)   # long enough for A's held answer to arrive

    assert page.get_attribute(sel("slot-t_1-19:00"), "data-available") == "false", \
        "the grid describes the newer search, not the late one"
    assert page.get_attribute(sel("slot-t_3-19:00"), "data-available") == "true"
    assert page.get_attribute(sel("slot-t_1+t_2-19:00"), "data-available") == "true"
    assert page.query_selector(sel("booking-form")) is None, \
        "a new search closes the form that belonged to the old one"


# ---- lookup --------------------------------------------------------------

def test_lookup_shows_cancels_and_frees_the_table(seeded, page):
    booking = ok(seeded.ada.book(table_id="t_2", party_size=4,
                                 starts_at_local=fx.local(seeded.date, "19:00")), 201)
    sign_in(page)
    page.goto("/lookup")
    page.fill(sel("lookup-reference-input"), booking["reference"])
    page.click(sel("lookup-submit"))
    page.wait_for_selector(sel("reservation-detail"))
    assert page.text_content(sel("reservation-status")).strip() == "confirmed"
    assert "2" in page.text_content(sel("reservation-tables"))

    page.click(sel("reservation-cancel-button"))
    page.wait_for_selector(sel("reservation-cancel-button"), state="detached")
    assert page.text_content(sel("reservation-status")).strip() == "cancelled"

    search(page, seeded.date, party_size=4)
    assert page.get_attribute(sel("slot-t_2-19:00"), "data-available") == "true"


def test_an_unknown_reference_shows_an_error(seeded, page):
    sign_in(page)
    page.goto("/lookup")
    page.fill(sel("lookup-reference-input"), "ZZZZZZ")
    page.click(sel("lookup-submit"))
    page.wait_for_selector(sel("reservation-error"))
    assert page.query_selector(sel("reservation-detail")) is None


def test_a_refused_cancel_shows_an_error_and_keeps_the_booking(api, reset, browser,
                                                              base_url):
    """A booking inside its cutoff cannot be cancelled, and the screen says so."""
    locked = {**fx.managed("u_ada"), "cancellation_cutoff_minutes": 60 * 24 * 3650}
    reset(fx.fixture(restaurants=[locked]))
    ada = api().authenticated(ADA["email"], ADA["password"])
    date = fx.booking_date()
    booking = ok(ada.book(table_id="t_2", party_size=4,
                          starts_at_local=fx.local(date, "19:00")), 201)
    context, tab = open_page(browser, base_url, DESKTOP)
    try:
        sign_in(tab)
        tab.goto("/lookup")
        tab.fill(sel("lookup-reference-input"), booking["reference"])
        tab.click(sel("lookup-submit"))
        tab.wait_for_selector(sel("reservation-cancel-button"))
        tab.click(sel("reservation-cancel-button"))
        tab.wait_for_selector(sel("reservation-error"))
        assert tab.text_content(sel("reservation-status")).strip() == "confirmed"
        assert ok(ada.get(f"/reservations/{booking['reference']}"))["status"] == \
            "confirmed"
    finally:
        context.close()


def test_a_closed_day_shows_no_slots(api, reset, browser, base_url):
    date = fx.booking_date()
    weekday = fx.WEEKDAYS[dt.date.fromisoformat(date).weekday()]
    closed = [hours for hours in fx.all_week() if hours["weekday"] != weekday]
    reset(fx.fixture(restaurants=[fx.managed("u_ada", opening_hours=closed)]))
    context, tab = open_page(browser, base_url, MOBILE)
    try:
        search(tab, date, party_size=2)
        tab.wait_for_selector(sel("no-slots"))
        assert tab.query_selector(sel("availability-grid")) is None
        assert horizontal_overflow(tab) <= 0
    finally:
        context.close()


def test_signup_signs_you_in(seeded, page):
    page.goto("/signup")
    page.fill(sel("signup-email"), "carol@example.com")
    page.fill(sel("signup-password"), "correct horse")
    page.fill(sel("signup-display-name"), "Carol")
    page.click(sel("signup-submit"))
    page.wait_for_selector(sel("current-user"))
    assert "Carol" in page.text_content(sel("current-user"))
