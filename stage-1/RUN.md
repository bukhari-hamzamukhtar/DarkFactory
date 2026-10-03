# Tablekeeper — stage 1

An HTTP reservations service. Everything it needs is in the image: the store is
in memory and the IANA timezone database is compiled into the binary, so the
container needs no outbound network, no volume and no companion service.

## Build and run

```sh
docker build -t tablekeeper stage-1
docker run --rm -e PORT=8080 -p 8080:8080 tablekeeper
```

Then:

```sh
curl -s localhost:8080/health
# {"status":"ok"}
```

`PORT` selects the listening port and defaults to `8080`; the service always
listens on `0.0.0.0`. The container answers `GET /health` with
`200 {"status": "ok"}` within a second of starting.

State is in memory and ephemeral: it does not survive a container restart, which
is what the requirements call for. Seed it with `POST /_test/reset`, move it with
`GET /_test/export` and `POST /_test/import`.

```sh
curl -s -X POST localhost:8080/_test/reset -H 'Content-Type: application/json' \
     --data @fixture.json -i | head -1
# HTTP/1.1 204 No Content
```

## Endpoints

| Method | Path | Auth |
|---|---|---|
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

`tests/` holds a service-level suite that runs against a built image and is not
part of it. See `tests/README.md`.
