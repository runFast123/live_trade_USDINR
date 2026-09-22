# What the live order probe established

**18 September 2026, 11:19 IST.** One real order was placed on the account:
BUY 1 of USDINR26SEPFUT (token 1769) at 93.0600, the contract's lower circuit,
with the market bid at 95.7800. It was priced so it could not trade, and it did
not trade. The exchange **rejected** it.

Raw capture: `dist/data/probe-2026-09-18_111951.json`. Every claim below is
taken from that file, and each one is pinned by a test in
[tests/test_live_findings.py](tests/test_live_findings.py).

---

## The headline: this account cannot trade currency derivatives

```
"OrderStatus":  "REJECTED"
"ErrorString":  "exchg not enabled for this acct"
```

The rejection is an **entitlement** problem, not a price, quantity or margin
problem. The account is not enabled for the NSE currency derivatives segment
(segment 13).

**This blocks every remaining live step.** Phases 3.2 and 3.3 of the plan — one
lot watched, then one clip at production size — cannot run at all until Choice
enables the segment on this account. It is worth raising with them today, since
it is a back-office change with a lead time we do not control, and it sits on
the critical path in front of everything else.

It is also, in a narrow sense, a good result: the safest possible order found
the problem. Had this surfaced during a real roll, it would have surfaced as a
sold near leg and a refused far leg.

---

## Confirmed working

### The price scale, twice over

This was the single largest unverified risk in the program — an earlier version
sent paisa instead of exchange units and would have priced orders 100,000×
wrong.

| | |
|---|---|
| we sent | `930600000` |
| the order book echoed | `Price: 93.06` |

930600000 ÷ 10,000,000 = 93.06 exactly. Had rupees been sent, the echo would
have read 0.0000093.

A second, independent witness in the same response: `LTP: 957800000.0` against
a live market bid of 95.7800. The same divisor, from a field we do not set.

**The USDINR PriceDivisor of 10,000,000 is confirmed.**

### The plumbing

- login, session reuse, and the daily scrip master refresh all worked unattended
- `place_order` reached the broker and returned a reference
- the order appeared in the order book, in `get_order_book_v2`, and in
  `get_order_by_no`
- the trade book is reachable and returned `{"Trades": []}`, correctly

### The response shapes, now known rather than guessed

```
order book      {"Status": "...", "Response": {"Orders":  [ ... ]}, "Reason": "..."}
trade book      {"Status": "...", "Response": {"Trades":  [ ... ]}, "Reason": "..."}
place_order     {"Status": "Success", "Response": "260918000069382", "Reason": ""}
cancel_order    {"Status": "Fail", "Response": "Invalid Exchange Order Number", ...}
```

Order row fields, verbatim: `AvgBuyPrice, BS, BracketGatewayOrderId,
BracketOrderId, BracketOrderModifyBit, BracketOrderStatus, ClientOrderNo,
DisclosedQty, ErrorString, ExchangeOrderNo, ExchangeOrderTime, GTDDays,
GTDStatus, GatewayOrderNo, InitiatedBy, LTP, LTPJumpPrice, LegIndicator,
ModifiedBy, OrderStatus, OrderType, Price, ProductType, ProfitOrderPrice,
Qty, Remarks, ResponseType, SLJumpprice, SLOrderPrice, SLTriggerPrice,
SegmentId, SeqNo, Symbol, Time, Token, TotalQtyRemaining, TradedQty,
TriggerPrice, Validity`

The fill quantity is **`TradedQty`**, with `TotalQtyRemaining` alongside it. The
app's guess list had `FilledQty` first and `TradedQty` second, so it would have
worked — but by luck, not by knowledge.

---

## Four faults the probe exposed, now fixed

### 1. "Success" did not mean accepted

`place_order` returned `{"Status": "Success"}` for an order the exchange
rejected. Anything reading that response as confirmation is wrong. A failure
there is still conclusive — nothing was sent — but a success means only that
the request was carried.

### 2. A failed call arrived as an ordinary response

The cancel came back `{"Status": "Fail", "Response": "Invalid Exchange Order
Number"}`. It raised nothing, so the app logged **"cancel sent"** for a cancel
that had been refused. The vendor client treats any HTTP 200 as success, so
every envelope now gets looked inside (`call_failed`).

This was the most dangerous of the four: under the planned hardening a failed
cancel must halt the roll, and the app could not see that one had failed.

### 3. The rejection reason was thrown away

`ErrorString` carries the only actionable sentence in the whole response, and
nothing read it. The operator would have seen `filled 0 of 1 (rejected)` and had
no way to learn the account was not enabled. The reason now travels with the
status into the log and the outcome detail.

### 4. An order's reference changed between polls

`GatewayOrderNo` was `null` and `ExchangeOrderNo` was `""` on a freshly placed
order, so the app identified it by `ClientOrderNo`. Once the exchange
acknowledges an order, `ExchangeOrderNo` fills in and the preference order makes
the *same order* answer to a *different* reference. Two polls, two identities.

Worse, orders are found by "the row on this token and side that was not here
before". An order placed moments earlier by someone else, recorded under its
ClientOrderNo, would acquire an ExchangeOrderNo and then look new — and be
treated as ours.

Identity is now `ClientOrderNo` alone, which is present from the first moment
and does not change.

A related discovery: `choice_api` hard-codes `"ClientOrderNo": 123456` into
every payload, and the app was built believing the book would echo that back,
making it useless for identification. The broker in fact overwrites it with its
own per-account sequence (`100000014`). That is one observation, so the
placeholder value is still treated as meaningless and falls back to the old
behaviour rather than collapsing every order onto one identity.

### And one found while fixing them

`"PartiallyFilled"` contains the substring `"filled"`. The terminal-order check
would have read a partly filled, still-working order as finished, skipped the
cancel, and moved to the second leg leaving the remainder live at the exchange.
Partial statuses are now excluded explicitly.

---

## Settled: a quantity is in units

**Confirmed by the account holder on 18 September 2026: the exchange reads an
order quantity as units of the underlying, not as contracts.**

So `qty = lots x MarketLot` is right, and one lot of USDINR is `qty = 1000`.
`quantity_unit_confirmed` is now true in config.json, which clears the first
precondition on live mode.

This is a statement from the account holder rather than something this program
observed, and the distinction is worth keeping: the probe could not settle it,
and still has not. Once the segment is enabled, one accepted order at `qty=1000`
would turn the confirmation into an observation. Until then the record should
say plainly where the answer came from.

The reasoning that pointed the other way is left below, because if it is ever
revisited this is the evidence that will need explaining.

### Why it looked like contracts

**The probe could not settle this, and must not be read as having settled it.**

The order book shows `Qty: 1` — but that only proves the broker echoed what we
sent. The order was refused on account entitlement *before* the exchange
validated anything, so no exchange-side check of the quantity ever ran.

The open question is unchanged:

| Field (USDINR Sep, 17 Sep) | As contracts | As USD units |
|---|---|---|
| open interest 2,942,758 | $2.94 bn | $2.94 m |
| day volume 643,253 | $643 m | $643 k |
| best bid 338 | 338 lots | 0.338 of a lot |

NSE front-month USDINR open interest is billions, not millions, which points at
**contracts**. If that is right, the app's `qty = lots × 1000` orders a thousand
lots when it means one.

Two ways to settle it, neither of which needs a fill:

1. **Ask Choice directly**, at the same time as asking them to enable the
   segment. They can answer it in one sentence.
2. **Re-run the probe once the segment is enabled.** A rejection reading
   "quantity not a multiple of market lot" would prove units; acceptance of
   `qty=1` at the circuit floor would prove contracts.

The open interest figures are the one thing that still does not sit comfortably
with "units", and they are worth putting to Choice at the same time as the
segment enablement. Nothing in the app depends on resolving that, since the unit
is now set from the account holder's answer.

---

## What swagger.json added

The vendor's OpenAPI spec (`swagger.json`, ChoiceOpenTransactionPusher) does not
document the order placement endpoint, but its shared descriptions settle three
things the app had been guessing at.

### Validity 4 is immediate-or-cancel

    Validity    1 for Day, 4 for IOC

The app was written believing only Day validity existed, and builds IOC by hand:
place, poll for a fill window, cancel the remainder. That hand-built version is
the source of the cancel race, and of the window in which a partial fill is
still working while the second leg is being priced.

Real IOC removes both. `validity` now accepts 4 and `place_leg` knows an IOC
order cannot rest, so it treats one that does as an alarm rather than something
to cancel. The default is still 1, because IOC has not been exercised against
the exchange and defaulting to an untested path would be trading one unknown
for another.

### Two order statuses were being read as live orders

The documented vocabulary is `CLIENT XMITTED`, `GATEWAY XMITTED`, `OMS XMITTED`,
`EXCHANGE XMITTED`, `PENDING`, `CANCELLED`, `EXECUTED`, `GATEWAY REJECT`,
`OMS REJECT`, `ORDER ERROR`, `FROZEN`, `A.ACCEPT`, `A.REJECT`, `A.MODIFY`,
`A.CANCEL`, `AMO SUBMITTED`, `AMO CANCELLED`.

The app matches on words, and two of those contain none of the words it looked
for:

- **`ORDER ERROR`** -- "Order rejected from exchange". The ordinary
  exchange-side rejection, read as a working order.
- **`FROZEN`** -- "rejected from exchange (the order will go for a freeze if the
  quantity is greater than the freeze quantity determined by NSE)".

Both would have had the app try to cancel an order the exchange had already
thrown out; the cancel would be refused, and under the hardening a refused
cancel halts the roll. So every exchange rejection would have become a halt.

`FROZEN` is the worse of the two, because it is not an error at all -- it is the
routine answer to a clip larger than NSE's freeze quantity, and the sane
response is to size down. **That is a live constraint for Phase 2**, which plans
clips out of a 100+ lot position: the freeze quantity is a per-contract ceiling
that clip sizing has to respect, and it is not in the scrip master.

Also worth noting: `PENDING` means "order confirmed from exchange", i.e. working
-- not awaiting submission.

### Smaller confirmations

- `price` is `integer/int64` in the schema, consistent with exchange units
- `segmentId` 13 is `NSECDS-DERIVATIVES (CURRENCIES)`, as configured
- `productType` `D` is delivery/carry-forward, which is what a roll wants
- `averageTradedPrice` exists, which is what Phase 2.7 needs for a realised
  cost rather than an assumed one
- `RL_MKT` and `SL_MKT` do exist, so the old comment that the API has no market
  orders was wrong. It makes no difference here: a market order would ignore
  the price limit that is the entire instruction.

---

## What this changes in the plan

- **Phase 0 (quantity unit): still open.** Now blocked behind the entitlement.
- **Phase 1.3 (honest order outcomes): largely done**, and no longer theorised —
  every part of it was observed.
- **Phase 1.4 (trade book as the fill record): done.** It now confirms each
  leg against the order book and reports a disagreement, without halting on a
  row shape that is still unconfirmed.
- **Phases 1.5 to 1.8: done.** Margin gate (both legs in one request),
  post-trade reconciliation, cancel-all with a clean shutdown, and a JSONL
  journal.
- **Phases 3.2, 3.3 and 4: blocked** until the account is enabled.
- **New for Phase 2:** clip sizing must respect NSE's freeze quantity,
  or oversized clips come back FROZEN.
- **Live mode** is now a guarded switch in the window rather than a
  config edit, and it refuses to engage while the quantity unit is
  unconfirmed. See the README.

The evidence recorder can keep running throughout. It needs no order permissions
and the question it answers — whether the cost ever reaches the client's limit —
is independent of all of this.

---

## 19 September 2026: a second blocker, found without placing anything

Checked read-only against the live session, on a Saturday with the market
closed. `get_margin` and `get_funds_view` ask questions; neither sends an
order, so none of this cost an order attempt.

```
get_funds_view -> Status: Success
    CashAvailable   0.0      MarginAvailable  0.0
    Collateral      0.0      Deposit          0.0
    MarginUsed      0.0      ODLimit          0.0
```

**The account is empty.** Every figure is zero.

This is a *second* blocker, independent of the entitlement one above, and it
was not visible from the probe: the order was rejected on entitlement before
margin was ever assessed. Enabling segment 13 would not by itself make phase
3.2 possible — a one lot USDINR roll still has to be funded.

`get_margin` for the two legs returned `{"Status": "Fail", "Reason": "No data
found"}`. On a closed market that is ambiguous: it may be the segment, or it
may be that there is nothing to price against out of hours. It should be asked
again during a session before anything is concluded from it.

### What this did confirm

The margin gate works against the real account, which had never been
exercised before:

```
required   None          (the call could not be answered)
available  0.0
affordable None          -- "could not be established"
would a live order be let through?   False
```

The gate treats unknown as unaffordable and refuses, which is the behaviour
the design asks for: guessing in the optimistic direction here is how a naked
short happens. It is the first time that path has been run against live data
rather than a fixture.

### Where this leaves phase 3

Two things must be true before 3.1 can even be attempted, and neither is code:

1. Choice enables segment 13 on the account.
2. The account is funded.

Plus a trading day. September stops trading at 12:30 on **28 September**, which
is six sessions away, so the recommendation in the plan stands and has become
urgent: roll September by hand this cycle.

---

## Settled by Choice, 19 September 2026: the quantity unit

Asked in writing, answered in writing. **An order quantity is in units of the
underlying.** One USDINR contract is `Qty = 1000`.

| | |
|---|---|
| `placeorder` `Qty` for one contract | **1000**, not 1 |
| unit | units of the underlying (USD) |
| must be a multiple of `MarketLot` | **yes**, hard-enforced by OMS/RMS and the exchange |
| order book `Qty`, `LeavesQty` | same unit as sent |
| trade book `TradedQty`, `FilledQty` | same unit |
| `get_margin` `token\|qty` | same unit — `1769\|1000` for one contract |
| freeze limit | 10,000 contracts = `Qty` 10,000,000 |

This confirms the app's existing clip of `lots x 1000`. It is **not** a
thousand-fold over-order. `quantity_unit_confirmed` is now true on evidence
rather than on a statement.

### But their answer about the market feed is wrong, and their own rule proves it

Choice also said the feed's `BidQty` / `AskQty` are "normalized and broadcast
in units of the underlying", so that "a BidQty of 5000 represents 5
contracts".

That cannot be true, and the proof is their own answer above. If every order
quantity must be an exact multiple of 1,000 units, then the total resting at a
price is a sum of multiples of 1,000, and so a multiple of 1,000 itself.

Of 11,272 depth readings recorded on 18 September across **both** the
websocket feed and the polled touchline, **11,092 were not multiples of
1,000**. The commonest values were 10, 50, 100, 30, 5, 1, 8, 2. Read as
contracts every one of them is ordinary: 1 contract is $1,000 resting, 302 is
$302,000.

**The feed is in contracts. The order is in units.**

### What that cost

The touch-size gate compared a clip in units against a size in contracts, so
a one lot clip demanded a thousand lots resting before it would trade.

    Sep -> Nov, 2,117 observations on 18 September
      touch-size gate passed, reading depth as units     : 0
      touch-size gate passes, reading depth as contracts : 2,117

It blocked every observation of a full trading day. Sizes are converted to
order units as the quote is built (`depth_in_lots`, default true), so
everything downstream compares like with like.

The price was still never inside the limit that day, so the roll would not
have fired regardless -- but it would have been the price stopping it rather
than a unit mismatch, and only one of those is worth acting on.

### Worth putting back to them

Their answers 3 and 4 are mutually inconsistent. Worth asking which feed, if
any, normalises to units -- and whether `TotalBuyQty` / `TotalSellQty` differ
from the touch sizes, since the app does not use them today but the depth
ladder work in phase 2 will.

### Choice confirmed the split, 19 September 2026

Asked again, with the arithmetic. They agreed, and gave the full table.

| Field | Unit |
|---|---|
| `placeorder` `Qty` | units of the underlying |
| order book `Qty`, trade book `TradedQty` | units |
| `get_margin` `qty` | units |
| touchline `BidQty` / `AskQty` | **contracts** |
| depth `TotalBuyQty` / `TotalSellQty` | **contracts** |
| `Volume` / `TotalTradedQty` | **contracts** |

Their own translation rule is what the app now does: order quantity is feed
quantity times MarketLot.

The app reads none of the depth totals or the day volume, so nothing else
needed changing. `TotalBuyQty` and `TotalSellQty` do appear in broker.py, but
as fallback aliases when reading the POSITION book, which is a different
response from a different endpoint. Worth knowing the names collide.

### Still unanswered: the position book

Their table does not cover it, and it is the one that decides whether the app
can roll anything. A 100 lot holding read as 100 units would refuse to trade;
100,000 units read as 100,000 lots would try to roll a thousand times too
much.

There is no evidence to settle it either way, because this account holds
nothing. So the app now settles it by itself: a **position unit** gate refuses
when the reported holding is not a whole multiple of the lot size. A position
is always whole lots -- an exchange cannot fill a part contract -- so a figure
that is not is a figure in the wrong unit. It names both readings and blocks
until the operator confirms.

Same arithmetic that settled the depth question, applied to the one field
still open.

---

## 21 September 2026: a new account, and segment 13 is live

The account was changed. Checked read-only -- `get_margin`, `get_funds_view`
and `get_net_position` ask questions and place nothing.

### Segment 13 is enabled

`get_margin` for segment 13 returns `Status: Success` with real figures for
both legs. The entitlement that blocked every live step since 18 September is
gone.

```
Span_Summary: {"Span": 3638.0, "ExpMgn": 959.35,
               "OptionPremium": 0.0, "MgnBenefit": 0.0, "TotalMgn": 4597.35}
Margins:      [{"Token": 1769, "QTY": 1, "InitialMargin": 1817.0, "ExpMgn": 478.9},
               {"Token": 1284, "QTY": 1, "InitialMargin": 1821.0, "ExpMgn": 480.45}]
```

### The quantity unit, confirmed a third time and by the exchange side

`token_qty` was sent as `1769|1000` and `1284|1000`. The response echoes
**`"QTY": 1`** for each. So 1,000 units is one contract, exactly as Choice
said in writing, and now demonstrated by the broker's own arithmetic rather
than by their description of it.

### There is no calendar spread benefit

`MgnBenefit` is **0.0**, and the total is the simple sum of the two legs:
1817 + 478.9 + 1821 + 480.45 = 4,597.35. The plan assumed a calendar spread
would net down and therefore cost far less than two outrights. It does not,
on this account.

That changes the funding arithmetic, and not by a little:

| lots | margin needed |
|---|---|
| 1 | Rs 4,597 |
| 10 | Rs 45,973 |
| 100 | **Rs 459,725** |

### The account holds Rs 825.40 and no position

`CashAvailable` and `MarginAvailable` are both 825.40; everything else is
zero. `get_net_position` returns Success with **no rows at all** -- no
September, no position in any contract.

So one lot is short by Rs 3,772, and the 100+ lot roll the whole app exists
for needs about Rs 460,000 of margin against a position that is not there.

### A bug this found: the app could not read the margin response

`margin.required` came back `None` -- "the margin requirement for this roll
could not be read". The figure lives in `Span_Summary` under the name
`TotalMgn`, which was in none of the spellings tried.

Unknown blocks rather than permits, so nothing unsafe followed. But it also
means the margin gate could never have passed, and a funded account would
have been stopped by it with a message saying only that the figure was
unreadable.

The basket total is now read by name. It is read by name rather than by
scanning because `_iter_records` walks a **per-leg** record first: a generic
scan that recognised a per-leg spelling would report 1,817 as the roll's
margin instead of 4,597 -- an undercount of about half, in the direction that
lets an order through.

Against the live account it now says:

    margin Rs 4,597 needed, Rs 825 available: NOT ENOUGH (-Rs 3,772 spare)

### The second section is quoted now

Confirmed against the live feed, which is what v2.7.1 fixed:

    tokens the engine wants quoted: ['1769', '1584', '1284']
    1769 USDINR26SEPFUT  bid 95.7750 x 40000   ask 95.7800 x 10000
    1584 USDINR26NOVFUT  bid 96.2000 x 50000   ask 96.5000 x 211000
    1284 USDINR26OCTFUT  bid 96.0800 x 30000   ask 96.0900 x 93000

Every depth figure is a whole multiple of 1,000, which is the contracts to
units conversion working.

### And the market is saying something useful

    Sep -> Nov  cost 0.7250  =  75.7 bps  against 50   25.7 bps away
    Sep -> Oct  cost 0.3150  =  32.9 bps  against 30    2.9 bps away

The two month roll is further out than the recorded day suggested. The **one
month roll is close** -- 2.9 bps -- and the recorded day agrees: Sep into Oct
sat at a median of 31.3 bps and spent 7.4% of that day at or below 30.

---

## 22 September 2026: why the screen kept saying it had no quotes

Reported as "the No live quotes error". Two faults, both measured against the
live socket rather than reasoned about, and one of them was not ours.

### The websocket kills itself every ninety seconds

`choice_api.PriceFeedSocketClient._ws_run` calls

```python
self.ws.run_forever(ping_interval=30, ping_timeout=10)
```

The Choice broadcast server **never answers a WebSocket PING**. So
`websocket-client` decides the connection is dead and closes it — while it is
delivering ticks. Measured twice on the live socket, 11:02 and 10:54 IST:

```
WEBSOCKET ERROR => ping/pong timed out
WEBSOCKET CLOSED => Code=None | Msg=None
Reconnecting websocket in 3 seconds...
```

Closes came **91 seconds apart**, and the app's own log shows the same cadence
all morning (reconnects at 09:25:41, 09:27:14; 10:09:57, 10:11:31 — 93 and 94
seconds). Driving the same vendor callbacks with `ping_timeout=None` ran
without a single close in the same window; the pings still go out, only the
demand for an answer is dropped.

Fixed in `LiveFeed._keep_alive`, which runs the loop itself. It checks the
vendor client's shape first and leaves it alone if a future version does not
match, so an upgrade cannot break the feed.

### One failed REST poll took every price off the screen

Each drop put the app on the REST touchline for a few seconds, and that
endpoint fails often — three distinct ways, all seen today:

| What came back | Seen |
|---|---|
| `HTTP 500 Internal Server Error` | 10:49:46, 10:55:31 |
| `Timeout performing HGET MarketData ... StackExchange.Redis` | 10:11:46 |
| `{"Status":"Success","Response":{"MultipleTouchline":None}}` | repeatedly on 21 Sep |

`_read_quotes` let that raise, the whole tick was abandoned, and both leg
cards went to "no quote" with the roll cost blank. Roughly once every two to
five minutes.

Now: a tick that arrived moments ago is preferred over a poll, and if the poll
does fail the last feed prices stay on screen. **That cannot cause a trade on a
stale price** — a held quote is dated from when the tick *arrived*, not from
when it was read, so the freshness gate refuses it the moment it passes
`max_quote_age_sec`. Asserted in
[tests/test_feed_survives_and_quotes_persist.py](tests/test_feed_survives_and_quotes_persist.py).

Also fixed: `_set_source` was never reached when the poll raised, so the screen
kept claiming "live feed" while it was in fact polling and failing.

---

## 22 September 2026: the account now reads empty

`roll_app --preflight` (new, read-only) against the live broker at 11:28 IST.

**The order path itself is sound.** On the live book, both legs passed every
check the exchange enforces:

```
near leg (SELL USDINR26OCTFUT) 1,000 units = 1 lot of 1,000
  price 96.0825 sent as 960825000 (divisor 10000000, tick 0.0025)
  inside the circuit band 93.2425..99.0075
far leg  (BUY USDINR26NOVFUT) 1,000 units = 1 lot of 1,000
  price 96.4175 sent as 964175000, inside 93.5275..99.3125
order_type=RL_LIMIT product_type=D validity=1
```

Order book and trade book both readable and both empty. `get_margin` answers,
so segment 13 is still entitled: one lot of the roll needs **Rs 4,594**.

**But the account has nothing in it.** Not a read failure — every call returns
`"Status": "Success"` with a fully-formed body:

```json
"FundsView": {"CashAvailable": 0.0, "MarginAvailable": 0.0, "MarginUsed": 0.0,
              "Collateral": 0.0, "Deposit": 0.0, ...}
"NetPositions": []
```

Yesterday the same app read Rs 61.5 lakh cash, Rs 1.62 crore margin available,
and **long 4 lots of October**. Today: zero, and no positions at all.

The `vendor_id` and mobile number in `config.json` are unchanged; the
**`api_key` is different** from the one in `config.json.bak`. So either the new
key is registered against a different trading account, or the funds and the
October position left this one overnight. That is a question for Choice, not
one this app can answer.

**Phase 3 cannot start in this state**: 3.1's probe needs margin, and 3.2 and
3.3 need a near-month position to roll.

Unchanged and still outstanding: `max_leg_spread` is 0.05 while November's
spread read **0.1050** today, which remains the one gate blocking an
otherwise-ready roll.
