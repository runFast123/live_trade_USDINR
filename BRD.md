# BRD — USDINR rollover logic

This document contains the trading logic and nothing else. No setup steps, no
architecture, no build instructions. If it is not the rule or a condition on the
rule, it is not in here.

---

## 1. What the app does

Watch two USDINR futures contracts. When the cost of moving the position from the
near contract to the far contract falls below a set limit, sell the near contract
and buy the far contract, one clip at a time. Otherwise do nothing.

| Role | Contract | Action | Price used |
|---|---|---|---|
| Near leg | USDINR futures, nearer expiry (Sep 2026) | **Sell** | its **best bid**, the top rung of the buy ladder |
| Far leg | USDINR futures, later expiry (Nov 2026) | **Buy** | its **best ask**, the top rung of the sell ladder |

Segment: `13` (NSE currency derivatives).

---

## 2. The rule

```
roll_cost = far_leg.best_ask - near_leg.best_bid

if roll_cost < ROLL_LIMIT and every safety gate passes:
    roll one clip using two limit orders
else:
    send nothing, keep watching
```

**Read the formula in that order and no other.** `far ask − near bid`.
The reverse, `near bid − far ask`, is negative in a normal market, and a negative
number is always below a positive limit, so the app would fire on every tick.

`ROLL_LIMIT` default: **0.30**. Configurable.

Comparison is strictly less than. A roll cost of exactly `0.30` does **not**
trade.

### Worked example (from the note)

| | |
|---|---|
| Near best bid | 95.9400 |
| Far best ask | 96.5200 |
| roll_cost | `96.5200 − 95.9400` = **0.5800** |
| Per clip | 0.5800 × 1000 = **₹580** |
| Decision | 0.5800 is not below 0.30 → **send nothing** |

### Arithmetic

Every price and the roll cost are exact decimals. Floating point is never used,
because `96.52 - 95.94` evaluates to `0.5799999999999983` in binary floating
point, which can silently flip the comparison against the limit.

---

## 3. Limit prices actually sent

Orders are limit orders. An optional price allowance improves the chance of a
fill:

```
sell_limit = round_down_to_tick(near.best_bid - allowance)
buy_limit  = round_up_to_tick  (far.best_ask  + allowance)

worst_case = buy_limit - sell_limit
require worst_case < ROLL_LIMIT
```

Both roundings move the prices in the direction that makes the roll more
expensive, so `worst_case` is recomputed from the **rounded** prices, never the
raw ones. If the allowance pushes `worst_case` to or past the limit, the app does
not trade; it does not quietly shrink the allowance.

- Tick: **0.0025**, cross-checked against the exchange's `PriceTick`
- Default allowance: **0 ticks** (strictest)
- Lot: **1000** units, read from `MarketLot`. `qty = lots × 1000`.

All of this arithmetic is in rupees. The conversion into the units actually sent
to the exchange happens once, at the point of sending, and is described in
section 6.

---

## 4. Safety gates

Every gate must pass before any order is sent. **A gate that cannot be evaluated
counts as a failure, never as a pass.** A missed rollover is recoverable; a
duplicated order, a wrong price scale or a half-completed roll is not.

| # | Gate | Blocks when |
|---|---|---|
| 0 | Scrip master is today's | the contract file is from a previous day, so expiries and circuit limits are behind |
| 1 | Not halted | a previous problem has not been cleared by a human |
| 2 | No order in flight | an order from this app is already working |
| 3 | Session | not logged in |
| 4 | Market open | the segment is closed, or the status cannot be read |
| 5 | Contracts resolved | either token is not in today's scrip master |
| 6 | Distinct contracts | both legs point at the same contract |
| 7 | Each leg is a future | the instrument type is not `FUTCUR`; an option can never be a leg |
| 8 | Each leg's tick | the exchange's `PriceTick` is not the configured tick |
| 9 | Each leg's price scale | the scrip master declares no `PriceDivisor`, so the quote scale would have to be inferred |
| 10 | Expiry order | the far expiry does not come after the near expiry |
| 11 | Near not expired | the near contract's expiry has passed |
| 12 | Expiry day cutoff | it is the near contract's expiry day and past 12:25 |
| 13 | Expiry matches config | the scrip master expiry differs from the configured date |
| 14 | Lot size | the lot size is not the expected 1000 |
| 15 | Trading window | outside the configured start/end times |
| 16 | Clips remaining | the day's clip budget is already used |
| 17 | Position | you are not long enough of the near contract, or it cannot be read |
| 18 | Quotes | either leg has no quote |
| 19 | Quote freshness | either quote is older than 3 seconds, or the live feed has gone silent |
| 20 | Leg spread | either leg's own bid/ask spread is wider than 0.0500 |
| 21 | Two-sided market | either leg has an empty bid or ask, or a crossed book |
| 22 | Touch size | the size resting at the near bid or the far ask cannot fill the clip |
| 23 | Tick grid | a feed price is further off the exchange's tick grid than float rounding explains |
| 24 | Price scale | the prices do not land inside the contracts' price limits at the detected scale |
| 25 | Contract price limits | a price is outside that contract's own circuit limits for the day |
| 26 | Roll cost plausible | the roll cost is outside -2.00 to +5.00 (treated as bad data) |
| 27 | Roll cost below limit | `roll_cost >= ROLL_LIMIT`, or `worst_case >= ROLL_LIMIT` |
| 28 | Armed | the operator has not armed the app, or the arming has expired |

### Where the prices come from

The websocket feed is the source. The REST touchline is a **cached snapshot**:
polled three seconds apart it returns an identical payload, its own server
timestamp included, and a quiet far month has been seen thirteen minutes behind.
A roll cost computed from that is not the current cost.

The websocket pushes a message whenever the book moves, and states the price
scale in the message. Polling remains as the fallback for when the socket is
down, and the app says on screen which of the two it is using.

A push feed is silent while nothing changes, so silence is not staleness: the
last message still describes the book. What is dangerous is a socket that has
quietly died, so the feed counts as usable only while the connection is up, both
legs have been seen, and something has arrived within the last two minutes.
Otherwise the app falls back to polling.

The two sources use different scales, and neither is converted into the other:
the feed sends exchange units, REST sends rupees. Each is verified separately
against the contract's price band.

### Gate 22, the size at the touch

The quote feed returns the five deep ladder on each side, and only the top rung
is used. If the near bid or the far offer does not hold enough to fill the whole
clip, the roll does not start.

This matters more than it sounds. A far month offer often holds only a handful
of units. Selling the whole near leg and then finding seven units on the far
offer is precisely how a roll ends up half done, which is the one outcome this
app must never produce. Configurable, on by default.

### Gate 23, the tick grid

The feed sends prices as 32 bit floats, so 95.92 arrives as 95.91999816894531.
Each price is put back on the exchange's tick grid before it is used. Without
that, rounding the near bid down to a tick would give 95.9175, a whole tick
below the real bid, and the near leg would be sold a tick cheaper than intended.
A price further from the grid than float rounding can explain is refused.

### Gates 7 to 9, and what the scrip master decides

Nothing about a contract is assumed. Every one of these is read from today's
scrip master row and then checked:

| Column | Used for | USDINR futures |
|---|---|---|
| `Instrument` | rejecting options | `FUTCUR` |
| `Expiry` | the expiry gates, written as `28SEP26` | 28 Sep 2026 |
| `PriceDivisor` | the price scale | 10000000 |
| `PriceTick` | the tick, cross-checked against config | 25000, so 0.0025 |
| `MarketLot` | the order quantity | 1000 |
| `LowPriceRange` / `HighPriceRange` | the plausible price band | e.g. 93.08 to 98.835 |

Segment 13 holds around 14,000 rows and all but about 170 are options. Only
futures are ever offered as a leg.

The file changes every trading day: contracts expire out of it, new ones appear,
and every circuit limit moves. The app reloads it when the calendar day turns
over, re-reads both contracts from the new file, and refuses to trade on a file
that is not today's.

### Gates 24 and 25, the price scale

**The quote feed and the order API do not use the same scale.** The touchline
returns rupees directly, while an order price is sent in the contract's exchange
units. 95.9200 arrives from the feed as 95.9200 and is sent to the order API as
959200000. The two are never mixed.

The feed's scale is still checked rather than assumed. The app tries the
divisors declared for the two legs along with 1 and 100, and keeps the one that
puts every price inside the two contracts' combined circuit limits. If no divisor works, or more than one does, it refuses
to produce a quote and the rule is never evaluated. Each price is then checked
against its own contract's limits, so a price that is plausible for the far leg
but impossible for the near leg is still rejected. The divisor may not change
during a session.

### Gate 28, arming

Arming is a deliberate act by the operator and expires on its own after 120
seconds. The app never arms itself.

---

## 5. Execution sequence

Two limit orders, sent **one after the other**, never together.

```
1. SELL near leg, qty, at sell_limit
2. Watch for up to 2 seconds
     fully filled    -> go to step 3
     partly filled   -> cancel the remainder, carry the filled qty to step 3
     not filled      -> cancel, no exposure changed, back to watching
     unconfirmable   -> HALT
3. BUY far leg, for exactly the qty that filled in step 1, at buy_limit
4. Watch for up to 2 seconds
     fully filled    -> roll complete, log it, count the clip
     anything else   -> HALT and alert
```

**The near leg goes first on purpose.** If the second leg fails after the first
has filled, this order leaves the account flat, which can be corrected by buying
back. Doing it the other way round would leave the account long two contracts
with one of them about to expire.

Step 3 uses the quantity that actually filled in step 1, not the quantity that
was requested. A partial fill must not turn into an oversized far leg.

### Half-rolled state

If step 4 does not fully fill, the app **halts**, alerts, and stops trading until
a human clears it. It does not retry and it does not chase the price. An optional
auto-unwind (off by default) buys the near leg back to return the account to
where it started.

---

## 6. Constraints that shape the logic

These come from the broker API and change what the rule can do:

- **No market orders.** Limit orders only, so a qualifying quote can fail to fill.
  That is why every send is followed by a fill check.
- **No immediate-or-cancel validity.** Only Day validity is available, so IOC
  behaviour is built as: place, watch for 2 seconds, cancel the remainder.
- **Order prices are sent in the contract's own exchange units**, which is the
  rupee price multiplied by the `PriceDivisor` the scrip master declares:

  | | PriceDivisor | ₹ price | sent as |
  |---|---|---|---|
  | Equity | 100 | 1300.00 | `130000` |
  | USDINR future | 10000000 | 95.9400 | `959400000` |

  The vendor documentation only shows the equity case and calls the result
  "paisa". That is simply what a divisor of 100 amounts to. Using it for a
  currency future would understate the price by a factor of a hundred thousand,
  so the contract's own divisor is always used. A price that is not a whole
  number of exchange units, or that does not sit on the exchange's `PriceTick`
  grid, is refused rather than rounded.
- **Quantity is in units, not lots.** 1 lot is sent as `qty = 1000`.
- **The order number is not chosen by the caller,** so an order is identified in
  the order book as the new one on that token and side.
- **The scrip master writes expiries as `28SEP26`,** a two digit year, and is
  the only source for a contract's expiry.

---

## 7. Conflicts in the original note, and what was decided

| Item | The note said | The message said | Decided |
|---|---|---|---|
| Roll limit | 0.30 | 0.50 | **0.30**, configurable. The stricter of the two. |
| Formula | Nov price − Sep price | "26th Bid − 26th Ask" | **far ask − near bid.** The literal reading is negative and would trade on every tick. |
| Near expiry | 28 Sep | 26 Sep | **Read from the scrip master, never hard-coded.** The configured date is a cross-check: if it disagrees with the scrip master, the app does not trade. |

---

## 8. Operating modes

- **Dry run (default).** Everything is evaluated and logged, nothing is sent to
  the exchange.
- **Live.** Orders are sent, but only after the operator arms the app and every
  gate passes.
