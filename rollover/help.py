"""What the screen means, written for the person reading it.

Every entry here comes from a question that was actually asked while using the
app, not from a guess about what might be unclear. The wording answers the
question rather than describing the control: "can both sections trade at the
same time" is answered with what happens, not with a list of buttons.

No Tk in this module, so the text can be checked by a test.
"""
from __future__ import annotations

from typing import List, Tuple

# (heading, [paragraph, ...])
Topic = Tuple[str, List[str]]


TOPICS: List[Topic] = [
    ("What this app does", [
        "It watches two contracts and tells you what it would cost to roll "
        "from one to the other right now: sell the near month, buy the far "
        "month. The cost is the far ask minus the near bid, shown in rupees "
        "and in basis points of the near price.",

        "It only trades when that cost is at or below your limit, and only "
        "when you have armed it. In DRY RUN it never sends anything at all, "
        "whatever the price does.",
    ]),

    ("The two numbers both called a limit", [
        "ROLL LIMIT is the ceiling for this tenor. It comes from the "
        "schedule -- so many basis points for a one month roll, so many for "
        "two -- and no rung may ever be looser than it.",

        "A card under ROLL COST AT EACH LIMIT is a rung: a limit you have "
        "chosen, with a quantity to roll at it. Several rungs add up, and "
        "the cheapest one with quantity left is worked first.",

        "So a box reading 30 above a line reading 25 bps is not a "
        "contradiction: 30 is the ceiling, 25 is the rung being worked.",
    ]),

    ("Limits with a quantity, and limits without", [
        "A limit with a quantity will be traded, up to that quantity, and "
        "never at a looser price.",

        "A limit with the quantity left blank is a watch line. It is priced "
        "and compared so you can see what a different limit would look like, "
        "and it is never traded. That is the safe place to ask 'what would "
        "fifty give me?' -- the ROLL LIMIT box is not, because it is the "
        "ceiling.",
    ]),

    ("Sections: more than one roll at once", [
        "A section is one roll: its own two contracts, its own limits, its "
        "own quantities. Add section opens the contract list and nothing is "
        "chosen for you.",

        "A new section arrives switched off and with no limits, because a "
        "section with no limits has no cap on what it would sell. Set its "
        "limits, then press Enable.",
    ]),

    ("Can two sections trade at the same time?", [
        "Both are watched and priced at the same time, always. Both can be "
        "ready at the same time.",

        "But only one order is ever live on the account. While a roll is "
        "working, every other section shows 'an order is already working' "
        "and stands down. When several are ready at once, the one furthest "
        "inside its OWN limit goes first -- not the one with the lowest "
        "cost, since each is judged against its own limit.",

        "On the near contract's last trading day that reverses: whichever "
        "has the most left to roll goes first, and price only breaks ties. "
        "What must not happen that day is one section finishing while "
        "another is left holding the expiring month.",
    ]),

    ("Two sections, one position", [
        "Sections selling the same near month are spending one position "
        "between them. What they claim together has to fit inside what you "
        "actually hold, and that is settled when you arm rather than "
        "discovered when the second order is rejected.",

        "Neither can sell into the other's share. The line under the "
        "sections says how much is claimed against how much is held.",

        "This is why a section with no limits cannot be switched on "
        "alongside another: with no cap it could roll the whole position and "
        "leave the other nothing.",
    ]),

    ("How much it will do in a day", [
        "Each section has its own daily clip count. max_clips_per_day_account "
        "caps what they do together -- without it, two sections allowed one "
        "clip a day are two clips on your account.",

        "Arming lasts for one clip. After a roll fires it disarms, so the "
        "next one is a fresh decision you make.",
    ]),

    ("The Waiting for column", [
        "It says the first thing stopping that section from trading, in the "
        "same words as the SAFETY GATES table lower down.",

        "Every gate has to pass before anything is sent. They are not "
        "warnings: any one of them failing stops the roll.",
    ]),

    ("Working the sections list", [
        "Click a row to work on it -- the cards below, and the limits, "
        "follow whichever row is selected.",

        "Double-click a row to switch it on or off. Click a column heading "
        "to sort by it, again to reverse, a third time to go back to the "
        "configured order. Sorting only changes the order they are listed "
        "in; it cannot change what trades.",

        "A '>' beside a name means that section is ready and would roll "
        "first.",
    ]),

    ("Dry run and live", [
        "DRY RUN is the resting state and sends nothing. Go live... asks you "
        "to type a confirmation and shows what you would be committing, "
        "across every enabled section.",

        "Going back to dry run is instant and asks nothing.",
    ]),

    ("Where the prices are coming from", [
        "The line above the leg cards says which source is being used. "
        "live feed is the websocket and is what you want. polled is the "
        "broker's REST snapshot, which lags.",

        "live feed, reconnecting means the socket dropped and the last "
        "prices it sent are still on screen. They age, and once they pass "
        "the freshness limit nothing can trade on them.",

        "polled, not answering means the broker's price endpoint is failing. "
        "The screen keeps the last live prices so you can still see the "
        "market; the same freshness limit still applies.",
    ]),

    ("If something goes wrong", [
        "HALTED stops everything, not one section: they sell the same month "
        "into the same uncertainty. It survives closing the app, and only "
        "you can clear it.",

        "Cancel all cancels any working order. Closing the window also "
        "cancels anything live rather than leaving it at the exchange.",
    ]),
]


def as_text() -> str:
    """The whole guide as plain text, for the console or a file."""
    out = []
    for heading, paragraphs in TOPICS:
        out.append(heading)
        out.append("-" * len(heading))
        for paragraph in paragraphs:
            out.append(paragraph)
            out.append("")
    return "\n".join(out).rstrip() + "\n"
