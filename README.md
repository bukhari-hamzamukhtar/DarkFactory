# Dark Factory — tablekeeper

Team **lychee** · track **tablekeeper** · WeAreDevelopers x BAND, Dark Factory (hackathon edition)

A restaurant reservation service built by a band of three coding agents in BAND Desktop.
No application code in this repository was written by a human. One message was sent into
the room — the stage-1 task — and everything after it is the band.

## How to read this repository

| Path | What it is |
|---|---|
| [`FACTORY.md`](FACTORY.md) | **Start here.** The factory: seats, design decisions, what it caught, what it cost |
| `mandates/` | One standing instruction per seat. These are the factory |
| `room.json` | The full BAND room export — the record of the band doing the work |
| `stage-1/` | The JSON API. Go, single container, complete and buildable on its own |
| `stage-2/` | The API **plus the browser product** — search, booking, confirmation, lookup, combined tables |
| `stage-3/` | Adds dated booking policies, availability explanations, reservation history and recurring series |

## Running it

```sh
cd stage-3
docker build -t tablekeeper .
docker run --rm --cpus 2 --memory 2g -e PORT=8080 -p 8080:8080 tablekeeper
```

Then open `http://localhost:8080/` — search and availability grid, `/signup`, `/login`,
`/lookup`. Full detail in [`stage-3/RUN.md`](stage-3/RUN.md); every stage folder runs the same way, and `stage-1/`
and serves the API only, as its spec requires. The image is `scratch` with a static
binary; the IANA time zone database is compiled in and the one dependency is vendored in
the repository, so the build fetches no modules and the runtime needs no network.

## What the band produced

Five commits, all authored by `coder`, none amended or rebased. Both stage folders build
and serve from a clean container with outbound network blocked, and the harness reports
**claimed stage: 3**.

`stage-1/` is also the revision the `verifier` **rejected**. It independently derived about 300
assertions from the specification and found three defects that no supplied check reaches
— an import that wipes its destination instead of returning 422, a closing-time
comparison that uses wall-clock minutes across a daylight-saving change, and an unknown
bearer token treated as a missing idempotency key rather than as unauthorised. The fix
round in `stage-1/` was cut short by a model session limit, so that folder ships with the
defects intact — **all of them are fixed in `stage-2/`**, which is the folder that carries
the product.

Those findings, and why a factory finds them where a single agent does not, are the
subject of [`FACTORY.md`](FACTORY.md).

## What is being submitted

The factory, the run that produced the result, and the result — in that order of
importance. `mandates/` and `FACTORY.md` are the entry; `stage-1/` is what the entry
produced. The mandates name nothing specific to reservations or to this track: point
them at a different specification and they still describe a working software factory.
