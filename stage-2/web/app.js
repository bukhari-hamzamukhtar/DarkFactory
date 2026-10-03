/* Tablekeeper — the browser client.
 *
 * Three rules shape this file, and all three come from the requirements:
 *
 *   1. The server is authoritative. Nothing here manufactures a confirmation from
 *      cached data; a reference is only ever shown because a response carried one.
 *   2. A late answer must not win. Every search carries a sequence number and a
 *      response whose number is no longer the newest is dropped on the floor.
 *   3. An outcome that was never seen is not a failure. If a booking response is
 *      lost, the form keeps its idempotency key and body, says plainly that the
 *      outcome is unknown, and a retry settles it -- as a replay if the booking did
 *      commit, as an error if it did not.
 */
(function () {
  "use strict";

  var SESSION_KEY = "tablekeeper.session";
  var route = document.body.dataset.route;

  // ---- small helpers ----------------------------------------------------

  function testid(name) {
    return document.querySelector('[data-testid="' + name + '"]');
  }

  function make(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function clear(node) {
    if (node) while (node.firstChild) node.removeChild(node.firstChild);
  }

  function busy(button, state) {
    if (!button) return;
    button.dataset.busy = state ? "true" : "false";
    button.disabled = !!state;
  }

  function notice(kind, id, message) {
    var box = make("div", "notice notice--" + kind);
    if (id) box.setAttribute("data-testid", id);
    var icon = make("i", "notice__icon", kind === "good" ? "✓" : kind === "warn" ? "⚠" : "✕");
    icon.setAttribute("aria-hidden", "true");
    box.append(icon, make("span", null, message));
    return box;
  }

  function newKey() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      return window.crypto.randomUUID();
    }
    var bytes = new Uint8Array(16);
    (window.crypto || {}).getRandomValues
      ? window.crypto.getRandomValues(bytes)
      : bytes.forEach(function (_, i) { bytes[i] = Math.floor(Math.random() * 256); });
    return Array.prototype.map.call(bytes, function (b) {
      return ("0" + b.toString(16)).slice(-2);
    }).join("");
  }

  var WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  // humanStamp reads a bare local stamp (YYYY-MM-DDTHH:MM) without ever turning it
  // into an instant: it is wall-clock at the restaurant, and the browser's own zone
  // has no business shifting it.
  function humanStamp(stamp) {
    var parts = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(stamp || "");
    if (!parts) return stamp || "";
    var day = new Date(Date.UTC(+parts[1], +parts[2] - 1, +parts[3]));
    return WEEKDAYS[day.getUTCDay()] + " " + (+parts[3]) + " " + MONTHS[+parts[2] - 1] +
           " " + parts[1] + " at " + parts[4] + ":" + parts[5];
  }

  function timeOf(stamp) {
    return (stamp || "").slice(-5);
  }

  function sameMembers(first, second) {
    if (!first || !second || first.length !== second.length) return false;
    for (var i = 0; i < first.length; i++) {
      if (second.indexOf(first[i]) === -1) return false;
    }
    return true;
  }

  // ---- session ----------------------------------------------------------

  function loadSession() {
    try {
      var raw = window.localStorage.getItem(SESSION_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch (err) {
      return null;
    }
  }

  var session = loadSession();

  function adoptSession(payload, email) {
    var name = (payload && payload.display_name) || "";
    if (!name && email) name = String(email).split("@")[0];
    session = { token: payload.token, name: name, userId: payload.user_id };
    try { window.localStorage.setItem(SESSION_KEY, JSON.stringify(session)); } catch (err) {}
    renderSession();
  }

  function endSession() {
    session = null;
    try { window.localStorage.removeItem(SESSION_KEY); } catch (err) {}
    renderSession();
  }

  function renderSession() {
    var slot = document.querySelector("[data-session-slot]");
    if (!slot) return;
    Array.prototype.forEach.call(slot.querySelectorAll("[data-session-live]"), function (node) {
      node.remove();
    });
    var signedOut = slot.querySelectorAll("[data-signed-out]");
    if (!session) {
      Array.prototype.forEach.call(signedOut, function (node) { node.hidden = false; });
      return;
    }
    Array.prototype.forEach.call(signedOut, function (node) { node.hidden = true; });

    var name = session.name || "Guest";
    var who = make("span", "session__user");
    who.dataset.sessionLive = "true";
    who.setAttribute("data-testid", "current-user");
    var avatar = make("span", "session__avatar", name.slice(0, 1).toUpperCase());
    avatar.setAttribute("aria-hidden", "true");
    who.append(avatar, make("span", null, name));

    var out = make("button", "button button--quiet", "Sign out");
    out.type = "button";
    out.dataset.sessionLive = "true";
    out.setAttribute("data-testid", "logout-button");
    out.addEventListener("click", endSession);

    slot.append(who, out);
  }

  // ---- the API ----------------------------------------------------------

  // request resolves with the status and parsed body. It rejects only when the
  // outcome is genuinely unknown -- the connection failed, or the response was
  // never delivered -- which is the case the booking form has to handle specially.
  async function request(method, path, options) {
    options = options || {};
    var headers = {};
    if (options.body !== undefined) headers["Content-Type"] = "application/json";
    var token = options.anonymous ? null : (session && session.token);
    if (token) headers["Authorization"] = "Bearer " + token;
    if (options.key) headers["Idempotency-Key"] = options.key;

    var response = await fetch(path, {
      method: method,
      headers: headers,
      cache: "no-store",
      body: options.body === undefined ? undefined : JSON.stringify(options.body)
    });
    var data = null;
    var text = await response.text();
    if (text) {
      try { data = JSON.parse(text); } catch (err) { data = null; }
    }
    return {
      status: response.status,
      data: data,
      code: data && data.error ? data.error.code : null,
      message: data && data.error ? data.error.message : null
    };
  }

  // ---- auth screens -----------------------------------------------------

  function authSlot() {
    return document.querySelector("[data-auth-slot]");
  }

  function clearAuth() {
    clear(authSlot());
  }

  function showAuthError(message) {
    var slot = authSlot();
    if (!slot) return;
    clear(slot);
    slot.append(notice("error", "auth-error", message || "That did not work."));
  }

  function showSignedIn(message) {
    var slot = authSlot();
    if (!slot) return;
    clear(slot);
    var box = notice("good", null, message);
    var link = make("a", null, " Find a table");
    link.href = "/";
    box.append(link);
    slot.append(box);
  }

  function wireSignup() {
    var form = document.querySelector("[data-signup-form]");
    if (!form) return;
    form.addEventListener("submit", async function (event) {
      event.preventDefault();
      clearAuth();
      var button = testid("signup-submit");
      var email = testid("signup-email").value.trim();
      var password = testid("signup-password").value;
      var name = testid("signup-display-name").value.trim();
      var body = { email: email, password: password };
      if (name) body.display_name = name;
      busy(button, true);
      try {
        var result = await request("POST", "/auth/signup", { body: body, anonymous: true });
        if (result.status === 201) {
          adoptSession(result.data, email);
          showSignedIn("Welcome. Your account is ready.");
        } else {
          showAuthError(result.message || "We could not create that account.");
        }
      } catch (err) {
        showAuthError("We could not reach the service. Please try again.");
      } finally {
        busy(button, false);
      }
    });
  }

  function wireLogin() {
    var form = document.querySelector("[data-login-form]");
    if (!form) return;
    form.addEventListener("submit", async function (event) {
      event.preventDefault();
      clearAuth();
      var button = testid("login-submit");
      var email = testid("login-email").value.trim();
      var password = testid("login-password").value;
      busy(button, true);
      try {
        var result = await request("POST", "/auth/login", {
          body: { email: email, password: password }, anonymous: true
        });
        if (result.status === 200) {
          adoptSession(result.data, email);
          showSignedIn("Signed in.");
        } else {
          showAuthError(result.message || "Check your email and password.");
        }
      } catch (err) {
        showAuthError("We could not reach the service. Please try again.");
      } finally {
        busy(button, false);
      }
    });
  }

  // ---- search and booking ----------------------------------------------

  var search = {
    sequence: 0,
    context: null,     // what the newest search asked for
    restaurant: null,  // its restaurant, with table labels
    slots: [],
    selection: null,   // the seating the form is for
    pending: null      // {key, serialized, body} -- the retry identity
  };

  function resultsHost() {
    return document.querySelector("[data-results]");
  }

  function bookingHost() {
    return document.querySelector("[data-booking-slot]");
  }

  function availabilityPath(context) {
    return "/availability?restaurant_id=" + encodeURIComponent(context.restaurantId) +
           "&date=" + encodeURIComponent(context.date) +
           "&party_size=" + encodeURIComponent(context.partySize);
  }

  function labelOf(tableID) {
    var tables = (search.restaurant && search.restaurant.tables) || [];
    for (var i = 0; i < tables.length; i++) {
      if (tables[i].id === tableID) return "Table " + tables[i].label;
    }
    return tableID;
  }

  function seatingLabel(tableIDs) {
    return tableIDs.map(labelOf).join(" + ");
  }

  function wireSearch() {
    var form = document.querySelector("[data-search-form]");
    if (!form) return;
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      runSearch();
    });
    refreshRestaurants();
  }

  // refreshRestaurants keeps the picker honest if the page has been open across a
  // change of fixture. The options are server-rendered too, so the select is never
  // empty while this is in flight.
  async function refreshRestaurants() {
    var select = testid("restaurant-select");
    if (!select) return;
    try {
      var result = await request("GET", "/restaurants", { anonymous: true });
      if (result.status !== 200 || !result.data || !result.data.restaurants) return;
      var wanted = select.value;
      var listed = result.data.restaurants;
      var same = listed.length === select.options.length && listed.every(function (rest, i) {
        return select.options[i] && select.options[i].value === rest.id;
      });
      if (same) return;
      clear(select);
      listed.forEach(function (rest) {
        var option = make("option", null, rest.name);
        option.value = rest.id;
        select.append(option);
      });
      if (listed.some(function (rest) { return rest.id === wanted; })) select.value = wanted;
    } catch (err) {
      /* The picker keeps whatever the server rendered. */
    }
  }

  function readContext() {
    return {
      restaurantId: testid("restaurant-select").value,
      date: testid("date-input").value,
      partySize: testid("party-size-input").value
    };
  }

  function showSkeleton() {
    var host = resultsHost();
    clear(host);
    var skeleton = make("div", "skeleton");
    skeleton.setAttribute("aria-hidden", "true");
    for (var i = 0; i < 4; i++) skeleton.append(make("div", "skeleton__row"));
    host.append(skeleton);
    var waiting = make("p", "muted", "Looking for tables…");
    host.append(waiting);
  }

  async function runSearch() {
    var sequence = ++search.sequence;
    var context = readContext();
    var button = testid("search-button");
    busy(button, true);
    showSkeleton();
    closeBooking();

    var detail, availability;
    try {
      detail = await request("GET", "/restaurants/" + encodeURIComponent(context.restaurantId),
                             { anonymous: true });
      availability = await request("GET", availabilityPath(context), { anonymous: true });
    } catch (err) {
      if (sequence !== search.sequence) return;   // a newer search is answering
      busy(button, false);
      showResultsProblem("We could not reach the service. Please try again.");
      return;
    }
    // Everything below belongs to this search only. A search that has been
    // superseded stops here: its results must never replace the newer ones.
    if (sequence !== search.sequence) return;
    busy(button, false);

    if (detail.status !== 200) {
      showResultsProblem(detail.message || "That restaurant is not available.");
      return;
    }
    if (availability.status !== 200) {
      showResultsProblem(availability.message || "We could not read that day.");
      return;
    }
    search.context = context;
    search.restaurant = detail.data;
    search.slots = availability.data.slots || [];
    renderContextNote();
    renderGrid();
  }

  function renderContextNote() {
    var note = document.querySelector("[data-search-context]");
    if (!note || !search.context) return;
    var guests = search.context.partySize;
    note.textContent = "Showing " + (search.restaurant.name || "") + " · " +
      humanStamp(search.context.date + "T00:00").replace(" at 00:00", "") + " · " +
      guests + (String(guests) === "1" ? " guest" : " guests");
    note.hidden = false;
  }

  function showResultsProblem(message) {
    var host = resultsHost();
    clear(host);
    host.append(notice("error", null, message));
  }

  function renderGrid() {
    var host = resultsHost();
    clear(host);
    if (!search.slots.length) {
      var empty = make("div", "empty");
      empty.setAttribute("data-testid", "no-slots");
      empty.append(make("p", "empty__title", "No sittings that day"));
      empty.append(make("p", null,
        "The restaurant is closed, or every sitting has finished. Try another date."));
      host.append(empty);
      return;
    }
    var grid = make("div", "day");
    grid.setAttribute("data-testid", "availability-grid");
    search.slots.forEach(function (slot) { grid.append(slotRow(slot)); });
    host.append(grid);
    markSelection();
  }

  function slotRow(slot) {
    var row = make("section", "slot");
    var at = timeOf(slot.starts_at_local);
    var heading = make("h3", "slot__time", at);
    var options = make("div", "slot__options");

    var tables = (search.restaurant && search.restaurant.tables) || [];
    var free = slot.available_table_ids || [];
    var offered = 0;

    tables.forEach(function (table) {
      var available = free.indexOf(table.id) !== -1;
      if (available) offered++;
      options.append(cell({
        id: "slot-" + table.id + "-" + at,
        tableIds: [table.id],
        label: "Table " + table.label,
        seats: table.capacity,
        available: available,
        stamp: slot.starts_at_local,
        combo: false
      }));
    });

    var pairs = (search.restaurant && search.restaurant.combinable) || [];
    var openOptions = slot.available_options || [];
    pairs.forEach(function (pair) {
      if (!pair || pair.length !== 2) return;
      var seats = 0;
      pair.forEach(function (id) {
        tables.forEach(function (table) { if (table.id === id) seats += table.capacity; });
      });
      var available = openOptions.some(function (option) {
        return sameMembers(option.table_ids || [], pair);
      });
      if (available) offered++;
      options.append(cell({
        id: "slot-" + pair[0] + "+" + pair[1] + "-" + at,
        tableIds: [pair[0], pair[1]],
        label: "Tables " + pair.map(function (id) {
          var found = "";
          tables.forEach(function (table) { if (table.id === id) found = table.label; });
          return found || id;
        }).join(" + "),
        seats: seats,
        available: available,
        stamp: slot.starts_at_local,
        combo: true
      }));
    });

    heading.append(make("span", "slot__count",
      offered === 0 ? "fully booked" : offered + (offered === 1 ? " option" : " options")));
    row.append(heading, options);
    return row;
  }

  function cell(spec) {
    var button = make("button", "cell" + (spec.combo ? " cell--combo" : ""));
    button.type = "button";
    button.setAttribute("data-testid", spec.id);
    button.setAttribute("data-available", spec.available ? "true" : "false");
    button.setAttribute("aria-pressed", "false");
    button.append(make("span", "cell__label", spec.label));
    button.append(make("span", "cell__meta",
      spec.available ? "seats " + spec.seats : "unavailable"));
    if (!spec.available) {
      button.disabled = true;
      button.setAttribute("aria-disabled", "true");
      return button;
    }
    button.addEventListener("click", function () {
      if (!session) {
        showAuthError("Please sign in to hold a table.");
        var slot = authSlot();
        if (slot) slot.scrollIntoView({ block: "nearest" });
        return;
      }
      clearAuth();
      openBooking({
        tableIds: spec.tableIds.slice(),
        label: spec.label,
        seats: spec.seats,
        stamp: spec.stamp
      });
    });
    return button;
  }

  function markSelection() {
    if (!search.selection) return;
    var at = timeOf(search.selection.stamp);
    var id = search.selection.tableIds.length === 1
      ? "slot-" + search.selection.tableIds[0] + "-" + at
      : "slot-" + search.selection.tableIds.join("+") + "-" + at;
    var node = testid(id);
    if (node) node.setAttribute("aria-pressed", "true");
  }

  function closeBooking() {
    search.selection = null;
    search.pending = null;
    clear(bookingHost());
  }

  function openBooking(selection) {
    search.selection = selection;
    search.pending = null;          // a new seating is a new booking identity
    var host = bookingHost();
    clear(host);

    var card = make("section", "card booking");
    card.setAttribute("data-testid", "booking-form");
    card.append(make("h2", "card__title", "Hold this table"));
    var summary = make("p", "booking__summary",
      selection.label + " · " + humanStamp(selection.stamp));
    summary.setAttribute("data-testid", "booking-summary");
    card.append(summary);

    var form = make("form", "booking__row");
    var field = make("div", "field");
    var label = make("label", "field__label", "Guests");
    label.setAttribute("for", "booking-party");
    var input = make("input", "field__input");
    input.id = "booking-party";
    input.setAttribute("data-testid", "booking-party-size");
    input.type = "number";
    input.min = "1";
    input.step = "1";
    input.value = String(search.context ? search.context.partySize : selection.seats);
    field.append(label, input);

    var submit = make("button", "button button--primary", "Confirm booking");
    submit.type = "submit";
    submit.setAttribute("data-testid", "booking-submit");

    form.append(field, submit);
    form.addEventListener("submit", submitBooking);
    card.append(form);
    var feedback = make("div", "feedback");
    feedback.setAttribute("data-booking-feedback", "true");
    card.append(feedback);
    host.append(card);
    markSelection();
    card.scrollIntoView({ block: "nearest" });
  }

  function bookingFeedback() {
    return document.querySelector("[data-booking-feedback]");
  }

  function clearBookingFeedback() {
    clear(bookingFeedback());
  }

  function showBookingError(message) {
    var slot = bookingFeedback();
    if (!slot) return;
    clear(slot);
    slot.append(notice("error", "booking-error", message || "That booking was refused."));
  }

  function showBookingUncertain() {
    var slot = bookingFeedback();
    if (!slot) return;
    clear(slot);
    slot.append(notice("warn", "booking-uncertain",
      "We did not hear back, so we cannot say yet whether this table is held. " +
      "Press Confirm booking again: it will settle the same request rather than make a second one."));
  }

  function dropConfirmation() {
    var existing = testid("confirmation");
    if (existing) existing.remove();
  }

  function showConfirmation(reservation) {
    dropConfirmation();
    var card = make("section", "card confirmation");
    card.setAttribute("data-testid", "confirmation");
    card.append(make("p", "confirmation__eyebrow", "Table held"));

    var reference = make("p", "confirmation__reference", reservation.reference);
    reference.setAttribute("data-testid", "confirmation-reference");
    card.append(reference);

    var tableIDs = reservation.table_ids ||
      (reservation.table_id ? [reservation.table_id] : []);
    var restaurantName = (search.restaurant && search.restaurant.name) || reservation.restaurant_id;

    var details = make("p", "confirmation__details",
      restaurantName + " · " + seatingLabel(tableIDs) + " · " +
      humanStamp(reservation.starts_at_local) + " · " +
      reservation.party_size + (reservation.party_size === 1 ? " guest" : " guests"));
    details.setAttribute("data-testid", "confirmation-details");
    card.append(details);

    var tables = make("p", "confirmation__tables", seatingLabel(tableIDs));
    tables.setAttribute("data-testid", "confirmation-tables");
    card.append(tables);

    card.append(make("p", "muted",
      "Keep the reference: you can view or cancel this booking from My booking."));
    bookingHost().append(card);
  }

  async function submitBooking(event) {
    event.preventDefault();
    var selection = search.selection;
    if (!selection) return;
    var input = testid("booking-party-size");
    var typed = input.value.trim();
    var party = /^\d+$/.test(typed) ? Number(typed) : typed;

    var body = {
      restaurant_id: search.context.restaurantId,
      starts_at_local: selection.stamp,
      party_size: party
    };
    // A single table is sent the way stage 1 spelt it, so a retry of a booking made
    // before an upgrade is byte-for-byte the same request (§7, §10).
    if (selection.tableIds.length === 1) {
      body.table_id = selection.tableIds[0];
    } else {
      body.table_ids = selection.tableIds.slice();
    }

    var serialized = JSON.stringify(body);
    if (!search.pending || search.pending.serialized !== serialized) {
      // An unchanged form retries the same request; a changed field is a new one.
      search.pending = { key: newKey(), serialized: serialized, body: body };
    }

    var button = testid("booking-submit");
    clearBookingFeedback();
    busy(button, true);

    var result;
    try {
      result = await request("POST", "/reservations", {
        body: search.pending.body, key: search.pending.key
      });
    } catch (err) {
      // The response never arrived. The booking may well have committed, so this
      // is neither a success nor a failure: keep the key and the body, say so, and
      // let the retry find out.
      busy(button, false);
      showBookingUncertain();
      return;
    }
    busy(button, false);

    if (result.status === 201 || result.status === 200) {
      clearBookingFeedback();
      showConfirmation(result.data);
      refreshAvailability();
      return;
    }
    dropConfirmation();
    if (result.status === 409 && result.code === "table_unavailable") {
      showBookingError("Another diner took that table first. " +
        "Your choice is still here -- pick another table or time.");
      refreshAvailability();
      return;
    }
    if (result.status === 401) {
      showBookingError("Your session has expired. Please sign in again.");
      return;
    }
    showBookingError(result.message || "That booking was refused.");
  }

  // refreshAvailability re-reads the day the form belongs to, leaving the form and
  // its inputs exactly as the diner left them.
  async function refreshAvailability() {
    if (!search.context) return;
    var sequence = ++search.sequence;
    try {
      var result = await request("GET", availabilityPath(search.context), { anonymous: true });
      if (sequence !== search.sequence || result.status !== 200) return;
      search.slots = result.data.slots || [];
      renderGrid();
    } catch (err) {
      /* Leave the grid as it was; the next search will correct it. */
    }
  }

  // ---- lookup -----------------------------------------------------------

  function reservationHost() {
    return document.querySelector("[data-reservation-slot]");
  }

  function clearReservation() {
    clear(reservationHost());
  }

  function showReservationError(message) {
    var host = reservationHost();
    clear(host);
    host.append(notice("error", "reservation-error",
      message || "We could not find that booking."));
  }

  function wireLookup() {
    var form = document.querySelector("[data-lookup-form]");
    if (!form) return;
    form.addEventListener("submit", async function (event) {
      event.preventDefault();
      var reference = testid("lookup-reference-input").value.trim().toUpperCase();
      var button = testid("lookup-submit");
      clearReservation();
      if (!reference) {
        showReservationError("Enter the reference from your confirmation.");
        return;
      }
      if (!session) {
        showReservationError("Please sign in to see your booking.");
        return;
      }
      busy(button, true);
      try {
        var result = await request("GET", "/reservations/" + encodeURIComponent(reference));
        if (result.status === 200) {
          await renderReservation(result.data);
        } else if (result.status === 401) {
          showReservationError("Please sign in again to see your booking.");
        } else {
          showReservationError("We have no booking with that reference.");
        }
      } catch (err) {
        showReservationError("We could not reach the service. Please try again.");
      } finally {
        busy(button, false);
      }
    });
  }

  var lookupRestaurants = {};

  async function restaurantFor(id) {
    if (lookupRestaurants[id]) return lookupRestaurants[id];
    try {
      var result = await request("GET", "/restaurants/" + encodeURIComponent(id),
                                 { anonymous: true });
      if (result.status === 200) {
        lookupRestaurants[id] = result.data;
        return result.data;
      }
    } catch (err) {
      /* fall through to ids */
    }
    return null;
  }

  async function renderReservation(reservation) {
    var host = reservationHost();
    clear(host);
    var rest = await restaurantFor(reservation.restaurant_id);
    var tableIDs = reservation.table_ids ||
      (reservation.table_id ? [reservation.table_id] : []);
    var labels = tableIDs.map(function (id) {
      var label = id;
      ((rest && rest.tables) || []).forEach(function (table) {
        if (table.id === id) label = "Table " + table.label;
      });
      return label;
    }).join(" + ");

    var card = make("section", "card");
    card.setAttribute("data-testid", "reservation-detail");
    card.append(make("h2", "card__title", (rest && rest.name) || reservation.restaurant_id));

    var status = make("span", "pill pill--" + reservation.status, reservation.status);
    status.setAttribute("data-testid", "reservation-status");
    var statusLine = make("p", null);
    statusLine.append(status);
    card.append(statusLine);

    var tables = make("p", "confirmation__tables", labels);
    tables.setAttribute("data-testid", "reservation-tables");
    card.append(tables);

    var row = make("div", "detail__row");
    [["Reference", reservation.reference],
     ["When", humanStamp(reservation.starts_at_local)],
     ["Guests", String(reservation.party_size)]].forEach(function (pair) {
      var item = make("div", "detail__item");
      item.append(make("strong", null, pair[0]));
      item.append(make("span", null, pair[1]));
      row.append(item);
    });
    card.append(row);

    if (reservation.status === "confirmed") {
      var cancel = make("button", "button button--danger", "Cancel booking");
      cancel.type = "button";
      cancel.setAttribute("data-testid", "reservation-cancel-button");
      cancel.addEventListener("click", function () { cancelReservation(reservation, cancel); });
      var actions = make("p", null);
      actions.style.marginBottom = "0";
      actions.append(cancel);
      card.append(actions);
    }
    host.append(card);
  }

  async function cancelReservation(reservation, button) {
    busy(button, true);
    try {
      var result = await request("POST",
        "/reservations/" + encodeURIComponent(reservation.reference) + "/cancel");
      if (result.status === 200) {
        await renderReservation(result.data);
        return;
      }
      busy(button, false);
      var host = reservationHost();
      var warning = notice("error", "reservation-error",
        result.message || "That booking can no longer be cancelled.");
      host.insertBefore(warning, host.firstChild);
    } catch (err) {
      busy(button, false);
      var host2 = reservationHost();
      host2.insertBefore(notice("error", "reservation-error",
        "We could not reach the service. Please try again."), host2.firstChild);
    }
  }

  // ---- start ------------------------------------------------------------

  renderSession();
  if (route === "signup") wireSignup();
  if (route === "login") wireLogin();
  if (route === "search") wireSearch();
  if (route === "lookup") wireLookup();
})();
