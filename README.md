# Systematic review screening with Jev

A two-stage screener for systematic reviews on **any topic**. It judges title/abstract
records, and then full texts, against inclusion and exclusion criteria that you write in a
protocol. The judging is done by TypeSafe's **Jev** model.

It ships with one worked example, *the uses and benefits of playing board games across the
lifespan*. You can run it as it is, or copy it as the starting point for your own review.

It is also a working demonstration of **how to build on Jev**, and of how that differs from
building on a chat model.

---

## Quick start

```bash
uv sync
uv run streamlit run app.py
```

Paste a TypeSafe API key into the sidebar (see [Credentials](#credentials)). Then either
pick the board-games example or create your own protocol on the **Protocol** tab, and work
through the tabs left to right.

For the command line:

```bash
export TYPESAFE_API_KEY=your-key-here
uv run screener doctor   # checks the key and both literature APIs, and says what is wrong
uv run screener demo     # screens ten bundled example records against the real model
```

---

## Credentials

This project needs **one API key**. The two literature sources are open APIs and need no
key, account or institutional subscription:

- **OpenAlex** provides search and abstracts.
- **Europe PMC** provides full text.

| Setting | Service | Required? | Cost |
| --- | --- | --- | --- |
| TypeSafe API key | TypeSafe (Jev) | **Yes** — every screening decision | Paid, but tiny |
| Jev model | TypeSafe | No — defaults to `jev-latest` | — |
| OpenAlex contact email | OpenAlex | No, but please | Free |

**In the web app**, type all three into the sidebar. They stay in your browser session and
are never written to disk. **For the CLI**, export them as environment variables:
`TYPESAFE_API_KEY`, `TYPESAFE_DEFAULT_MODEL` and `OPENALEX_MAILTO`. The app also falls back
to these variables when the sidebar is empty. Never commit a key.

### The TypeSafe key

1. Sign in at **<https://console.typesafe.ai/>**, creating an account if you have none.
2. Open **<https://console.typesafe.ai/keys>** and create a key, e.g. `sr-screener`.
3. **Copy it immediately.** Keys are shown once. If you lose one, revoke it and make another.

Check it with `uv run screener doctor`. To see which model versions your account can reach:

```bash
curl -s https://api.typesafe.ai/v1/models -H "Authorization: Bearer $TYPESAFE_API_KEY"
```

A `401` means the key is wrong or has been revoked.

**Cost.** Jev 1.13 charges **$0.042 per million input tokens**, and output tokens are
free. Measured on the example protocol:

| | tokens per unit | cost |
| --- | --- | --- |
| Stage 1, per abstract | ~2,700 | **~$0.11 per 1,000 records** |
| Stage 2, per full paper | ~18,000 | **~$0.75 per 1,000 papers** |

The Screen tab and `screener screen --dry-run` estimate the cost before anything is spent.
Every run reports what it actually used.

**Pinning a model.** The `jev-latest` alias moves when a new model ships, and the answers
behind it can change without any change on your side. Pin a version, e.g. `jev-1.13.0`,
once you have tuned thresholds against it.

### The OpenAlex contact email

This is **not authentication**, and it unlocks no extra data. It lets OpenAlex reach you if
a script misbehaves, and it puts your requests in their faster "polite pool". Everything
works without it.

OpenAlex meters usage in credits. A 200-record page costs about ten credits, against a
daily free allowance of roughly 20,000 records. Searches report the credits remaining.

### Europe PMC

Nothing to configure. See [Known limits](#known-limits) for what it can and cannot supply.

---

## Using the app

Seven tabs, in pipeline order:

| Tab | What it does |
| --- | --- |
| **0 · Protocol** | Choose, create, import, download and edit review protocols |
| **1 · Search** | Build the OpenAlex query, count the matches, fetch the records |
| **2 · Screen** | Stage 1: one Jev call per title/abstract, with a cost estimate first |
| **3 · Review** | Browse the decisions, highest-ranked first, with the probabilities behind each |
| **4 · Full text** | Stage 2: check Europe PMC coverage, then screen the retrievable papers |
| **5 · Label** | Hand-screen a stratified calibration sample (the ground truth) |
| **6 · Evaluate** | Recall, precision and work saved against your labels; threshold sweep; PRISMA flow |

The app is a view over `screener.pipeline`, the same functions the CLI calls. No screening
logic lives in the UI.

### Protocols

A protocol is the whole review: the search, the criteria, the classifications, the
thresholds and the stage-2 routing. On the Protocol tab you can:

- **start a new review** from the blank starter, or as a copy of any existing protocol;
- **import** a protocol YAML file, and **download** the current one;
- **edit** the protocol in a form, or as raw YAML for anything the form does not cover.
  Changes are applied only when the whole protocol validates.

**Bundled protocols are read-only.** Currently that is just the board-games example. They
stay as blueprints, so duplicate one to change it. Every protocol keeps its own records,
results and labels, so two reviews never mix.

### Where your data lives

Everything lives **in your browser session's memory**: your protocols, fetched records,
screening results and labels, and the bundled example too.

- **It is private.** Each session is separate, so visitors never see each other's work.
- **Disk changes don't touch it.** Once something is fetched or computed, the session never
  reads it from disk again, so deleting files on the server leaves your work intact.
- **It lasts as long as the session.** Reloading the page, or restarting the server, starts
  afresh. To keep something, use the download buttons: protocol YAML, screening and
  full-text results as CSV, and labels as CSV. A downloaded protocol can be imported again.
- **Only public data touches the disk.** The app writes nothing to disk except the shared
  caches of public OpenAlex and Europe PMC data, which are rebuilt if they disappear. Jev
  answers are not cached, because they were bought with your key.

A session holding a large review uses a fair amount of server memory, roughly 190 MB for
5,000 screened records. Fetch in batches of a few hundred to a couple of thousand records.

To use the UI on the same files as the CLI, run it in single-user mode. It loads from, and
writes through to, `data/`:

```bash
SCREENER_SINGLE_USER=1 uv run streamlit run app.py
```

---

## Writing a protocol

Protocols are YAML files. [protocols/boardgames.yaml](protocols/boardgames.yaml) is a
complete, tuned example, and
[protocols/templates/starter.yaml](protocols/templates/starter.yaml) is the skeleton that
new reviews start from.

```yaml
name: My review
description: For humans; not sent to Jev.
query: '"exact phrase" AND (outcome OR effect)'   # OpenAlex full-text search
filters:            # applied by OpenAlex and re-checked in code, never asked of Jev
  year_from: 2000
  languages: [en]
  types: [article, review]
  require_abstract: false
thresholds:
  include_min: 0.6      # an inclusion criterion must reach this to count as met
  exclude_min: 0.85     # an exclusion criterion must reach this to drop the record
  informative_min: 0.4  # below this, the abstract says too little: send to a human
criteria:   # yes/no questions (Jev Nouls)
- id: inc_topic
  kind: inclusion       # inclusion | exclusion | guard
  label: Core topic studied          # reused verbatim as the PRISMA exclusion reason
  instructions: The study ...        # the question Jev answers
  outcomes:                          # optional, and the best fix for a misfiring criterion
    "true": What a yes looks like.
    "false":
      what: What a no looks like.
      examples: ["A near miss that must be a no."]
  sections: [methods, intervention]  # which parts of a full text to read at stage 2
choices:    # closed-set classifications, for reviewers and breakdowns (Jev Choices)
- id: study_design
  instructions: What design does this study use?
  options: {rct: Randomised controlled trial, qualitative: null}
scores:     # ordered ratings, lowest level first (Jev Scores)
- id: topical_fit
  instructions: How central is the topic to this study?
  levels: [Incidental, "One component", "The whole study"]
ranking_score: topical_fit   # optional: which Score orders the review queue
```

**Decision rules.** A record is:

- **excluded** when any exclusion reaches `exclude_min`. This is the only automatic drop.
- **included** when every inclusion reaches `include_min`, no exclusion fires, and every
  guard reaches `informative_min`.
- **maybe** in every other case, which sends it to a human. That includes an inclusion
  that is not met and a question that came back without an answer.

**Ranking.** The review queue is ordered by the `ranking_score` Score. If none is named,
the protocol's `topical_fit` Score is used, then its first Score, and failing those the
mean inclusion probability.

**Stage-2 sections.** Allowed values are `title`, `abstract`, `introduction`, `methods`,
`participants`, `intervention`, `measures`, `analysis`, `results`, `discussion`,
`limitations` and `other`. An empty list means title, abstract and methods.

**Writing criteria that work.** Jev answers the question you *wrote*, not the one you meant:

- **State the exact condition.** When you catch yourself explaining what you really meant
  after a wrong answer, that explanation is the missing half of the instruction. Put it in
  `outcomes`.
- **One judgement per question.** A criterion that quietly asks two things returns a
  probability that means neither.
- **Phrase it so that high means yes.** A Noul whose `true` maps to "no" performs worse.
- **Pick the type your code can act on.** A Choice maps onto branches, a Score onto a
  threshold, and a Noul onto an `if`.
- **Keep counts and dates out of the criteria.** Years, languages and document types
  belong in `filters`, where they are applied exactly.

The example protocol shows why `outcomes` matter. A board game can appear in a paper as an
**activity** people take part in, or as a **measurement instrument**, such as a Prisoner's
Dilemma used to elicit cooperation. Only the first belongs in that review. Both look
identical to a keyword search, and "is the game used as an instrument?" on its own is too
abstract to answer reliably. Describing a yes and a no, with examples of each, is what
makes it work:

```yaml
- id: exc_game_as_instrument
  kind: exclusion
  instructions: >-
    The game is used only as a task or instrument for measuring something about the
    participants, rather than being studied as an activity they take part in.
  outcomes:
    "true":
      what: The game exists in the study to elicit a measurement.
      examples:
        - "A Prisoner's Dilemma or Public Goods game used to measure cooperation."
    "false":
      what: >-
        Participants played the game as a pastime, class activity, therapy or intervention,
        and the study asks what taking part did for them.
      examples:
        - "A weekly board-game club run to reduce loneliness in older adults."
```

---

## How it works

### Jev in sixty seconds

Jev does not generate text. You hand it two things:

- a **state**, which is the thing to judge;
- a map of **typed questions**.

It returns a **calibrated probability distribution for each question**, and your code
decides what to do with them.

```python
from typesafe_sdk import Choice, Noul, Score, TypeSafeClient

with TypeSafeClient() as client:
    response = client.system_one(
        state={"title": "...", "abstract": "..."},
        questions={
            "is_rct":      Noul(instructions="The study is a randomised controlled trial."),
            "age_group":   Choice(instructions="Which age group took part?",
                                  criteria={"children": None, "adults": None, "older_adults": None}),
            "topical_fit": Score(instructions="How central is the topic to this study?",
                                 criteria=["incidental", "one component", "the whole study"]),
        },
    )

response.nouls["is_rct"].noul         # 0.93   probability that it is true
response.choices["age_group"].choice  # "older_adults"
response.scores["topical_fit"].score  # 1.7    a position, may fall between levels
```

| Type | Asks | Returns |
| --- | --- | --- |
| `Noul` | Is this true? | `noul` — P(yes), 0–1. **No confidence field.** |
| `Choice` | Which of these options? | `choice`, `probabilities`, `confidence` |
| `Score` | Which level? | `score` (fractional), `legend`, `probabilities`, `confidence` |

There is no prose to parse and no verdict to trust. You get numbers, and the policy (where
the thresholds sit, what goes to a human) is ordinary Python you can read and test. It
lives in one file, [decide.py](src/screener/decide.py), which never calls an API.

### Why the thresholds are lopsided

Screening is **recall-critical**:

- letting an ineligible paper through costs a reviewer a minute;
- dropping an eligible paper corrupts the review.

So by default an exclusion must reach **0.85** to drop a record without a human, while an
inclusion needs only **0.60** to count as met. Everything in between goes to a person.

A Noul has no `confidence` field. Its uncertainty is its distance from 0.5, which is
`1 − 2·|p − 0.5|`. Do not expect arithmetic identities between questions:

- `P(x)` and `1 − P(not x)` need not agree.
- A threshold tuned on a Noul does not carry over to a Choice.

Tune on your own data.

### Stage 1: one call with every question

Every question in a request sees the same state and is answered in parallel against a
single ingest of it. Adding a question barely moves latency, and it costs only its own
tokens. So stage 1 asks **everything at once**: all criteria, classifications and scores in
one call per record, including questions that only matter for some records. This is the
*speculative fan-out* pattern.

### What never goes to the model

Years, counts, dates, languages and document types never go to Jev. It is documented as
unreliable at counting and at comparing dates, while a search index does these exactly and
for free. They become part of the OpenAlex filter:

```python
"from_publication_date:2000-01-01"   # inclusive bounds, no off-by-one to get wrong
"language:en|it"
"type:article|review"
```

They are then **re-checked in code** after fetching, because OpenAlex returns
`publication_year`, `language` and `type` on every work. `build_filter` rejects a comma in
the topical query, because OpenAlex would read it as a filter separator and silently
truncate the search.

### Stage 2: routing sections, not whole papers

Jev's accuracy falls as the state fills with text unrelated to the decision, so a full
paper is never sent whole. Instead:

1. The full text is parsed into sections.
2. The headings are mapped onto the canonical section set.
3. Each question reads only the sections its protocol entry routes it to.
4. Questions with the same routing share a call, so a paper takes a handful of focused
   requests rather than one huge one.

The audit trail records which sections each judgement read.

The routing follows four rules, all in [screen_fulltext.py](src/screener/screen_fulltext.py):

- **Every state is anchored with the title and abstract.** A stage-2 question therefore
  never sees less than stage 1 did.
- **When none of a question's sections exist, it falls back to whatever body text there
  is,** not to the abstract. Otherwise a paper with unusual headings would silently degrade
  into re-screening its abstract.
- **Headings the regex matcher cannot place** cost one cheap Choice over the canonical
  labels. Well-behaved headings never spend a call.
- **A pre-flight token check** splits any bundle that would exceed Jev's 32k-token state
  budget. It splits on section boundaries first, and truncates only a single section that
  is too large on its own.

PRISMA 2020 requires full-text exclusions to be reported with reasons. The **exclusion
reason** is a Choice over the protocol's exclusion criteria. It is asked on every paper and
read only when the decision is `exclude`.

### Measuring whether it works

A live search has no ground truth, so validation works the way it does for real AI
screeners:

1. **Label** draws a **stratified** sample across the three decision bands. A random sample
   of a corpus that is 2% eligible would be nearly all obvious excludes, and would say
   nothing about the error that matters.
2. You screen that sample by hand.
3. **Evaluate** reads it back and reports:

```text
  recall               96.2%   <- eligible studies kept
  auto-excluded        71.4%   <- reading avoided
  workload saved (WSS) 67.6%
    eligible, DROPPED     1    <- the error that matters
```

The **threshold sweep** then finds the setting that saves the most reading while holding
your recall target. It **costs nothing**: every probability is stored, so re-deciding the
corpus is arithmetic over cached numbers. If no setting holds the target, it says so and
declines to recommend one. At that point the criteria need sharpening; lowering the target
is not the fix.

---

## Command line

```bash
uv run screener doctor                         # check the key and both APIs
uv run screener demo                           # ten bundled records, no search needed
uv run screener query                          # print the assembled OpenAlex filter
uv run screener search --count                 # how big is this search?
uv run screener search --limit 200             # fetch records
uv run screener screen --dry-run               # cost estimate, no calls
uv run screener screen                         # stage 1
uv run screener fulltext --check-entitlement   # how many full texts are obtainable?
uv run screener sections 10.1371/journal.pone.0118173   # how did one paper parse?
uv run screener fulltext                       # stage 2
uv run screener label --stage abstracts -n 50  # draw a calibration sample, then fill in gold_label
uv run screener evaluate --stage abstracts
uv run screener tune --target-recall 0.95      # add --apply to write the thresholds back
uv run screener prisma
```

Every command uses the example protocol unless you pass `--protocol path/to/protocol.yaml`.
Results go to `data/runs/<protocol file name>/`.

### The example records

`screener demo` screens [examples/sample_records.jsonl](examples/sample_records.jsonl)
against the board-games protocol. It holds ten illustrative records in the shape the
OpenAlex fetcher produces. They are **written for this project and are not real papers**:
the DOIs sit under the placeholder prefix `10.0000/` and none resolve. Each one puts
pressure on a specific criterion:

| | Record | What it tests |
| --- | --- | --- |
| 01 | Board-game RCT for loneliness, older adults | The clean include |
| 02 | Repeated Prisoner's Dilemma in a lab | **Game as measurement instrument**, the hardest criterion |
| 03 | Tablet adaptation of a board game | Digital-only exclusion, despite "board game" in the title |
| 04 | Chess in primary schools, maths attainment | Include at the other end of the lifespan |
| 05 | Card games and betting in a casino | Gambling exclusion |
| 06 | Monte Carlo tree search agent | No human play |
| 07 | Tabletop RPG group for adolescents, qualitative | Include from a non-quantitative design |
| 08 | A board game *design* framework | Not empirical, no participants |
| 09 | Survey of what care homes offer | **Borderline**: real games and real people, but no outcome of playing measured |
| 10 | Mahjong and cognitive decline, cohort | Include from an observational design |

`screener demo --write-labels` writes a label template, pre-filled with the predictions.
Correct it by hand before evaluating against it, because grading a screener against its
own output measures nothing.

---

## Known limits

- **Europe PMC holds full text for its biomedical open-access subset only.** For topics
  outside the life sciences, expect most stage-1 includes to be unobtainable. On the
  example corpus, about 30% are retrievable. Check coverage first, since it costs nothing.
  Everything it cannot supply is reported as PRISMA's "reports not retrieved". For broader
  coverage, implement `FullTextSource` in [fulltext/base.py](src/screener/fulltext/base.py),
  for example Unpaywall, or a folder of PDFs your library obtained.
- **OpenAlex is one database.** A publishable review would run the same strategy across
  other databases too, and de-duplicate across them.
- **About a quarter of OpenAlex works carry no abstract.** They are kept, because a missing
  abstract is not evidence of ineligibility. They fail the guard and land in `maybe` for a
  human.
- **Jev's primary training language is English.** Spot-check records in other languages.
- **Metrics are only as good as the calibration sample.** Fifty records give a wide
  confidence interval on recall.
- **This automates screening only.** Data extraction and risk-of-bias assessment are out of
  scope. No decision from this tool should reach a published review without a human
  adjudicating the `maybe` pile.

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `TYPESAFE_API_KEY is not set` | No key in the sidebar or environment | Paste it in the sidebar, or `export TYPESAFE_API_KEY=...` |
| TypeSafe `401` | Key wrong or revoked | Create a new one at <https://console.typesafe.ai/keys> |
| TypeSafe `429` | Rate limit (250k tok/s, 1200 req/min) | The SDK retries with backoff; lower the concurrency if it persists |
| OpenAlex `429` | Daily credit allowance used up | The client backs off; cached pages cost nothing |
| Records with no abstract | ~24% of OpenAlex works lack one | Expected; they go to a human |
| Most full texts `not_found` | Europe PMC is biomedical open access only | Expected; reported as PRISMA "not retrieved" |
| Search is slow | Not in the polite pool | Set the OpenAlex contact email |

## Layout

```text
app.py                        Streamlit UI
protocols/boardgames.yaml     the example protocol (read-only in the UI)
protocols/templates/          the blank starter for new protocols
examples/                     ten synthetic records for `screener demo`
src/screener/
  protocol.py                 protocol schema and YAML loading
  library.py                  protocol library: examples, user protocols, import
  query.py                    OpenAlex filter assembly
  questions.py                protocol -> Jev questions; stage-2 bundling
  openalex.py                 OpenAlex client
  fulltext/                   Europe PMC retrieval and section parsing
  screen.py                   stage 1
  screen_fulltext.py          stage 2
  decide.py                   the decision policy: pure, no API calls
  evaluate.py / tune.py       metrics and threshold sweep
  prisma.py                   PRISMA flow counts
  pipeline.py                 the steps the CLI and UI share
  paths.py                    per-protocol run directories for the CLI
  ui/                         Streamlit tabs, protocol editor, session state
data/                         runtime files, gitignored
  cache/                      OpenAlex and Europe PMC caches (+ Jev answers for the CLI and single-user mode)
  runs/<protocol>/            the CLI's records, results and labels
  protocols/                  protocols created in the UI in single-user mode
```

## Further reading

- [TypeSafe docs](https://docs.typesafe.ai/): start at
  [Primitives](https://docs.typesafe.ai/primitives) and
  [Confidence](https://docs.typesafe.ai/confidence)
- [Jev 1.13 jaggedness](https://docs.typesafe.ai/model-jaggedness/jev-1.13): most of this
  design is a response to it
- [Speculative fan-out](https://docs.typesafe.ai/patterns/fan-out) and
  [Composite scoring](https://docs.typesafe.ai/patterns/composite-scoring)
