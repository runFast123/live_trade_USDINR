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

118 tests. [tests/test_realdata.py](tests/test_realdata.py) is built from real
captured responses: the scrip master row, the `MultipleTouchline` payload,
`MarketStatus` and `NetPosition`.

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
and every safety gate with the blocking ones listed first.

- **ARM** allows the next qualifying quote to trade. It expires after 120
  seconds and the app never arms itself.
- **ROLL LIMIT** is editable here. A new value is validated the same way
  `config.json` is, saved, and **always disarms**, so a typed digit can never
  fire a roll on the next tick.
- **Change contracts** reopens the chooser and restarts the watch loop on the
  new pair.

Every window scrolls, so nothing is out of reach on a small screen.

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

Tag a commit and [the workflow](.github/workflows/release.yml) builds the exe,
publishes it with its SHA256, and the update button then offers it to everyone:

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
| `logs/` | a dated audit log of every decision and order |

## Command line

```
roll_app.exe                 the windows
roll_app.exe --find USDINR   list contracts and tokens
roll_app.exe --check         validate config.json and exit
roll_app.exe --selftest      run the rule on worked examples, no network
```

## Things worth knowing

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
