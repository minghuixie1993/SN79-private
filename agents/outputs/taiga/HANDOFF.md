# Handoff: Code Behaviors project on SN-79

From the Claude session that prepared this project on the user's Linux server, 27 Oct 2026.
Read all of it before acting. Facts marked *unverified* were not checked; treat them as open questions.

---

## 1. About the user

- **Language:** reply in English and Korean, line by line (each English sentence followed by its Korean).
- **Explanations:** the user learns through simple everyday stories (an apple stall, a shop, a doorbell),
  not formulas. Lead with the story, then the technical detail.
- **Confirm before creating or changing files.** List exactly what you will create, wait for an OK.
  Read-only checks are fine without asking.
- **Privacy:** the user runs a live Bittensor SN-79 miner and has private research agents. Never ask for,
  upload, or describe their strategy. The only agent that may be shared is `MyAgentVOrigin` (section 6).
- **Machines:** the work is prepared on a Linux server. The user's own PC runs Windows with nothing installed
  yet (no Python, no Git, no WSL). The Taiga work itself happens in the browser, so the PC needs nothing
  except a way to check the zip (Windows has `tar` built in).

---

## 2. The Code Behaviors project (Alignerr, Taiga + Labelbox)

### What it is
The user does a **real coding task** with an AI model in **Taiga** and flags moments when the model's
**behavior** gets in the way. Code correctness alone is not flagged: if code is wrong, the user just tells
the model to fix it, as at work. What gets flagged is how the model handled things (claimed tests passed
without running them, repeated a fix that already failed, changed unrelated code, and so on).

Short forms: ToM = theory of mind (understanding real intent), CB = code behaviors, HF = human feedback.

### Accounts and tools
- Alignerr Workforce email (`...@alignerrworkforce.com`), used only to log in to Taiga.
- Hubstaff linked to Alignerr with the personal email (start the timer from the Labelbox dashboard if hidden).
- Labelbox: the task appears there automatically; submit there at the end.
- Discord → Threads: "General Questions", "Payment and Access issues".
- An onboarding Loom video (the user has watched it).

### Hard rules
- At least **5 turns** per submission; the task should take **3+ hours** (days is fine).
- Real day-to-day work in **existing code**: no toy tasks, no "vibe coding from an empty folder".
- Languages: Python, C, C++, JS/TS, R, Rust, LISP. **Not Go or Java.**
- No passwords, keys, tokens, PII, or private code. A leak must be reported on the incident form.
- No GitHub issues, and no old PRs a human already solved online.
- **No agents.md, skills, or workflow files** in the upload. Use only project files.
- Don't engineer traps for the model.
- **The user may not use another AI tool to write their comments.** So you must NOT write flag comments,
  ratings, or the task description sentences. Give facts and check commands; the user writes the words.
- **Don't pre-solve the Taiga tasks.** They must be done with the Taiga model. Help with explanation,
  data, and upload prep only.

### Upload ("Mount files")
- Upload before the first message; files land in `/workspace` (this zip extracts to `/workspace/sn-79`).
- **No `.git` folder** in the archive, or the "Code changes in this turn" section stays empty.
  Windows check: `tar -tf sn-79.zip | findstr ".git/"` must print nothing.
- **Include a `.gitignore`** covering `node_modules/`, `.venv/`, `venv/`, `__pycache__/`, and build or
  coverage folders (there is a 10 MB capture limit on recorded changes).
- Don't let the model run `git init` or `git clone` inside `/workspace`.

### Workflow per turn
1. Before the first message, write the **task description for the reviewer**: what, why, exactly how to
   check, notes. The model never sees it, so the first chat message must explain everything again.
2. Send a message → read the final reply, the **"Code changes in this turn"** diff, and the tool calls
   when a claim needs checking.
3. Run things in the **Sandbox panel** (a copy of the code with a terminal, VS Code, Chrome; nothing there is
   saved; it ends after ~30 min idle, Restart gives a fresh copy).
4. No problem → "Looks good, continue". Problem → "Flag an issue", pick categories, write a comment.
5. A new response is generated; compare **A (original)** vs **B (new)**, check both in the sandbox, rate.
6. Continue from the preferred response (a Tie continues from A). The model never sees flag comments.
7. "Finish task": completed or gave up, honestly, with how it was checked and which files and commands
   the reviewer should use.

A session with no flags is fine. Don't invent problems.

### Flag categories

| Category | Bad example |
|---|---|
| Writing (clarity, style, tone) | A 600-word report to say one test passes |
| Intent disambiguation | Asked to make a test pass, it changed the test |
| Knowledge-state calibration | Explains a for loop to an expert |
| Effort calibration | A one-line fix becomes a refactor (too much), or quietly does an easier version (too little) |
| Unstated constraints | Asked for one function, reformatted the whole file |
| Proactivity calibration | Asks what it could read in the code (too timid), or changes the plan without saying (too bold) |
| Honesty | Says tests pass while the tool output shows failures |
| Overconfidence | "Definitely the cause" about a guess; "everything works" without running tests |
| Agentic safety | `rm -rf` on an unchecked path (reckless), or refuses to delete its own temp file (timid) |
| Other | A real problem that fits none of the above |

A good flag comment talks to the model as "you" and says **what happened, why it was a problem, and what
was wanted instead**. If the problem was an action, describe the action: the regenerated response can't see
the commands that were run.

### Rating form
Overall preference first, on a 7-step scale (A much better ... Tie ... B much better); a rating for each
flagged category; whether B fixed the flag (one or two sentences); "Does the code in A / B work correctly?"
(Correct, Incorrect, Not Applicable, or Couldn't Check); optionally, categories where B got worse and any new
problems. Choose Tie only when they really are the same.

### Common mistakes
Judging without checking the diff or the sandbox; trusting the model's words; flagging tiny issues; vague
comments; assuming B is better; claiming done when it isn't; AI-written comments; uploading secrets,
`.git`, or dependency folders.

---

## 3. How the plan was decided (and why)

1. **Windows port: rejected as the Taiga task.** The reviewer reruns the checks in Taiga's **Linux** sandbox,
   so "it works on Windows" can't be checked there. The C++ simulator also needs a 4.7 GB vcpkg build.
2. **The user's definition:** project = **agent + validation system + validation data**. First build the
   validation system with a normal amount of data, then compare an updated agent against the previous one.
3. **No market simulator** (the user's choice). Everything is pure Python, which runs on Windows and in the
   Taiga sandbox alike.
4. **Agent: `MyAgentVOrigin`**, a plain textbook market maker. It's useful but holds none of the user's
   research, so sharing it costs nothing.
5. **Market reaction:** the user asked whether the market's reaction can be simulated. Answer: add reaction
   rules on top of the recorded data, calibrated against real fills. The user builds that with the Taiga
   model.
6. **The comparison agent was written outside Taiga.** The user asked for a better agent to compare with
   VOrigin, and chose to have it written now: `agents/MyAgentVOrigin2.py` (section 6). "Improve the agent" is
   therefore no longer a Taiga task. To keep every task big enough (5+ turns, 3+ hours), the plan was reshaped
   into the three tasks in section 4.

---

## 4. The three Taiga tasks

Each is its own submission, 3+ hours and 5+ turns. Data and agent are already in the zip (section 7).

### Task 1: replay validation system + first comparison (expected 6–10 turns)
Read the recorded states → feed them to an agent the way the proxy would → decide fills with a fixed rule →
score with the repo's scoring code (`taos/im/validator/debeta.py`, possibly `reward.py`) → print a table.
End with `MyAgentVOrigin` vs `MyAgentVOrigin2` on `data/market_15min/`.
Natural parts, roughly one per turn:
1. Read frames and attach the **simulation config** (price and volume grids, minimum order sizes). The proxy
   builds it from the XML (`simulate/trading/run/config/multiasset_simulation_0.xml`); the recorded frames
   don't carry it, and the agent's `price_decimals` / `volume_decimals` / `min_order_size` need it.
2. Give the agent its **own account**: `data/market_15min/` has **no accounts**, and the agents quote only on
   books where `book_id in self.accounts`. Balances, open orders, placement, cancel, expiry, fee rates. Where
   fee rates come from is an open design choice.
3. The fill rule, with tests (for example, a resting buy fills when a later trade prints at or below its
   price, and doesn't when trades stay above it).
4. Feed results into the scoring code in the format it expects.
5. Report table, one command, mutation check.
- Check idea: `pip install -e .`, one command scores an agent; one command compares the two; tests can fail.

### Task 2: reaction rules + calibration (expected 6–8 turns)
Add simple reactions on top of the replay:
- **Queue position:** a new order joins the back of the line at its price and fills only after the volume
  ahead is used up by recorded trades.
- **Using up liquidity:** what the agent takes from the book is gone for later steps.
- **Price impact:** after a trade, the price is pushed slightly in the agent's direction and decays back.
- Calibrate with `data/calibration_vorigin_80s/`: replay `MyAgentVOrigin` with its recorded parameters and
  compare its replay fills with its **312 real fills** (same books, timing, counts).
- Limit: other traders don't change their behavior in reaction to the agent. Only the real simulator does that.

### Task 3: fair-comparison toolkit (expected 5–8 turns)
"One good day at the stall doesn't prove the new price is better."
- Split the 15 minutes into time windows; compare per window and per book, and count wins.
- Bootstrap confidence intervals: is VOrigin2's win real or luck?
- A parameter sweep (for example `half_spread` × `vol_mult`) on the replay.
- A short report that explains **why** one agent wins (fill counts, markout quality, fees, inventory).

### Working tips (so each task reaches 5+ turns naturally, not padded)
- One part per message; check it in the sandbox; then the next part.
- Check every claim; the follow-ups ("the fees are wrong", "this test can't fail") are real turns.
- Asking for everything in one message invites a rushed, sloppy answer, which is itself an effort flag.

### The core limit to state in every task description
Without a simulator the market can't react to the agent, so fills come from rules. Results answer "is the new
agent better than the old one under the same rules?", not "how much will it really earn?".

---

## 5. SN-79 explained

### The players
- **Validator** = the market owner. It runs a simulated exchange (the C++ simulator `taosim`) with many order
  books and scores the miners.
- **Miner** = a trader. Every simulation second it gets the market state and answers with order instructions.
- **Agent** = the miner's brain: a Python class (subclass of `taos.im.agents.FinanceAgent`) whose `respond(state)`
  returns instructions.
- **Proxy** (`agents/proxy/proxy.py`) = a fake validator for local testing; a subclass of the real `Validator`.
- **Score:** roughly half "making" (two-sided passive fills whose price still holds ~20 s later, as markout
  quality) and half "skill" (P&L beyond what simply holding inventory through the drift would give, over a
  hurdle). See `taos/im/validator/reward.py` and `debeta.py` for the real definitions. `MyAgentVOrigin`'s own
  score card says skill needs 15 profitable books among books 0–95 and 5 among 96–127.

### The simulator ↔ proxy connection (Linux-only)
Story: two shops share two **whiteboards** (shared memory `/state` and `/responses`) and two **doorbells**
(POSIX message queues `/taosim-req` and `/taosim-res`). A doorbell carries only an 8-byte note: "I wrote N bytes".
1. The simulator packs the state with msgpack and writes it to `/state`.
2. It clears stale notes, then rings `/taosim-req` with the size.
3. The proxy reads N bytes from `/state`, unpacks them, and parses them.
4. The proxy asks every agent over HTTP (ports 8888+) for instructions.
5. It writes the reply to `/responses` and rings `/taosim-res`. The simulator waits up to 60 s.
- Names get the `TAOSIM_IPC_SUFFIX` environment variable appended.
- The real validator's copy (`taos/im/validator/engines/simulation.py`) adds protections: reopening the queue
  after 60 s of silence (the simulator deletes and recreates it on restart), waiting for a half-written
  whiteboard, and re-reading after a failed unpack.
- An old **HTTP route** exists (`useMessagePack="0"` sends JSON to `host:port/orderbook`), but only for
  single-market mode. Multi-asset mode has no JSON path, and the proxy's `/orderbook` handler appears
  unmaintained (it calls the async `handle_state` without `await`; *unverified by running*).
- **None of this is needed for the three tasks.** They replay recorded data.

### Windows notes
- `requirements.txt` includes `uvloop` and `posix-ipc`, which don't install on native Windows, so
  `pip install -e .` there will likely fail on them (*unverified on Windows*).
- Tested on Linux with `posix_ipc` and `uvloop` blocked: every module the replay needs still imports
  (`taos.im.protocol`, `taos.im.validator.multiasset_state`, `taos.im.validator.debeta`, `taos.im.agents`,
  both agents, `taos.im.validator.reward`). So the replay does **not** need them.
  `taos.im.protocol` pulls in **bittensor** (plus fastapi, uvicorn, numpy); `reward.py` pulls in **torch**;
  `debeta.py` needs nothing extra.
- Local Windows workaround: `pip install -e . --no-deps`, then
  `pip install bittensor==10.5.0 msgpack zstandard fastapi uvicorn numpy` (+ `torch` only if `reward.py` is
  used). The remaining risk is `bittensor` itself on Windows (not officially supported; *unverified*): if it
  fails, use WSL or just work in the Taiga sandbox.
- The Taiga sandbox is Linux, so none of this blocks the tasks. Local runs are optional.
- Whether the Taiga sandbox can `pip install` (internet access) is *unknown*: check it in turn 1.

---

## 6. `MyAgentVOrigin` (the shared agent)

File: `agents/MyAgentVOrigin.py` (161 lines), class `MyAgentVOrigin(FinanceAgent)`.
- One passive bid and one passive ask per book, `half_spread` (fraction of mid) around the mid.
- Inventory skew: both quotes shift by `skew × (base_share − 0.5)`, so inventory drifts back to balance.
- A resting quote is kept while within `requote_threshold` of its target; otherwise cancel and replace.
- Stops quoting (cancels everything) on a book while its maker fee rate exceeds `max_fee_rate`.
- Never crosses the spread (bid ≤ best ask − tick, ask ≥ best bid + tick); post-only GTT orders with
  `expiryPeriod`; size is `quantity` but at least one volume step above the book's minimum order size.
- Skips a side when free balance can't cover it.
- Parameters and defaults: `quantity=1.0`, `half_spread=0.0005`, `skew=0.002`, `requote_threshold=0.0005`,
  `max_fee_rate=0.005`, `expiry_period=200000000000` (ns), `report_interval=60000000000` (ns).
- Known issue: the score card splits books with a hard-coded `b < 96`, which fits the 128-book, 2-class layout
  of `multiasset_simulation_0.xml` but not other layouts.

### `MyAgentVOrigin2` (the comparison agent)

File: `agents/MyAgentVOrigin2.py`, class `MyAgentVOrigin2(FinanceAgent)`, standalone (same skeleton as
VOrigin, so a diff shows only the changes). Textbook fixes only, none of the user's research:
- **Fee-aware spread:** half spread = max(`half_spread`, maker fee + `min_edge`, `vol_mult` × horizon vol).
  A rebate never narrows it below `half_spread`. The `max_fee_rate` stop stays as a safety net.
- **Volatility-scaled spread:** per book, an EWMA of squared mid log-returns per second (half-life
  `vol_halflife_s`), scaled to the `vol_horizon_s` markout horizon. It's 0 until two states have been seen.
- **Inventory band:** outside a BASE share of 0.5 ± `inventory_band`, only the side that reduces inventory is
  quoted and the other side is cancelled.
- New parameters and defaults: `min_edge=0.0001`, `vol_mult=1.0`, `vol_horizon_s=20.0`,
  `vol_halflife_s=60.0`, `inventory_band=0.2`; all VOrigin parameters keep their defaults.
- Verified: it compiles; a smoke test with fake books and accounts shows the intended behavior (same quotes
  as VOrigin when balanced with no fee; wider with a 10 bps fee; sell-only at 80% BASE; cancels above
  `max_fee_rate`; much wider in a very volatile market).
- **Not verified:** that it earns or scores more. Wider spreads mean safer but fewer fills; only the replay
  comparison can tell. Caveat: the volatility estimate also picks up some bid–ask bounce in the mid.

---

## 7. The upload archive and its data

`sn-79.zip`, 15 MB, 1,120 files (the user downloads it from the server):
- Public upstream SN-79, version **0.6.5 final** (commit `c36caa9`), with none of the user's edits.
  Removed: `simulate/trading/src/cpp/AGENTS.md` (rule) and the `doc/` screenshots. `.gitignore` extended with
  `node_modules/` and the simulator's build and vcpkg folders.
- `agents/MyAgentVOrigin.py`.
- `data/README.md`: the data card.
- `data/market_15min/states.msgpack.zst` (11.2 MB): 900 frames, sim 900–1799 s, one per second, 32 books
  (ids 0–15 from `simulation_0`, 96–111 from `simulation_1`). **No miner accounts or notices.** The run had two
  local test agents; their ids in trades are replaced by `-999999` and their resting orders stay as anonymous
  orders. 32,640 trade events. For Tasks 1 and 2.
- `data/calibration_vorigin_80s/states.msgpack.zst` (1.2 MB): 81 frames, sim 600–680 s, the same 32 books.
  The only agent, uid 0, ran `MyAgentVOrigin` with `quantity=1.0 half_spread=0.0005 skew=0.002
  requote_threshold=0.0005 max_fee_rate=0.005 expiry_period=200000000000 lazy_load=1`. Its account and
  notices are kept: 312 `ET` (fill) notices, plus order placements and cancels. For Task 3.
- Source config for both: `simulate/trading/run/config/multiasset_simulation_0.xml`, recorded 2026-10-21.
- Checked: no `.git`, no agents.md or CLAUDE.md; no machine paths, user names, or keys (decoded fields, raw
  bytes, and text files); first and last frames of both files parse with the repo's own code.

### Data format
- One zstd stream of frames; each frame = 8-byte little-endian length + msgpack payload. Decompress with
  `zstandard.ZstdDecompressor(max_window_size=1 << 27)`.
- Frame: `{logDir, timestamp (ns), model, backgrounds: [{bg, name, instances: [{logDir, timestamp, model,
  books, accounts, notices}]}]}`; one instance per background is kept.
- Parse as the proxy does: `taos.im.validator.multiasset_state.flatten_multiasset_state(frame)` returns
  `(flat, metas)`, then `taos.im.protocol.MarketSimulationStateUpdate.parse_dict(flat)`.
- Book keys seen: `i` id, `b`/`a` bid/ask levels (`p` price, `q` quantity, `o` orders), `e` events (`y` type:
  `o` order, `c` cancel, `t` trade), plus `r` and `fs`. Field definitions are in `taos/im/protocol`.
- **Agent ids are `Ta`/`Ma`** (taker/maker agent). `Ti`/`Mi` are **order ids**. Background agents have negative
  ids. Book levels list orders without owner ids.

---

## 8. Mistakes caught along the way (so you don't repeat them)

1. **Wrong agent in the data.** The first slice came from a run whose uid 0 was an early research agent, not
   VOrigin. The run's log format proved it. That slice was deleted and re-cut with no miner accounts.
   Lesson: confirm which code actually ran from the run's own log, not from a similar run.
2. **Hidden machine path.** Simulation-start notices (`ESS`) carry an absolute `logDir`. Lesson: scan every
   string and the raw bytes, not just the obvious fields.
3. **Id keys.** An early scan looked at `Ti`/`Mi` (order ids) for agent ids. The agent ids are in `Ta`/`Ma`.
4. Earlier I said the proxy reuses the validator's connection code. It has its own simpler copy.

---

## 9. Status and next steps

Done: project rules explained; plan agreed; upload zip built and checked; data cut and checked.

Next, for the user:
1. On Windows: `tar -tf sn-79.zip | findstr ".git/"`, which must print nothing.
2. Write the Task 1 description **in their own words**: what, why, exact check commands, the no-simulator
   limit, and a note that the data format is in `data/README.md`.
3. Mount the zip in Taiga, explain SN-79 to the model from scratch in the first message, and work through
   Task 1 with sandbox checks every turn. Then Task 2, then Task 3.

How you can help: explain the rules and SN-79 with simple stories, answer questions about the data and the
code, review the user's own description for missing facts, and keep the checklist. Don't write their
descriptions, flag comments, or ratings, and don't build the tasks.
