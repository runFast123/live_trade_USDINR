# USDINR rollover watcher

A Windows app that watches the near and far USDINR futures on NSE currency
derivatives and rolls one clip when the cost of rolling is below your limit.

**The trading logic is in [BRD.md](BRD.md).** That file is the specification and
contains nothing else. This file is about running, building and releasing.

> It starts in dry run. Nothing reaches the exchange until you set `dry_run` to
> `false`, and even then only after you arm it and every safety gate passes.

## Quick look, no credentials and no network

```
python roll_app.py --selftest
```

Runs the rule against fixed numbers, including the worked example from the
original note: near bid 95.9400, far ask 96.5200, roll cost **0.5800**, which is
not below the 0.30 limit, so nothing is sent.

```
python -m unittest discover -s tests -t .
```

296 tests, covering the rule, the tenor based limit, the gates, the order
sequence, the price feed, the recorder, the probe, crash-safe state and the
updater.

- [tests/test_limits.py](tests/test_limits.py) pins the basis point maths and
  the tenor schedule, including that 30 bps is not 30 paise.
- [tests/test_realdata.py](tests/test_realdata.py) uses real captured responses:
  the scrip master row, the `MultipleTouchline` payload, `MarketStatus` and
  `NetPosition`.
- [tests/test_feed.py](tests/test_feed.py) uses a real websocket message, which
  is the only documentation the FIX tags have.
- [tests/test_execution.py](tests/test_execution.py) covers the two-leg order
  sequence: partial fills, unreadable fills, and the half-rolled halt.
- [tests/test_update_install.py](tests/test_update_install.py) runs the real
  swap script against throwaway files.

## Install and run

```
pip install -r requirements.txt
python roll_app.py
```

There is nothing to edit by hand. The app walks through three windows.

### 1. Sign in

Your own vendor ID, API key and mobile number. If Choice returns the OTP itself
the login goes straight through; if it does not, the window asks for the code
from your authenticator app. *Remember these on this machine* saves them to
`config.json`; *Force a fresh login* ignores a session saved earlier today.

### 2. Choose the two contracts

Segment 13 holds about 14,000 rows and all but ~170 are options, so the list
shows **futures only**. Search by token or name, switch between *Month end* and
*All futures*, sort any column, or press **Suggest the usual roll** to take the
next two month-end contracts. A pair whose expiries run the wrong way round, or
that is the same contract twice, or whose lot size is not what the config
expects, cannot be saved.

### 3. Watch

Both legs with the size resting at each touch, the roll cost against the limit,
and every safety gate with the blocking ones listed first. Prices flash green or
red as they move, with the size of the move beside them, and a pill in the header
says whether they are coming from the **live feed** or from **polling**.

- **ARM** allows the next qualifying quote to trade. It expires after 120
  seconds and the app never arms itself.
- **ROLL LIMIT** is editable here, in whichever unit it is set in. In basis
  point mode it edits the limit for the tenor currently on screen. A new value
  is validated the same way `config.json` is, saved, and **always disarms**, so
  a typed digit can never fire a roll on the next tick.
- **Change contracts** reopens the chooser and restarts the watch loop on the
  new pair.

Every window scrolls, so nothing is out of reach on a small screen.

**It makes a noise** when the cost first comes below the limit, and a different,
more insistent one on anything that halts. With a tight limit the qualifying
windows are rare and brief, and nobody watches a screen all day.

### What it records

While it runs it writes `data/market-<date>.csv`: a row every few seconds with
both legs' bid, ask and resting size, the roll cost in basis points, the limit,
and which gates were blocking. On shutdown it summarises the day:

```
closest approach 28.9 bps at 10:19:37
at or below the 30 bps limit: 10.0 minutes
within 2 bps of it: 30.0 minutes
a limit of 29 bps would have been available for at least a minute
```

That last line is the point. *"Your 30 bps was touched for ten minutes; 31 would
have given you fifteen"* is something a client can act on. *"Nothing happened
again"* is not.

## Several limits at once

The single ROLL LIMIT box answers "may I roll?" and nothing else. It cannot
answer "how much would I do at thirty basis points, and how much at fifty?" --
so the ladder gives each limit the same treatment that one limit had, and you
set them in the window rather than in a file:

```
ROLL COST AT EACH LIMIT                   3,000 of 10,000 rolled, 7,000 left   clip 1,000

| LIMIT              x |  | LIMIT              x |  | LIMIT              x |
| [30] bps      0.2877 |  | [45] bps      0.4316 |  | [65] bps      0.6234 |
|                      |  |                      |  |                      |
| too dear             |  | too dear             |  | in range             |
| 33.6 bps away        |  | 18.6 bps away        |  | 1.4 bps inside       |
| far ask <= 96.2027   |  | far ask <= 96.3466   |  | far ask <= 96.5384   |
| -------------------- |  | -------------------- |  | -------------------- |
| QTY          [10000] |  | QTY          [     ] |  | QTY          [     ] |
| 3,000 of 10,000      |  | watching only        |  | watching only        |

[Add limit]  [Set limits]
```

The limit and the quantity are typed. Everything to the right is the market
against that rung: what the basis points come to in rupees, the far ask that
would satisfy it, how far away we are, and how much of it is done. **Set
ladder** validates and applies; it always disarms, so a typed digit cannot fire
a roll on the next tick, and it writes the result back to `config.json`.

That ladder is a campaign of **35,000**: each rung is its own allocation and
they add up.

### Watching a limit without trading it

Leave the quantity blank and the limit becomes a **watch line**: priced and
compared alongside the rungs, and never traded.

```
LIMIT  QTY        IN RUPEES   FAR ASK AT OR BELOW  DISTANCE      ROLLED   STATUS
[ 30]  [ 10000]      0.2877               96.2027     +33.6  3,000/10,000  33.6 bps away
[ 45]  [      ]      0.4316               96.3466     +18.6      watching  18.6 bps away
[ 50]  [      ]      0.4796               96.3946     +13.6      watching  13.6 bps away
[ 65]  [      ]      0.6234               96.5384      -1.4      watching  in range
```

This is the answer to "what would fifty look like?", and it matters *where* you
ask it. The single ROLL LIMIT box is the tenor ceiling -- typing a smaller
number into it makes every looser rung illegal. A watch line cannot trade, so
it cannot loosen anything, and it is therefore not ceiling-bound: you can watch
90 bps while trading at 30.

For the same reason, lowering the ROLL LIMIT below a rung you already hold is
refused, and it names the rung in the way. Otherwise the ladder -- built once at
startup -- would go on trading at the old, wider number against the new
instruction for the rest of the session.

**The cheapest rung with quantity left is worked first.** When more than one
qualifies the price paid is the same either way, but crediting the tight rung
keeps the loose one in reserve for a worse market later. Crediting the loose one
first strands the rest of the campaign behind the tight limit.

**No rung may be looser than the tenor limit** in `limit_bps_schedule`. A ladder
decides how to spend within the limit, not whether to raise it, so a rung above
the ceiling is refused with the reason shown next to the buttons. If the tenor
is not known yet, nothing is accepted at all -- without a ceiling a rung could
quietly exceed the limit for this roll.

Also refused, in place, with the bad row left on screen to be corrected: a
quantity that is not a multiple of the lot size, the same limit twice, and
anything that is not a number.

A rung is a budget, not an order size. `lots` still decides what goes out in one
order and `max_clips_per_day` how many orders a day -- a 10,000 rung with a
1,000 clip is ten orders, which is why the panel shows the clip size next to the
total. Raise `lots` to send more at once; the depth gate will still refuse a
clip the book cannot fill.

Progress survives restarts and does not reset overnight. Editing keeps it for
any rung still at the same limit and drops it for one removed or repriced,
because that is a different commitment. **Reset ladder** forgets all of it, and
changes nothing at the exchange.

## What happens around an order

Four things sit between a qualifying quote and a completed roll, and none of
them existed at first.

**Margin, before anything is sent.** Both legs go to `get_margin` in one
request, because a calendar spread nets and asking about the sell and the buy
separately would add two outright numbers together and refuse rolls the account
can comfortably afford. A shortfall otherwise surfaces as a filled near leg and
a rejected far one, which is a naked short in the month about to expire.
Unknown counts as unaffordable: guessing optimistically here is exactly how
that happens. Needs kkunal 1.3.0.

**Reconciliation, afterwards.** A roll of N moves the near leg down N and the
far leg up N. Both legs are read before the first order and again after the
second, and a position book that disagrees halts. A book that cannot be *read*
is not a book that *disagrees* -- the first warns, the second halts.

**The trade book, as a second witness.** `get_trade_book` is the exchange's
record of what executed, against the order book's summary of what the broker
believes. A disagreement is reported loudly but does not halt on its own: its
row shape is the one thing the live probe could not confirm, and halting every
roll on a guess about field names would be worse.

**A journal**, at `data/journal-<date>.jsonl`, one JSON object per line: the
decision with the limit that was in force, each order with the broker's
reference and its raw answer, the reconciliation verdict, and every halt.
Decimals are written as strings, so the number in the record is the number the
decision was made on. A crash costs at most the line being written.

**Cancel all**, and a close that uses it. A Day order outlives this process at
the exchange and the app-side synthetic IOC that would have cancelled it does
not. Closing the window cancels first and says so if anything was left in
doubt.

## Dry run and live

The app starts in whatever mode `config.json` says, and ships in **dry run**,
where it decides everything and sends nothing.

Switching to live is a button in the window, but it is not a toggle. It opens a
dialog that states what one clip commits in rupees, lists every precondition
with a verdict beside it, and stays disabled until they all read OK and the
phrase `GO LIVE` has been typed exactly. The preconditions are rechecked when
the button is pressed, not when the dialog was drawn, so a halt or a dropped
feed arriving in between still stops it.

    quantity unit confirmed     see below
    configuration valid         config.json passes every check
    signed in                   a live session with Choice
    not halted                  no outstanding halt
    scrip master is today's     current circuit limits and ticks
    live price feed             the websocket, not the REST fallback

Going back to dry run needs none of that. One press, no dialog, takes effect
immediately. A safety control that is awkward to release is one that gets
switched off and left off.

Live mode lasts for the session. Arming does not survive the switch in either
direction: whoever armed the app in dry run was authorising a simulation.

### `quantity_unit_confirmed`

This one is not about the program. Whether the exchange reads an order quantity
as **contracts** or as **units of the underlying** has never been established,
and the app sends `lots x MarketLot`. If the exchange counts contracts, that is
a thousandfold over-order.

The live probe could not settle it, because the order was refused on account
entitlement before the exchange validated anything. Until Choice confirm it, or
an accepted order proves it, live mode refuses to engage. Set
`quantity_unit_confirmed` to `true` in `config.json` once you know.

[LIVE-FINDINGS.md](LIVE-FINDINGS.md) has the detail.

## Updating

The app asks GitHub once at startup whether there is a newer release. If there
is, an **Update to vX.Y.Z** button appears in the header. One click downloads
it, checks it against the published SHA256, and restarts into the new version.

It will not install on its own, and it refuses while the app is armed or an
order is working. A release with no checksum, or one whose checksum does not
match, is discarded rather than installed.

Turn the check off with `"update_check": false` in `config.json`.

## Building

```
powershell -ExecutionPolicy Bypass -File build_exe.ps1
```

Produces `dist\roll_app.exe`. The tests run first and the build stops if they
fail. `config.json` is read from the folder the exe sits in, so the limit, the
contracts and the dry-run flag can be changed without rebuilding.

## Releasing

Tag a commit and [the workflow](.github/workflows/release.yml) builds both
executables, publishes them with their SHA256s, and the update button then
offers the new version to everyone. The in-app update replaces `roll_app.exe`
only; download `roll_cli.exe` from the release when you need the newer one.

```
git tag v1.0.1
git push origin v1.0.1
```

The workflow refuses to release if the tag does not match
`rollover.__version__`, so a build can never claim a version it is not.

## Files

| Path | What it is |
|---|---|
| [BRD.md](BRD.md) | the trading logic, and only that |
| [LIVE-FINDINGS.md](LIVE-FINDINGS.md) | what the live order probe proved, and what it did not |
| `roll_app.py` | entry point and command line |
| `rollover/money.py` | exact decimal prices, tick rounding, exchange units |
| `rollover/rule.py` | the roll rule; no network, no clock, no state |
| `rollover/gates.py` | the safety gates |
| `rollover/quotes.py` | touchline reading, depth, tick snapping, price scale |
| `rollover/broker.py` | everything that talks to Choice |
| `rollover/engine.py` | the watch loop and the two-leg execution |
| `rollover/theme.py` | colours, fonts, buttons, cards, scrolling |
| `rollover/login.py` | the sign-in window |
| `rollover/picker.py` | the contract chooser |
| `rollover/ui.py` | the watch window |
| `rollover/updater.py` | the update check and install |
| `tools/make_icon.py` | regenerates `assets/icon.ico` |
| `rollover/recorder.py` | the market sampler that answers "what limit would have worked" |
| `rollover/probe.py` | the one-order live probe |
| `rollover/notify.py` | sound, because a screen alert is useless to someone not looking |
| `rollover/ladder.py` | several limits at once, each with its own quantity |
| `rollover/margin.py` | can the account carry this roll, both legs in one question |
| `rollover/reconcile.py` | did the position actually move the way the fills said |
| `rollover/journal.py` | one JSON line per decision, order, fill and halt |
| `rollover/livemode.py` | the preconditions for sending real orders |
| `rollover/state.py` | the clip count, any halt and ladder progress, kept across restarts |
| `logs/` | a dated audit log of every decision and order |
| `data/` | market samples and probe captures |

## Command line

```
roll_app.exe                 the windows
roll_cli.exe --find USDINR   list contracts and tokens
roll_cli.exe --check         validate config.json and exit
roll_cli.exe --selftest      run the rule on worked examples, no network
roll_cli.exe --preflight     check the whole live order path, read-only
roll_cli.exe --probe         place ONE resting order, read it back, cancel it
```

`--config` also decides where everything else lives. The session, the logs, the
state file and the day's recordings all sit beside the config file you point
at, not beside the executable.

**Two executables, one program.** `roll_app.exe` is built windowed so that
double-clicking it opens the app and not a black console behind it. The price
of that is that Windows does not attach a windowed program to the console that
launched it, so anything it prints goes nowhere and it cannot read a typed
answer -- run `roll_app.exe --check` from PowerShell and you get silence.

`roll_cli.exe` is the same program built as a console application. Use it for
everything on this list except the first line. It matters most for `--probe`,
which has to be able to ask you a question and hear the answer.

### `--preflight` sends nothing

Run it before any live step. It reads the broker as it stands right now and
prints, field by field, the order the app *would* send: the quantity as a
number of lots, the limit price as the integer the API wants, and whether that
price sits on the tick grid and inside today's circuit band. It also reads the
position, both books, the margin one clip needs against the funds available,
and every gate.

It places nothing, cancels nothing, and a clean run is not permission to trade
-- `--probe` is still the first thing that puts an order in front of the
exchange.

### `--probe` places a real order

It is the only way to learn three things that no amount of reading can settle:
what a quantity actually means to the exchange, what the order book's field
names really are, and that the price scale and the cancel path work.

The order is a BUY at the contract's lower circuit, about three rupees under the
market, so it cannot trade. And if the price scale were wrong the price would
land far outside the day's circuit band in every direction, which the exchange
rejects rather than fills — that band is what makes this safe to run *before*
the scale is confirmed.

It needs a session, so log in through the app first. It prints what it is about
to do and sends nothing unless you type `PLACE REAL ORDER` exactly. Everything
it sees is written verbatim to `data/probe-<timestamp>.json`.

## Things worth knowing

- **The REST touchline is cached.** Polled three seconds apart it returns an
  identical payload, server timestamp included, and a quiet far month has been
  seen thirteen minutes behind. The websocket feed is the real source; polling
  is only the fallback, and the header says which is in use.
- **A halt outlives the process, and the trading day.** The clip count and any
  halt are written to `state.json` on every change. Closing and reopening the
  app will not let it roll twice, and will not erase a half-rolled halt — only
  a person clears that. Daily counters reset at the date change; the halt does
  not, because a half-rolled position does not repair itself overnight.
- **The scrip master is reloaded when the day turns over**, because contracts
  expire out of it and every circuit limit moves. The app will not trade on a
  file that is not today's.
- **The limit is in basis points, by tenor.** One month is 30 bps and two
  months 50, because a roll's cost scales with how far you are rolling. A basis
  point is a share of the price: 30 bps is 0.2879 at USDINR 95.97, not 0.30.
  The two are equal only at exactly 100.
- **Two different price scales.** The quote feed returns rupees. Order prices go
  in the contract's own exchange units, which is the rupee price times the
  `PriceDivisor` the scrip master declares: 100 for equity, **10000000** for
  USDINR futures. ₹95.9400 is sent as `959400000`.
- **The feed sends 32 bit floats.** 95.92 arrives as `95.91999816894531`, so
  every price is put back on the exchange's tick grid before it is used.
- **Thin far months.** A far offer often holds only a handful of units. The app
  will not start a roll it cannot finish, which is what the touch size gate is
  for.
- **No IOC validity.** The API offers Day validity only, so immediate-or-cancel
  is built as place, watch for two seconds, cancel the remainder.
- **The order book fill fields are still unconfirmed**, because confirming them
  needs a real fill. The app refuses to act on a fill it cannot read, and halts
  instead.

## Appearance

The colours are Choice Broking's, taken from their site: the navy `#0f1621` and
blue `#2777f3` from the logo, with `#ffce02`, `#45b644` and `#ef404a` for
warning, good and bad. Two of them are too dark to sit on a dark background as
small text, so those keep their exact values for fills and get a lightened pair
for type; everything on screen clears 4.5:1.

The icon is an original mark in that palette rather than Choice's logo. Their
wordmark is a trademark, and shipping it on a third party application in a
public repository would present this as Choice's own software. If you have
written permission to use it, replacing `assets/icon.ico` is the only change
needed.

This project is not affiliated with or endorsed by Choice Broking.
