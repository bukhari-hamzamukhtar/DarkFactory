# Tablekeeper — stage 2

A reservations service with a browser front end: diners search availability, book a
table or a combined pair of tables, and look up or cancel a booking by reference.

Everything it needs is in the image. The store is in memory, the IANA timezone
database is compiled into the binary, and the stylesheet and script are embedded in
it too, so the container needs no outbound network, no volume and no companion
service. There are no webfonts and no CDN references.

## Build and run

```sh
docker build -t tablekeeper2 stage-2
docker run --rm --cpus 2 --memory 2g -e PORT=8080 -p 8080:8080 tablekeeper2
```

Then open <http://localhost:8080/>, or:

```sh
curl -s localhost:8080/health
# {"status":"ok"}
```

`PORT` selects the listening port and defaults to `8080`; the service always listens
on `0.0.0.0` and answers `GET /health` with `200 {"status": "ok"}` within a second of
starting.

State is in memory and ephemeral. Seed it with `POST /_test/reset`, move it with
`GET /_test/export` and `POST /_test/import`. An export produced by this team's
stage-1 service is accepted unchanged: its accounts, tokens, references and
idempotency receipts all keep working, and a booking whose response was lost before
the export can still be retried afterwards with the same key and body.

## Screens

| Route | Screen |
|---|---|
| `/` | Search, availability grid, booking form and confirmation |
| `/signup` | Create an account |
| `/login` | Sign in |
| `/lookup` | Find a booking by reference, and cancel it |

The session lives in the browser (`localStorage`), so signing in on one screen keeps
you signed in on the others, and an export/import upgrade between requests does not
sign you out. The server stays authoritative: the page never shows a confirmation it
was not given by a response.

Three behaviours are deliberate and worth knowing about:

- **A late search cannot win.** Every search carries a sequence number, and a
  response that is no longer the newest is discarded, so the grid, the table labels
  and the booking form always describe the most recent search.
- **An unchanged form retries, it does not re-book.** The form mints one idempotency
  key for the request it is about to make and keeps it while nothing changes, so
  pressing the button twice returns the same reference. Editing a field makes the
  next press a new booking.
- **A lost response is reported as unknown, not as a failure.** If the connection
  drops after submitting, the form says the outcome is not known and keeps the key
  and body; pressing the button again settles it — as the original confirmation if
  the booking did commit, or as an error if it did not.

## API

Stage 1's API in full, plus:

| Change | Detail |
|---|---|
| `GET /availability` | slots gain `available_options`: every single table and declared pair that fits the party and is free, singles first in fixture order |
| `POST /reservations` | takes `table_ids`, or `table_id` for a single table; sending both is 422. Responses always carry `table_ids`, and `table_id` only when the seating is one table |
| `PATCH /reservations/{reference}` | accepts `table_ids` under the same rules |
| `POST /reservation-moves` | each move accepts `table_ids`; no table may end up in two of the resulting bookings |

A restaurant fixture may declare `combinable`: unordered pairs of its own table ids
that may be booked as one seating, with the pair's capacity being the sum. Pairs
only, never three tables, and combining is not transitive.

| Method | Path | Auth |
|---|---|---|
| GET | `/`, `/signup`, `/login`, `/lookup` | none (HTML) |
| GET | `/health` | none |
| POST | `/_test/reset` | none |
| GET | `/_test/export` | none |
| POST | `/_test/import` | none |
| POST | `/auth/signup` | none |
| POST | `/auth/login` | none |
| GET | `/restaurants` | none |
| GET | `/restaurants/{id}` | none |
| GET | `/availability` | none |
| POST | `/reservations` | bearer + `Idempotency-Key` |
| GET | `/reservations` | bearer |
| GET | `/reservations/{reference}` | bearer |
| PATCH | `/reservations/{reference}` | bearer |
| POST | `/reservations/{reference}/cancel` | bearer |
| POST | `/reservation-moves` | bearer + `Idempotency-Key` |

## Tests

`tests/` holds a service-level suite and a browser suite. Both run against a built
image and neither is part of it. See `tests/README.md`.
