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
