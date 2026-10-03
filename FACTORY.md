# FACTORY.md

How this factory is built, why it is built this way, what it cost, and how it catches
work that should not ship.

Everything in `mandates/` is generic. Nothing there names reservations, restaurants, or
any surface of this track — the track entered the factory exactly once, as the task
pasted into the room. `python -m harness check` confirms this against the organisers'
own vocabulary list, and it passes against the *other* track's list too.

## The organising principle

**No seat accepts its own work.**

Every other decision follows from it. An agent that writes code and then declares it
correct has not been reviewed; it has been asked to mark its own homework, and it says
yes. So authority is split three ways and no seat holds two of the three powers:

| Seat | Writes code | Judges correctness | Routes work |
|---|:---:|:---:|:---:|
| `coordinator` | no | no | **yes** |
| `coder` | **yes** | no | no |
| `verifier` | no | **yes** | no |

The verifier cannot edit what it judges. If it could, it would become the author of the
work it accepts and the separation collapses. It rejects with evidence; the finding
returns to the coder through the coordinator.

## Standing it up

Three seats in BAND Desktop. Seat names are lowercase and match the mandate filenames.

| Seat | Harness | Model | Reasoning | Mandate |
|---|---|---|---|---|
| `coordinator` | Claude Code | `claude-sonnet-5-5` | default | `mandates/coordinator.md` |
| `coder` | Claude Code | `claude-opus-5-5` | high | `mandates/coder.md` |
| `verifier` | Claude Code | `claude-opus-5-5` | high | `mandates/verifier.md` |

Each mandate is the agent's Role. Open a room, add the three seats, dispatch the task to
`coordinator`. One human message per stage; nothing after it.

### Why the models differ

The coordinator does not reason about the problem. It carries a specification from one
seat to another without summarising it — a seat cannot read another seat's context, so a
pointer is not a handoff. That is instruction-following, and the strongest model buys
nothing.

The coder and verifier both get the strongest model. The verifier's job is the harder of
the two: read the specification, work out what must be true, and find the behaviour
nothing has exercised. That is the task most likely to be done badly, so it is the last
place to economise.

**Known weakness:** coder and verifier run the same model and so share blind spots. We
accepted that because the independence here is structural — the verifier forms its
expectations before reading the supplied checks, cannot edit what it judges, and
reproduces every claim itself. In the run below it rejected the coder's work on three
counts, so the structure held.

## How the factory catches bad work

**1. Claims are not evidence.** The verifier may not accept a report that work passes. It
takes the revision, builds it, and runs the checks itself.

In this run it went further than the mandate required, and the detail is the point:

> I tested my own copy, made with `git archive` of that commit, **not the working tree**.

A working tree can contain uncommitted edits that will never reach a judge. Verifying the
commit rather than the directory is the difference between checking what was written and
checking what will ship.

**2. Expectations are derived before the supplied checks are read.** The verifier writes
down what the specification demands, then compares that against what is actually
exercised. The gap is the product. Here it produced **about 300 assertions from the spec**
against 120 supplied checks that all passed.

**3. Supplied checks are a smoke signal, not a definition of done.** Code written to
satisfy a specific check — special-casing an input, shortcutting a path a check does not
reach — is explicit grounds for rejection.

**4. Nothing already accepted may regress.**

## What it caught

The coder reported stage 1 complete at `126cf6b`, with all 120 supplied checks passing.
The verifier rejected it:

- **Import accepts an invalid state and wipes the destination.** The spec requires `422`
  with the destination unchanged. An import of `{"state":{}}` returned `204` and emptied
  a seeded destination. The verifier also listed the eight invalid-import cases that were
  handled *correctly*, which is what makes the finding actionable rather than a complaint.

- **Across a daylight-saving change, the closing-time check used wall-clock minutes
  instead of absolute time.** It failed in both directions: it accepted a booking whose
  own reported end time was after closing, and it rejected a booking the specification's
  own worked example says must succeed. Nothing in the supplied checks touches this.

- **An unknown bearer token was not treated as a missing one.** The spec orders
  authentication before idempotency resolution; an unknown token with no idempotency key
  returned `400` instead of `401`.

Every one of these is in the written specification. None is in the 120 checks that
passed. That is the entire case for building a factory rather than an agent.

## Measured cost

Run 2, the submitted run. One human message; everything after it is the band.

| | coordinator | coder | verifier |
|---|---:|---:|---:|
| Tool calls | 12 | 94 | 38 |
| Room messages | 6 | 3 | 2 |

- **Wall clock:** 80 minutes, dispatch to the rejection being relayed.
- **Commits:** 3, all authored by `coder`, none amended or rebased.
- **Supplied stage-1 checks:** 120 passed.
- **Verifier's own probes:** ~300 assertions; 3 defects found.
- **Model spend:** $19.37 for the room, as reported by BAND Desktop.
- **Where it went:** the coder is roughly two thirds of the tool calls, and it is also the
  seat whose work must be redone when the verifier rejects. The verifier costs about 40%
  of what the coder costs and found three defects in work that had already passed 120
  checks, which is the cheapest part of this factory per defect found.
- **The binding constraint is quota, not money or wall clock.** Two of three runs ended on
  a session limit rather than on a decision.

## What we tried that failed

**A seat that was configured but never verified.** The first practice run produced
nothing — no files, no commits, no error in the room. The task sat at `pending`. The
coding-agent runtime was installed and selected but not signed in, and nothing surfaced
that. A factory that cannot start looks exactly like a factory that is thinking. Prove
the runtime answers before dispatching.

**A seat whose runtime wedged.** One seat accumulated stale host sessions across three
rooms; releasing them kept failing and the seat silently stopped receiving work while the
room still showed it as a member. We stopped repairing it and replaced it with a fresh
identity. Repairing platform state cost several hours; replacing it cost two minutes.

**Collaboration evidence produced only at the end.** This is the important one. An
earlier full run dispatched, worked silently for two hours, and was cut off by a model
session limit one step before the coder reported back. The code was finished and passing.
But the coder had never sent a message, so the verifier was never handed anything, and
the room held no exchange between two seats at all — an unranked entry despite working
code.

The fix is in the mandates: the coder acknowledges a handoff with its plan *before* doing
any work, and the coordinator confirms it and tells the judging seat what is coming. In
the submitted run that exchange completed within the first minutes. An interruption now
costs stages instead of the entry. A factory should be legible while it works, not only
once it is done.

**Session limits are the real budget.** Two of three runs ended on a model usage limit
rather than on a decision. A factory of strong models working autonomously for hours is
constrained by quota long before it is constrained by the clock, and nothing inside the
band can see it coming. Design for interruption: commit early, report at milestones, make
the evidence exist before it is convenient.

## State at submission

The verifier's rejection was relayed to the coder, which began the fix and was stopped by
the session limit. The submitted `stage-1/` is therefore the rejected revision: it builds
and serves from a clean container, passes all 120 supplied checks, and carries the three
defects named above.

We are reporting that rather than quietly shipping it, because a factory whose value is
independent verification cannot also hide what its verification found.

## Pointing it at something else

The test applied to every mandate: could another team hand these to a band building
something completely different, and would they still make sense?

Nothing in `mandates/` names an endpoint, a field, an error code or a fixture. The
separation of powers, the evidence rules, the handoff discipline and the autonomy
constraint are properties of how work moves through a band, not of this problem. To reuse
it: configure the three seats, paste a different specification into a room, dispatch it
to the coordinator.
