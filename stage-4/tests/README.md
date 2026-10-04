# Stage-1 checks

Service-level checks written from the stage-1 requirements. They exercise a
running container over HTTP and never import the service's source, because the
deliverable is an HTTP service rather than a library.

These are not part of the image: `.dockerignore` keeps this directory out of the
build context.

## Running them

```sh
docker build -t tablekeeper stage-1
docker run -d --name tk --cpus 2 --memory 2g -e PORT=8080 -p 127.0.0.1:18080:8080 tablekeeper
python -m pytest stage-1/tests --base-url http://127.0.0.1:18080
```

`httpx` and `pytest` are the only requirements.

One check needs a second container, to show that an exported snapshot carries no
dependence on the process, port or address it came from. It is skipped unless a
second service is supplied:

```sh
docker run -d --name tk2 -e PORT=9000 -p 127.0.0.1:19090:9000 tablekeeper
python -m pytest stage-1/tests --base-url http://127.0.0.1:18080 \
       --second-base-url http://127.0.0.1:19090
```

## What is covered

| File | Requirements |
|---|---|
| `test_concurrency.py` | §1, §2, §7 — 50 requests in flight: races for one table and for overlapping intervals, identical retries, concurrent amendments, cancel-and-rebook, concurrent batches, a mixed load, health under load |
| `test_idempotency.py` | §7 — replay versus reuse, the order the key is resolved in, scope by user and by path, keys after a 4xx, body equality |
| `test_time_dst.py` | §9 — both transitions in `Europe/Berlin` and `America/New_York`: skipped hours, repeated hours, first-occurrence resolution, absolute-time durations |
| `test_moves.py` | §11 — atomic batches, swaps and rotations, error precedence, rollback, batch receipts |
| `test_export_import.py` | §10 — round-trip fidelity of bookings, tokens, hashes, receipts and configuration; replacement rather than merge; rejected envelopes; snapshot isolation |
| `test_errors_and_auth.py` | §3.4, §5, §6 — the error envelope, status and code for every stated case, authentication, and fixture validation |
| `test_availability_and_bookings.py` | §8 — the slot grid, capacity and ordering, the booking lifecycle, cutoffs, and the read endpoints |

Each file states which rule it is checking, so a failure says what the service
got wrong rather than only which assertion tripped.
