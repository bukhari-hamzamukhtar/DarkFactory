# Tablekeeper — stage 3

A reservations service with a browser front end. Diners search availability, book a
table or a combined pair, see why a table is unavailable, read a booking's own history
and arrange recurring bookings. Restaurants publish dated booking policies.

Everything it needs is in the image. The store is in memory, the IANA timezone database
is compiled into the binary, and the stylesheet and script are embedded in it too, so
the container needs no outbound network, no volume and no companion service. There are
no webfonts and no CDN references.

## Build and run

```sh
docker build -t tablekeeper3 stage-3
docker run --rm --cpus 2 --memory 2g -e PORT=8080 -p 8080:8080 tablekeeper3
```

Then open <http://localhost:8080/>, or:

```sh
curl -s localhost:8080/health
# {"status":"ok"}
```

`PORT` selects the listening port and defaults to `8080`; the service always listens on
`0.0.0.0` and answers `GET /health` with `200 {"status": "ok"}` within a second of
starting.

State is in memory and ephemeral. Seed it with `POST /_test/reset`, move it with
`GET /_test/export` and `POST /_test/import`. An export produced by this team's stage-1
or stage-2 service is accepted unchanged: its accounts, tokens, references and
idempotency receipts keep working, each of its bookings arrives at revision 1 under
policy 0 with a history entry stamped at its own `created_at`, and a recurring
agreement can be adopted on it.

## Screens

| Route | Screen |
|---|---|
| `/` | Search, availability grid, booking form and confirmation |
| `/signup` | Create an account |
| `/login` | Sign in |
| `/lookup` | Find a booking by reference, and cancel it |

No new screens are required by stage 3; the grid continues to follow the stage-2 rules.
The session lives in the browser, a late search response can never replace a newer one,
an unchanged booking form retries rather than re-books, and a lost response is reported
as an unknown outcome that a retry settles.

## API

Stages 1 and 2 in full, plus:

| Method | Path | Notes |
|---|---|---|
| GET | `/availability?...&explain=true` | each slot gains `explain`: every table once, in fixture order, with both rules (`capacity`, `no_overlap`) and the `policy_version` that decided it. Without the parameter the response keeps stage 1's shape |
| POST | `/restaurants/{id}/policies` | manager only, idempotent; a complete policy; 201 with `policy_version` |
| GET | `/restaurants/{id}/policies` | public; publication order; policy 0 is not a publication and is not listed |
| GET | `/reservations/{reference}/history` | the booking's own record, oldest first; owner only, and 404 rather than 401 without a token |
| GET | `/reservations/{reference}/decision` | the current `revision` and `accepted_terms`, including after cancellation |
| POST | `/series` | adopts a booking as occurrence zero of a recurring agreement; idempotent |
| GET | `/series/{series_id}` | the agreement with each occurrence's current state; owner only |

Every reservation response now carries `revision` and `accepted_terms`: a snapshot of
the whole policy that governed it. `PATCH` optionally takes `expected_revision`, and a
mismatch is `409 stale_revision`. `POST /reservation-moves` applies individual
amendment semantics to each move, including a per-move `expected_revision`.

A restaurant fixture may declare `manager_user_ids` (default `[]`) and `combinable`
pairs. `GET /restaurants/{id}` still answers the original fixture configuration — a
published policy changes booking decisions, not that document — and carries the
restaurant's `revision`, which moves once per series adoption and once per move batch.

`NOTES.md` records the readings behind the behaviour where the requirements leave room
for judgement.

## Tests

`tests/` holds a service-level suite and a browser suite. Both run against a built
image and neither is part of it. See `tests/README.md`.
