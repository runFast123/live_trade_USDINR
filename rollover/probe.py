"""A single resting order, placed to learn what the exchange actually says back.

Three things about live order handling cannot be confirmed from documentation or
from a fixture, and all three decide whether a real roll behaves:

  1. **What a quantity means.** The app sends `lots x MarketLot`. The exchange's
     own open interest only makes sense if quantities are *contracts*, in which
     case that is a thousand-fold over-order.
  2. **The order book's field names.** The fill reader guesses across half a
     dozen aliases and admits it may fail, and a fill it cannot read halts a
     roll half way through.
  3. **That the order price scale is accepted**, and that cancelling works.

So: place one order that cannot trade, read it back through every endpoint,
write down every field verbatim, cancel it, and confirm the cancellation.

**Why this is safe.** The order is a BUY priced at the contract's lower circuit,
roughly three rupees beneath the market, so it cannot fill. And if the price
scale were wrong, the price would land far outside the day's circuit band in
every direction, which the exchange rejects rather than fills. The band is what
makes this safe to run *before* the scale is confirmed.

It still places a real order, so it asks first.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, Optional

from .broker import BUY, Broker, _first, _ORDER_NO_KEYS
from .money import D, floor_tick, money

CONFIRM = "PLACE REAL ORDER"

# How far beneath the market to rest, before the circuit limit is applied.
CLEARANCE = D("3.00")


class ProbeError(RuntimeError):
    pass


def _capture(label: str, call) -> Dict[str, Any]:
    """Run an API call and record whatever came back, error included."""
    try:
        return {"ok": True, "response": call()}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def choose_price(info, bid: Optional[Decimal]) -> Decimal:
    """A price that cannot trade: well below the market, at or above the floor."""
    if info.low_range is None:
        raise ProbeError(
            "the scrip master gives no lower circuit for this contract, so a "
            "price that certainly cannot fill cannot be chosen")

    floor = info.low_range
    wanted = (bid - CLEARANCE) if bid is not None else floor
    price = floor_tick(max(floor, wanted), info.tick or D("0.0025"))

    if bid is not None and price >= bid:
        raise ProbeError(
            f"the chosen price {price} is not below the bid {bid}; refusing")
    return price


def describe(info, price: Decimal, qty: int, bid: Optional[Decimal],
             units: Decimal) -> str:
    lines = [
        "",
        "  " + "=" * 68,
        "  THIS PLACES A REAL ORDER ON YOUR ACCOUNT",
        "  " + "=" * 68,
        f"  contract   {info.label()}  [{info.instrument}]",
        "  side       BUY",
        f"  quantity   {qty}          <- what this is measured in is the question",
        f"  price      {money(price)}  (sent as {units})",
        f"  market bid {money(bid) if bid else 'unknown'}",
        f"  circuit    {info.low_range} .. {info.high_range}",
        "",
        "  It is priced at the contract's floor, far below the market, so it",
        "  cannot trade. If the price scale were wrong it would land outside",
        "  the circuit band and be rejected rather than filled.",
        "",
        "  It is read back, then cancelled immediately.",
        "",
    ]
    return "\n".join(lines)


def run(cfg, log, base_dir: str, token: Optional[str] = None, qty: int = 1,
        confirm=input) -> int:
    """Place, read back, and cancel one resting order. Returns an exit code."""
    broker = Broker(cfg, log)
    broker.build_client()

    session_path = os.path.join(base_dir, "session.json")
    if not broker.resume(session_path):
        print("No saved session for today. Open the app and log in first, then "
              "run this again.")
        return 2
    broker.load_scrip_master()

    token = token or cfg.near_token
    if not token:
        print("No contract. Set near_token in config.json, or pass --probe-token.")
        return 2
    info = broker.instrument(token)

    # Where is the market? Needed only to prove the price is far from it.
    bid = None
    try:
        from .quotes import QuoteReader
        reader = QuoteReader(broker.client, cfg)
        reader.set_instruments({info.token: info})
        bid = reader.fetch([info.token])[info.token].bid
    except Exception as exc:
        log.warn(f"Could not read a quote ({exc}); using the circuit floor.")

    price = choose_price(info, bid)
    units = broker.exchange_price(info, price)

    print(describe(info, price, qty, bid, units))
    answer = confirm(f'  Type "{CONFIRM}" to go ahead, anything else to stop: ')
    if (answer or "").strip() != CONFIRM:
        print("  Nothing was sent.")
        return 1

    record: Dict[str, Any] = {
        "when": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "contract": {
            "token": info.token, "name": info.sec_desc,
            "instrument": info.instrument, "lot_size": info.lot_size,
            "price_divisor": str(info.price_divisor), "tick": str(info.tick),
            "circuit": [str(info.low_range), str(info.high_range)],
        },
        "sent": {"side": "BUY", "qty": qty, "price_rupees": str(price),
                 "price_units": str(units), "market_bid": str(bid) if bid else None},
    }

    orders = broker.client.orders
    log.info(f"PROBE: BUY {qty} of {info.label()} at {money(price)} ({units})")

    record["place_order"] = _capture("place", lambda: orders.place_order(
        segment_id=cfg.segment_id, token=int(info.token),
        order_type=cfg.order_type, bs=BUY, qty=qty, price=int(units),
        trigger_price=0, validity=cfg.validity, product_type=cfg.product_type))
    print(f"  placed -> {str(record['place_order'])[:160]}")

    # Everything the broker can tell us about it.
    record["order_book"] = _capture("order_book", orders.get_order_book)
    record["order_book_v2"] = _capture("order_book_v2", orders.get_order_book_v2)
    record["trade_book"] = _capture("trade_book", orders.get_trade_book)

    ours = _find_ours(record["order_book"], info.token)
    if ours is None:
        ours = _find_ours(record["order_book_v2"], info.token)
    record["matched_order"] = ours

    order_no = _first(ours, _ORDER_NO_KEYS) if ours else None
    if order_no is not None:
        record["order_by_no"] = _capture(
            "order_by_no", lambda: orders.get_order_by_no(int(D(order_no))))

    # Cancel, whatever happened, then confirm it is gone.
    if ours is not None:
        record["cancel"] = _capture("cancel", lambda: orders.cancel_order(
            client_order_no=int(D(_first(ours, ("ClientOrderNo",)) or 0)),
            exchange_order_no=str(_first(ours, ("ExchangeOrderNo",)) or ""),
            gateway_order_no=str(_first(ours, ("GatewayOrderNo",)) or ""),
            segment_id=cfg.segment_id, token=int(info.token),
            order_type=cfg.order_type, bs=BUY, qty=qty, price=int(units),
            trigger_price=0, validity=cfg.validity,
            product_type=cfg.product_type))
        print(f"  cancelled -> {str(record['cancel'])[:160]}")
        record["order_book_after_cancel"] = _capture(
            "order_book_after", orders.get_order_book)
    else:
        print("  the order was not found in the book, so there was nothing to cancel")

    path = _save(record, base_dir)
    _report(record, info, qty, path)
    return 0


def _find_ours(captured: Dict[str, Any], token: str) -> Optional[Dict[str, Any]]:
    """The most recent row on our token, whatever shape the book came in."""
    if not captured.get("ok"):
        return None
    from .broker import _iter_records

    found = None
    for row in _iter_records(captured["response"]):
        value = _first(row, ("Token", "ScripToken", "InstrumentToken"))
        if value is not None and str(value).strip() == str(token):
            found = row
    return found


def _save(record: Dict[str, Any], base_dir: str) -> str:
    folder = os.path.join(base_dir, "data")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"probe-{datetime.now():%Y-%m-%d_%H%M%S}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2, default=str)
    return path


def _report(record: Dict[str, Any], info, qty: int, path: str) -> None:
    ours = record.get("matched_order")
    print()
    print("  " + "-" * 68)
    print("  WHAT WE LEARNED")
    print("  " + "-" * 68)

    if not ours:
        print("  The order never appeared in the order book, so the field names")
        print("  are still unknown. Check the response above and the broker")
        print("  terminal before drawing any conclusion.")
    else:
        print(f"  order book fields: {', '.join(sorted(str(k) for k in ours))}")
        back = _first(ours, ("Qty", "Quantity", "OrderQty", "TotalQty"))
        print()
        print(f"  we sent quantity {qty}, the book says {back}")
        if back is not None and str(back).strip() == str(qty):
            print(f"  -> the exchange took {qty} at face value.")
            print(f"     If {qty} means CONTRACTS, one lot is qty=1 and the")
            print(f"     app's current clip of {info.lot_size} is {info.lot_size}x too large.")
        print()
        for label, keys in (("status", ("OrderStatus", "Status", "OrdStatus")),
                            ("filled", ("FilledQty", "TradedQty", "FilledQuantity")),
                            ("price", ("Price", "OrderPrice", "LimitPrice")),
                            ("order no", _ORDER_NO_KEYS)):
            print(f"  {label:<9} {_first(ours, keys)}")

    trades = record.get("trade_book", {})
    print()
    print(f"  trade book reachable: {trades.get('ok')}")
    print(f"  order_by_no reachable: {record.get('order_by_no', {}).get('ok')}")
    print(f"  cancel accepted: {record.get('cancel', {}).get('ok')}")
    print()
    print(f"  Everything captured verbatim in:\n    {path}")
