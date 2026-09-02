"""Splitting one shared cost across several properties so the parts sum EXACTLY to the whole.

Used by portfolio ("bulk") purchases, where the closing costs are a single settlement figure
and the debt is one blanket loan over the whole package, but every downstream metric is
per-property and reads ``property_investment``.

The invariant that makes this safe is arithmetic, not stylistic: **the allocated shares always
sum back to the stated total, to the cent.** That is what lets the portfolio-level aggregates
(Σ NOI ÷ Σ price, Σ cash flow ÷ Σ equity) come out identical to modelling the deal as one
entity — a naive ``round(total * weight / sum)`` per property loses or invents pennies, and a
portfolio-wide figure computed from those shares would no longer match the real deal.

The method is *largest remainder*: give everyone their floor in whole cents, then hand the
leftover cents out to whoever was rounded down hardest. It is the same rule used for
apportioning legislative seats, and it is the only common one that both keeps the sum exact
and never systematically favours the same member.
"""

from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal

CENTS = Decimal("0.01")


def allocate(total: Decimal, weights: list[Decimal]) -> list[Decimal]:
    """Split ``total`` across ``weights``, returning shares that sum to exactly ``total``.

    ``weights`` are relative and need no particular scale (purchase prices, unit counts,
    square footage). If they sum to zero — e.g. a deal where every price is still 0 — the
    split falls back to equal shares rather than dividing by zero.

    Shares are whole cents. Any leftover cent from the division goes to the members with the
    largest fractional remainder, so the result is both exact in sum and as close to the true
    proportions as cent-granularity allows.
    """
    n = len(weights)
    if n == 0:
        raise ValueError("cannot allocate across zero members")
    if total < 0:
        raise ValueError("cannot allocate a negative total")

    # Work in integer cents throughout: this is the only representation in which "the parts
    # sum to the whole" is decidable without float slop.
    total_cents = int((Decimal(total) * 100).to_integral_value(ROUND_HALF_UP))

    weight_sum = sum(weights)
    if weight_sum <= 0:
        weights = [Decimal(1)] * n
        weight_sum = Decimal(n)

    exact = [Decimal(total_cents) * Decimal(w) / weight_sum for w in weights]
    shares = [int(e.to_integral_value(ROUND_FLOOR)) for e in exact]

    # Hand out the cents lost to flooring, largest fractional remainder first. Ties break on
    # the larger weight, then on position, so the result is deterministic for a given input
    # rather than depending on dict/set ordering.
    leftover = total_cents - sum(shares)
    order = sorted(
        range(n),
        key=lambda i: (exact[i] - shares[i], Decimal(weights[i]), -i),
        reverse=True,
    )
    for k in range(leftover):
        shares[order[k % n]] += 1

    return [(Decimal(c) / 100).quantize(CENTS) for c in shares]


def allocation_weights(method: str, prices: list[Decimal]) -> list[Decimal]:
    """The relative weights implied by an allocation method, given each member's price.

    ``price`` weights by the agreed purchase price (pro-rata — the default, and how blanket
    debt is conventionally apportioned); ``equal`` gives every property the same share
    regardless of size. ``custom`` has no computed weights: the caller supplies the shares
    directly, so asking for weights is a programming error.
    """
    if method == "price":
        return [Decimal(p) for p in prices]
    if method == "equal":
        return [Decimal(1)] * len(prices)
    raise ValueError(f"no computed weights for allocation method {method!r}")
