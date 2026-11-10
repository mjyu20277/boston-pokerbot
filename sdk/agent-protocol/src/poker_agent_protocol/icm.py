"""ICM equity. It lives here so bots can use it on the competition machines, which have this
package but not the engine; bots import it as `poker_agent_sdk.icm`."""

from collections.abc import Sequence


def icm(stacks: Sequence[int], payouts: Sequence[float]) -> list[float]:
    """Expected payout per player (Malmuth-Harville): each place is won in proportion to chips.

    Dynamic programming over the set of players already placed: O(2^n * n) for n <= 10.
    """
    players = [player for player in range(len(stacks)) if stacks[player] > 0]
    total = sum(stacks[player] for player in players)
    equity = [0.0] * len(stacks)
    places = min(len(payouts), len(players))
    # chance[mask]: probability that exactly the players in `mask` took the first places.
    chance = {0: 1.0}
    for place in range(places):
        following: dict[int, float] = {}
        for mask, probability in chance.items():
            remaining = total - sum(stacks[p] for i, p in enumerate(players) if mask >> i & 1)
            for i, player in enumerate(players):
                if mask >> i & 1:
                    continue
                share = probability * stacks[player] / remaining
                equity[player] += share * payouts[place]
                following[mask | 1 << i] = following.get(mask | 1 << i, 0.0) + share
        chance = following
    return equity
