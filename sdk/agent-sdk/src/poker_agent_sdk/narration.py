"""Spectator wording for replay frame labels, shared by the replay builder and local viewer."""

import re
from typing import Literal, TypedDict

Tone = Literal[
    "deal", "post", "passive", "aggressive", "allin", "fold", "hidden", "show", "street", "result"
]


class Say(TypedDict):
    tone: Tone
    text: str
    tag: str
    automatic: bool


AUTOMATIC = " · automatic fallback"
_BET = re.compile(r"^(All-in · )?(ante|blind|call|raise) ([\d,]+)$", re.IGNORECASE)
_DEAL = re.compile(r"^Dealing card \d+$|^Cards dealt$")
_STREET = re.compile(r"^(Flop|Turn|River|Board)( · board (\d+))?$")


def narrate(label: str, *, blinds: tuple[int, int]) -> Say:
    automatic = label.endswith(AUTOMATIC)
    if automatic:
        label = label[: -len(AUTOMATIC)]

    def say(tone: Tone, text: str, tag: str = "") -> Say:
        return {"tone": tone, "text": text, "tag": tag, "automatic": automatic}

    bet = _BET.match(label)
    if bet:
        all_in = bool(bet[1])
        kind = bet[2].lower()
        amount = bet[3]
        if kind == "raise":
            return say(
                "allin" if all_in else "aggressive",
                f"moves all-in, raising to {amount}" if all_in else f"raises to {amount}",
                f"All-in {amount}" if all_in else f"Raise to {amount}",
            )
        if kind == "call":
            return say(
                "allin" if all_in else "passive",
                f"calls all-in for {amount}" if all_in else f"calls {amount}",
                f"All-in {amount}" if all_in else f"Call {amount}",
            )
        if kind == "ante":
            return say("post", f"posts an ante of {amount}", f"Ante {amount}")
        value = int(amount.replace(",", ""))
        small, big = blinds
        which = "big" if value == big else "small" if value == small else None
        if all_in:
            return say("allin", f"posts {amount} and is all-in", f"All-in {amount}")
        if which:
            return say(
                "post",
                f"posts the {which} blind of {amount}",
                f"{'BB' if which == 'big' else 'SB'} {amount}",
            )
        return say("post", f"posts a blind of {amount}", f"Blind {amount}")
    if label == "Check":
        return say("passive", "checks", "Check")
    if label == "Fold":
        return say("fold", "folds", "Fold")
    if label == "Muck":
        return say("fold", "mucks", "Muck")
    if label == "Shows":
        return say("show", "shows", "Shows")
    if label == "Sealed bid submitted":
        return say("hidden", "submits a sealed bid", "Bid submitted")
    if label == "Sealed bids submitted":
        return say("hidden", "Every player submits a sealed bid")
    if label.startswith("Auction options:"):
        return say("street", "Choose the flop · sealed bids")
    if label == "Showdown":
        return say("street", "Showdown")
    if label == "Pot awarded":
        return say("result", "")
    if _DEAL.match(label):
        return say("deal", "Cards are dealt")
    street = _STREET.match(label)
    if street:
        return say("street", f"{street[1]} on board {street[3]}" if street[3] else street[1])
    return say("passive", label, label)
