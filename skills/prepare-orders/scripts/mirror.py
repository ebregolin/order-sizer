#!/usr/bin/env python3
#
# Copyright 2026 Enrico Bregolin
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
"""
Sizing engine — the deterministic core of order-sizer.

Every number that reaches an IBKR instruction is produced here. The agent gathers
inputs and reads results; it never computes a quantity, price or limit itself.
Same inputs, same outputs, every time, and the reasoning is inspectable months later.

Model: the user acts when the source publishes. Nothing else triggers an order.
Source and user hold the same instruments, so a mark-to-market move changes both
proportionally and the ratio is self-maintaining. There is no periodic rebalancing
to do, and attempting it only generates commissions.

The one real cost in the normal path is EXECUTION DELAY: the user approves later
than the source published, and the market has moved. That is what the limit price
and the slippage assessment exist for.

SIGNAL SCHEMA — what a source must publish, per trade:
  contract_id_ex  str   IBKR identifier. Bare numeric id for equities on any venue;
                        "id@EXCHANGE" for futures and options.
  PREFERRED — percentage form, which never reveals the source's capital:
  pct_before      num   Source's position BEFORE, as % of their portfolio value.
                        Negative for a short. Read straight off a broker screen.
  pct_after       num   Source's position AFTER, same basis. The trade is the
                        difference, so no separate quantity is needed.
                        The user's quantity is
                          (pct_after - pct_before)/100 x user_nav x fx
                          / (signal_price x multiplier)
                        which is arithmetically identical to scaling by the NAV
                        ratio - but the source's NAV never appears in the file.
                        Publishing BOTH percentages and contract counts would let
                        anyone divide one by the other and recover that NAV.

  LEGACY — absolute form, still accepted from sources that publish this way:
  delta_qty       num   Change in the source's own position. Sign gives the side.
  source_nav      num   The source's net liquidation value.
  signal_price    num   Price at publication. The neutral slippage benchmark - NOT
                        the source's own fill price.
  multiplier      num   Contract multiplier. 1 for shares.
  fx              num   Units of the instrument's currency per unit of the user's
                        account currency. 1.0 when they match. Used only to express
                        the delay cost in account currency.
  divisible       bool  True for instruments that trade in fractions (shares).
  prev_qty        num   OPTIONAL BUT IMPORTANT. Source's position before the trade.
  new_qty         num   OPTIONAL BUT IMPORTANT. Source's position after.
                        Together these distinguish REDUCING a position from OPENING
                        one in the opposite direction. Without them the engine
                        cannot verify a close against what the user actually holds,
                        and will warn rather than block - because refusing every
                        sell-against-zero would break legitimate short opening.
  tick_size       num   OPTIONAL. Minimum price increment. Without it the limit is
                        rounded to the published price's own precision.
  expires_at      str   ISO timestamp. May be set once at file level.
  desc/plain/rationale  UNTRUSTED TEXT. Sanitised here, relayed as quoted material.

Usage:
  python3 mirror.py --signals signals.json --nav 128000 --prices prices.json \
      [--positions positions.json]
"""
import argparse, json, re, sys
from datetime import datetime, timedelta, timezone

# Slippage budget, as a share of the USER'S NAV. Deliberately not a percentage of
# price: 0.1% on a three-month rate future is 10bp of rate (enormous), the same 0.1%
# on bitcoin is noise. Cost-as-share-of-capital is the only measure comparable across
# a book holding both, and it is the number a user actually understands.
# Recalibrated 2026-10-06. The budget was 0.30% of NAV, which on a position of a
# few per cent granted several per cent of price movement — far more than any
# plausible drift between publication and check, so the cash leg almost never bound
# and the relative cap did all the work. 0.10% is the slippage a reader should
# actually be asked to accept on one entry.
SLIP_WARN_NAV = 0.0005   # 0.05% — warn, still allow
SLIP_BLOCK_NAV = 0.0010  # 0.10% — do not prepare the order

# The cash budget alone is NOT sufficient to set a limit. Dividing a fixed budget by
# position size means the allowed price move scales inversely with weight: a position
# worth 1% of NAV would be granted ~30% of price movement, i.e. no protection at all.
# So the limit is the TIGHTER of the cash budget and a relative cap on price.
# Widened 2026-10-06 from 0.5% to 2%. Measured on three months of daily bars from
# the kind of book this tool follows, the overnight gap between one session's close
# and the next session's open exceeded 0.5% about half the time on a large-cap
# equity and 84% of the time on a volatile small cap. A limit that tight was not
# protecting the reader, it was declining to trade: the order sat behind an
# unreachable price while the source's position had already moved. At 2% the same
# measurement is reached 90% / 56% / 98% of the time on the three names tested.
REL_CAP = 0.02           # never allow more than 2% of price, whatever the budget
# A generous ceiling, present only to stop a pathological cell (a pasted document,
# a runaway formula) from flooding the agent's context. It is NOT a security control:
# a 300-character injection works exactly as well as a 3000-character one, so a tight
# cap bought nothing and mutilated the source's actual commentary — which is the most
# valuable thing in the file. The real defences are the prompt rule that this text is
# data rather than instructions, and the fact that IBKR requires human approval.
MAX_TEXT = 4000

# Rounding to a whole contract shifts the position by up to half a contract. Whether
# that matters depends on the contract's size against the user's capital, so the
# threshold is in percentage points OF PORTFOLIO. Three points: below that the
# difference is immaterial to any sensible allocation; above it the account is small
# relative to the instrument and the user deserves to know.
DEV_WARN_PP = 3.0

# ...but percentage points alone leave a hole at the small end. Rounding 0.5
# contracts up to 1 DOUBLES the position, and if the published weight was 2% the
# deviation is only 2pp — under the threshold, so nothing was said. Two individually
# correct decisions (ties away from zero, thresholds in pp) left the space between
# them uncovered. A relative check catches exactly what the absolute one cannot: a
# large proportional change to a small position.
DEV_WARN_REL = 50.0      # per cent of the published weight

# Instrument types for which create_order_instruction can actually build an order.
# Everything else - bonds, CFDs, funds, crypto, warrants, commodities, indices -
# and every combo whose legs are not both equity options, has to be refused HERE,
# with a sentence the reader can act on, rather than failing somewhere downstream
# inside the broker call where the cause is invisible.
ORDERABLE_CLASSES = {"STK", "FUT", "OPT", "FOP"}

# The register computes a position's value as quantity x price x multiplier. The
# broker reports the same position's value independently. If the two disagree by
# more than this factor, a multiplier or a currency in the register is wrong - the
# failure that put a London position in pence against a quote in pounds, off by a
# hundred. A genuine price move cannot produce a threefold disagreement, so this
# fires only on bad reference data. It can only be checked on an instrument the
# reader already holds: for a new position the broker has nothing to compare.
VALUE_DISAGREE_FACTOR = 3.0


def round_half_away(x):
    """Round to the nearest integer, ties away from zero.

    Python's built-in round() is half-to-even ("banker's rounding"): 2.5 -> 2 but
    3.5 -> 4. On a sizing engine that is indefensible — two ties of equal magnitude
    resolve in opposite directions, unpredictably for whoever reads the result, and
    it silently gives up exposure in half of all tie cases.
    Ties away from zero is symmetric for longs and shorts (it acts on magnitude),
    predictable, and never systematically reduces the position.
    """
    return int(x + 0.5) if x >= 0 else -int(-x + 0.5)


def clean(s):
    """Tidy text from the signal file, preserving what the source actually wrote.

    Strips control characters that could fake structure, and caps absurd lengths.
    Deliberately KEEPS line breaks: the rationale is often several paragraphs, and
    an earlier version collapsed them into one block and cut it at 300 characters —
    destroying the source's reasoning for no security benefit whatsoever.

    This is hygiene, not defence. The agent is separately instructed to treat these
    fields as quoted data, never as instructions, and no order executes without the
    account holder approving it in IBKR.
    """
    if not isinstance(s, str):
        return ""
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", s)  # keep \n and \t
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    s = s.strip()
    if len(s) > MAX_TEXT:
        s = s[:MAX_TEXT] + " […text truncated: over 4000 characters]"
    return s


def parse_ts(v):
    """Accept ISO timestamps with or without a trailing Z, with or without offset."""
    if not v or not isinstance(v, str):
        return None
    t = v.strip().replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(t)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def round_to_tick(price, tick, side, published=None):
    """Round a limit price to a tradable increment, always in the safer direction.

    IBKR rejects off-tick limits outright. When no tick is supplied, fall back to the
    decimal precision of the published price, which came from the exchange and is
    therefore on-tick. Rounding down for a buy and up for a sell can only make the
    limit stricter, never looser.
    """
    if tick and tick > 0:
        n = price / tick
        n = int(n) if side == "BUY" else -int(-n)   # floor for BUY, ceil for SELL
        return round(n * tick, 10)
    # No tick supplied: never emit MORE precision than the published price carried.
    # str(63780.0) ends in ".0", which naively reads as one decimal and produced a
    # limit of 64098.9 on an instrument whose tick is 5 — accepted by the
    # instruction, then rejected at submission, i.e. in the user's face.
    txt = repr(float(published if published is not None else price))
    dec = 0
    if "." in txt and "e" not in txt.lower():
        frac = txt.split(".")[1].rstrip("0")
        dec = min(len(frac), 8)
    f = 10 ** dec
    return (int(price * f) if side == "BUY" else -int(-price * f)) / f


def build_message(action, slip_warn, dev_warn, dev_pp, dev_rel, cost_nav, fillable,
                  note, have_quote=True):
    """Assemble the text from the causes that actually fired — and only those.

    WARN has more than one cause. An earlier version kept a single sentence written
    for the slippage case and emitted it whenever WARN fired, so a rounding warning
    on a trade whose price had moved IN THE USER'S FAVOUR announced that the market
    had moved against them and quoted the gain as a cost — abs() having erased the
    sign. The data was right and the prose was wrong, which is worse than both being
    wrong: the user reads the sentence, not the JSON.
    Rule: never state anything not derived from the field that determined the action.
    """
    parts = []
    if action == "BLOCK":
        parts.append("The market has moved beyond the allowed tolerance. No order "
                     "prepared: check with the source before proceeding.")
    else:
        if dev_warn:
            if abs(dev_rel) > DEV_WARN_REL:
                # The proportional change is the striking fact here: saying "+2.0pp"
                # about a position that has doubled would understate it badly.
                parts.append(f"Note: this instrument cannot be traded in fractions, "
                             f"and rounding to a whole contract puts the position "
                             f"{dev_rel:+.0f}% away from the published weight "
                             f"({dev_pp:+.1f} percentage points of your portfolio). "
                             f"On a small position the proportional effect is large.")
            else:
                parts.append(f"Rounding to a whole contract shifts the weight by "
                             f"{dev_pp:+.1f} percentage points of your portfolio "
                             f"against the published figure.")
        if slip_warn:
            parts.append(f"The market has moved against you since publication: the "
                         f"delay costs roughly {cost_nav*100:.2f}% of your portfolio "
                         f"value.")
        elif -cost_nav >= SLIP_WARN_NAV:
            # Same threshold as the adverse side. Without it, three cents on ORCL
            # replaced a correct, short "price in line" with a sentence quantifying
            # zero: "roughly 0.00% of your portfolio value in your favour".
            parts.append(f"The market has moved in your favour since publication: "
                         f"roughly {abs(cost_nav)*100:.2f}% of your portfolio value.")
        if not parts and have_quote:
            parts.append("Price in line with the published figure.")
    if not have_quote:
        parts.append("Your account receives no real-time quote for this instrument, "
                     "so how far the market has moved since publication cannot be "
                     "measured. The limit price is still computed from the published "
                     "price: had the market moved beyond tolerance, the order would "
                     "simply not fill.")
    if not fillable:
        parts.append("Note: the current price is beyond the protective limit, so "
                     "this order may not fill.")
    if note:
        parts.append(note)
    return " ".join(parts)


def mirror_one(t, nav, price_now, held=None, min_pct=0.75, mkt=None):
    """One published position -> one user instruction.

    price_now may be None. Some accounts have no live data subscription for some
    contracts — CME crypto and FX futures are the common case — and an earlier
    version DISCARDED those trades entirely. That was wrong. The current price is
    used only to measure how far the market has moved since publication, i.e. to
    raise a warning. What actually protects the user is the LIMIT PRICE, and that is
    derived from the source's published price, not from the current one.
    So with no quote we fall back to the published price, set the limit exactly as
    always, and say plainly that the move could not be measured. The user who pays
    to follow a source should not lose the trade because their broker withholds a
    quote — least of all when the protection is intact either way.
    """
    desc0 = clean(t.get("desc"))
    mult, fx = t.get("multiplier"), t.get("fx", 1.0)
    ref = t.get("signal_price")
    pb, pa = t.get("pct_before"), t.get("pct_after")

    mkt = mkt or {}
    # Signed net quantity of orders already working at the broker on this contract.
    # It belongs with the holding, not beside it: see the netting comment below.
    working = mkt.get("working_qty") or 0.0

    # Can the broker connector build an order for this instrument at all? Asked
    # first, because no amount of correct arithmetic helps if the answer is no.
    ac = str(mkt.get("asset_class") or t.get("asset_class") or "").upper()
    if ac and ac not in ORDERABLE_CLASSES:
        return {"action": "BLOCK", "desc": desc0, "asset_class": ac,
                "message": f"The broker connector cannot prepare orders for "
                           f"instrument type {ac}. It builds orders for shares, "
                           "futures and single-leg options only. This position has "
                           "to be traded directly in your broker's own app."}

    # Reference-data cross-check. Two independent measures of the SAME position:
    # what the register's own arithmetic says it is worth, and what the broker
    # reports. They cannot disagree threefold for any market reason.
    bval = mkt.get("market_value")
    if held and bval and ref and mult:
        implied_local = abs(held) * abs(ref) * mult        # instrument currency
        broker_local = abs(float(bval))                    # instrument currency
        if implied_local > 0 and broker_local > 0:
            r = max(implied_local / broker_local, broker_local / implied_local)
            if r > VALUE_DISAGREE_FACTOR:
                return {"action": "BLOCK", "desc": desc0,
                        "register_value": round(implied_local, 2),
                        "broker_value": round(broker_local, 2),
                        "message": f"The register and your broker disagree about what "
                                   f"this position is worth by a factor of {r:.0f} "
                                   f"({implied_local:,.0f} against {broker_local:,.0f} "
                                   "in the instrument's own currency). A multiplier or "
                                   "a currency in the source's instrument table is "
                                   "wrong, so any quantity computed from it would be "
                                   "wrong by the same factor. Nothing prepared."}

    # Is the venue trading right now? An instruction built while the venue is shut
    # carries a limit derived from a price nobody can currently trade against, and
    # the reader cannot be relied on to tick an extended-hours box by hand. The
    # scheduled check runs every hour, so the cheapest correct answer is to prepare
    # nothing now and let the run that finds the venue open do it against a live
    # price. Nothing is lost: the signal's own validity is extended across the
    # closure so a Friday evening publication is still prepared on Monday.
    if mkt.get("session_open") is False:
        return {"action": "SKIP", "desc": desc0, "session_open": False,
                "message": "The venue for this instrument is closed right now, so "
                           "nothing was prepared. The next scheduled check that finds "
                           "it open will prepare this order against a live price."}

    if pb is not None and pa is not None:
        # Percentage form. Convert the source's weight change into the notional it
        # represents for THIS user, then into contracts.
        if not ref or not mult:
            return {"action": "ERROR", "desc": desc0,
                    "message": "Incomplete signal: price or multiplier missing."}
        published_delta = pa - pb
        # Size to the published WEIGHT, measured from where this reader actually is
        # — not by replaying the source's delta blindly. A reader who missed the
        # opening leg, was half filled, or had an order rejected would otherwise
        # carry that gap for ever, and one who already acted would be handed the
        # same trade again on the next run. Their own holding is the memory this
        # tool has no other way of keeping.
        #
        # The current weight is measured at the PUBLICATION price, not the live one,
        # so that a price move between publication and check cannot by itself create
        # or cancel an order. Publications move this tool; prices never do.
        # Orders already working count as if they were filled. They are not a
        # separate fact from the holding, they are the part of it that is in
        # flight: a reader building a position one point at a time would
        # otherwise be handed a second order for a gap the first order is
        # already closing, and if both filled the position would overshoot the
        # published target. Netting them in is conservative in the only
        # direction that matters - it can under-order, never double-order.
        # Every working order counts, including ones the reader placed
        # themselves, because the broker does not say who placed them.
        if held is not None:
            current_pct = (held + working) * ref * mult / (nav * fx) * 100.0
        else:
            current_pct = pb
        gap = pa - current_pct

        if published_delta == 0:
            return {"action": "SKIP", "desc": desc0, "current_pct": round(current_pct, 2),
                    "message": "The source's weight did not change."}

        # Direction lock. The target arithmetic can point the opposite way to the
        # trade that was published — a reader sitting at zero when the source TRIMS
        # a position would be told to buy it. Never prepare an order that
        # contradicts the register: every instruction must correspond to something
        # the source actually published.
        if gap == 0 or (gap > 0) != (published_delta > 0):
            return {"action": "SKIP", "desc": desc0,
                    "current_pct": round(current_pct, 2), "target_pct": round(pa, 2),
                    "published_delta_pct": round(published_delta, 2),
                    "message": "Your holding is on the other side of this trade: the "
                               "source " + ("increased" if published_delta > 0 else "reduced")
                               + f" this position to {pa:g}% and you hold about "
                               f"{current_pct:.2f}%. Closing that gap would mean trading "
                               "against what was published, so nothing was prepared."}

        if abs(gap) < min_pct:
            return {"action": "SKIP", "desc": desc0,
                    "current_pct": round(current_pct, 2), "target_pct": round(pa, 2),
                    "gap_pct": round(gap, 2),
                    "message": f"You already hold about {current_pct:.2f}% against the "
                               f"{pa:g}% published — a gap of {abs(gap):.2f}%, below the "
                               f"{min_pct:g}% minimum. No order prepared: the commission "
                               "would cost more than the difference is worth."}

        delta_pct = gap
        raw = (delta_pct / 100.0) * nav * fx / (ref * mult)
        scale = None
    else:
        src_nav = t.get("source_nav")
        if not src_nav or t.get("delta_qty") is None:
            return {"action": "ERROR", "desc": desc0,
                    "message": "Incomplete signal: needs the before/after portfolio "
                               "percentages, or a quantity and the source's portfolio "
                               "value."}
        scale = nav / src_nav
        raw = t["delta_qty"] * scale
        delta_pct = None

    # Whole units, always. An instruction is prepared now and submitted by a human
    # later — possibly outside regular trading hours, when no venue accepts a
    # fractional order. Whether the moment of submission is inside those hours
    # cannot be known when the order is built, so a fractional quantity is a bet on
    # when the reader presses send. That bet was losing: orders reached the broker
    # with no quantity at all. The Fractionable column stays in the source's sheet —
    # it is still true about the instrument — it simply no longer sizes anything.
    qty = round_half_away(raw)

    desc = desc0
    if qty == 0:
        return {"action": "SKIP", "desc": desc, "raw_qty": round(raw, 4),
                "message": "Your portfolio is too small for this position to reach "
                           "one whole contract. No order prepared."}

    side = "BUY" if qty > 0 else "SELL"
    qty = abs(qty)

    # Position awareness. Without it, a reduction can be prepared against something
    # the user never bought — if the opening order was skipped, expired or rejected,
    # the closing order would silently open a short instead.
    # A signal REDUCING an existing position must never exceed what is actually held.
    # A signal genuinely OPENING the other way is left alone: shorting is legitimate.
    note = None
    # Reduce-vs-open works identically on percentages or on contract counts: only
    # the signs and relative magnitudes matter.
    prev = pb if pb is not None else t.get("prev_qty")
    new = pa if pa is not None else t.get("new_qty")
    known_intent = prev is not None and new is not None
    reducing = (known_intent and abs(new) < abs(prev)
                and (new == 0 or new * prev > 0))

    # When the source omits prev_qty/new_qty we cannot tell a close from a new
    # short. Do NOT block: opening a short is legitimate and blocking it would
    # break the tool for any source that trades both ways. Warn instead, loudly.
    if (not known_intent and held is not None and abs(held) < 1e-9
            and (raw < 0) and not t.get("divisible")):
        note = ("The source does not say whether it is closing or opening. You do "
                "not hold this instrument: if this were a close, the order would "
                "open a short position instead. Check before approving.")

    if reducing and held is not None:
        have = abs(held)
        if have < 1e-9:
            return {"action": "BLOCK", "desc": desc, "side": side, "quantity": qty,
                    "message": "The source is reducing a position you do not hold. "
                               "The opening trade was most likely never executed: no "
                               "order prepared, so as not to open the opposite one."}
        if have < qty:
            note = (f"Reduced from {qty:g} to {have:g}: that is what you actually hold.")
            qty = int(have)

    if not ref:
        return {"action": "ERROR", "desc": desc,
                "message": "Incomplete signal: the publication price is missing."}

    # Slippage measured against the PUBLICATION price, never the source's own fill.
    # Neutral benchmark: it does not matter who executed first.
    have_quote = price_now is not None
    if not have_quote:
        price_now = ref            # no measurable move; the limit still protects
    move = price_now - ref
    adverse = move if side == "BUY" else -move
    cost = adverse * qty * mult / fx
    cost_nav = cost / nav

    slip_warn = cost_nav >= SLIP_WARN_NAV      # adverse only: cost_nav < 0 is a gain
    action = ("BLOCK" if cost_nav >= SLIP_BLOCK_NAV else
              "WARN" if slip_warn else "OK")

    budget_tol = SLIP_BLOCK_NAV * nav * fx / (qty * mult)
    tol = min(budget_tol, REL_CAP * abs(ref))
    # Floor at one tick. On an instrument whose notional dwarfs capital - a rate
    # future published at several hundred per cent of portfolio - the cash budget
    # divided by that weight comes out below the instrument's own minimum price
    # increment. Rounding then collapses the limit onto the published price and the
    # order can only fill if the market has not moved at all, which is no limit at
    # all. The arithmetic is right (on twenty times notional you genuinely cannot
    # afford slippage) but a sub-tick limit is an unfillable order, not a tight one.
    _tick = t.get("tick_size")
    try:
        _tick = abs(float(_tick)) if _tick else None
    except (TypeError, ValueError):
        _tick = None
    tol_floored = bool(_tick and tol < _tick)
    if tol_floored:
        tol = _tick
    limit = round_to_tick(ref + tol if side == "BUY" else ref - tol,
                          t.get("tick_size"), side, ref)

    # The cash budget and the relative cap can disagree: a small position with the
    # market already past the cap scores a low cash cost (action OK) while sitting
    # behind an unreachable limit. Saying "price in line" then would be false - the
    # user waits for a fill that cannot come. Detect and say so.
    fillable = limit >= price_now if side == "BUY" else limit <= price_now
    if not fillable and action == "OK":
        action = "WARN"

    # Rounding deviation, measured in PERCENTAGE POINTS OF THE PORTFOLIO — not as a
    # percentage of the order. Those are very different things: taking 2.77% where
    # 2.50% was published is 0.27pp of portfolio, which moves nothing, yet reads as
    # "10% too much" against the order and sounds like a risk event. The denominator
    # that matters is the user's capital, not the trade.
    unit_value = ref * mult / fx                    # one contract, in account currency
    dev_pp = (qty - abs(raw)) * unit_value / nav * 100
    # Carve-out: for rate futures the published weight runs into the hundreds or
    # thousands of percent, because notional dwarfs capital. A large pp deviation
    # there is not a large risk deviation, and warning on it would fire constantly
    # until nobody read the warnings at all.
    weight_scale = max(abs(pa), abs(pb)) if (pb is not None and pa is not None) else 0
    dev_rel = (qty - abs(raw)) / abs(raw) * 100 if raw else 0.0
    dev_warn = ((abs(dev_pp) > DEV_WARN_PP and weight_scale <= 100)
                or abs(dev_rel) > DEV_WARN_REL)
    if dev_warn and action == "OK":
        action = "WARN"

    # Time in force. DAY by default: the order dies at the close of the session it
    # was prepared in, so nothing this tool prepares can still be working tomorrow
    # against a signal the source has since moved on from. That bound matters more
    # than usual because the broker connector has NO CANCEL - nothing here can
    # retract an order once the reader has submitted it.
    #
    # The one exception is an instrument whose regular session is shut while an
    # extended session is demonstrably running for it right now. A DAY order there
    # can expire with tonight's extended session without ever reaching the regular
    # one, which is the opposite of what the reader wants. OND - overnight, carrying
    # into the next trading day - is the right setting, and its life is still
    # bounded at about two sessions rather than open-ended.
    #
    # OND is sent ONLY on positive evidence that an extended session is running. It
    # is deliberately not sent from a list of venues or asset classes: tested on
    # 6 October 2026, create_order_instruction ACCEPTED OND on an LSE stock, which
    # has no overnight session at all - it stored tif "OND" with no rejection and no
    # warning. So a wrong guess is not caught anywhere, and "try OND, fall back to
    # DAY" cannot work because nothing rejects. Evidence, or DAY.
    tif = "DAY"
    tif_note = None
    if mkt.get("rth_open") is False and mkt.get("extended_session") is True:
        tif = "OND"
        tif_note = ("This instrument's regular session is closed, but it is trading "
                    "in an extended session right now. The order is set to carry "
                    "into the next regular session rather than expire with tonight's, "
                    "so it does not have to be prepared again. Depending on which "
                    "extended session your broker routes it to, the app may also "
                    "offer an 'outside regular trading hours' option - this tool "
                    "cannot set it. You do not have to do anything: if it is not "
                    "enabled the order simply waits for the regular session to open.")
    elif mkt.get("rth_open") is False:
        tif_note = ("This instrument's regular session is closed. The order is "
                    "prepared to work when it opens.")

    return {
        "action": action,
        "contract_id_ex": t["contract_id_ex"],
        "desc": desc,
        "plain": clean(t.get("plain")),
        "rationale": clean(t.get("rationale")),
        "side": side,
        "quantity": qty,
        "order_type": "LIMIT",
        "limit_price": limit,
        "time_in_force": tif,
        "time_in_force_note": tif_note,
        "rth_open": mkt.get("rth_open"),
        "extended_session": mkt.get("extended_session"),
        "scale": round(scale, 4) if scale else None,
        "delta_pct_portafoglio": round(delta_pct, 3) if delta_pct is not None else None,
        "published_delta_pct": round(pa - pb, 3) if (pa is not None and pb is not None) else None,
        "current_pct": round(current_pct, 2) if pb is not None and pa is not None else None,
        "target_pct": round(pa, 2) if pa is not None else None,
        "raw_qty": round(raw, 3),
        "rounding_dev_pp": round(dev_pp, 2),        # percentage points of portfolio
        "rounding_dev_rel_pct": round(dev_rel, 2),  # % of the published position
        "rounding_dev_pct_ordine": round((qty - abs(raw)) / abs(raw) * 100, 2) if raw else 0.0,
        "signal_price": ref,
        "price_now": price_now,
        "price_source": "current quote" if have_quote else "published price "
                        "(no quote available on this account)",
        "slippage_measured": have_quote,
        "fillable_now": fillable,
        "tolerance_used": round(tol, 8),
        "tolerance_floored_at_tick": tol_floored,
        "working_qty_netted": working or 0,
        "session_open": mkt.get("session_open"),
        "tolerance_source": ("one tick (floor)" if tol_floored else
                             "budget 0.10% of NAV" if budget_tol <= REL_CAP * abs(ref)
                             else "cap 2% of price"),
        "held_before": held,
        "adjustment_note": note,
        "delay_cost_account_ccy": round(cost, 2),
        "delay_cost_nav_pct": round(cost_nav * 100, 3),
        "message": build_message(action, slip_warn, dev_warn, dev_pp, dev_rel,
                                 cost_nav, fillable, note, have_quote),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--signals", required=True)
    ap.add_argument("--nav", required=True, type=float)
    ap.add_argument("--prices", required=True,
                    help="JSON mapping contract_id_ex -> current price")
    ap.add_argument("--min-pct", type=float, default=0.75,
                    help="Smallest gap, in percentage points of the portfolio, worth "
                         "an order. Below it nothing is prepared and the reason is "
                         "reported. Default 0.75.")
    ap.add_argument("--positions", default=None,
                    help="JSON mapping contract_id_ex -> quantity currently held. "
                         "Strongly recommended: without it, reductions cannot be "
                         "checked against what you actually own.")
    ap.add_argument("--market", default=None,
                    help="JSON mapping contract_id_ex -> object with any of: "
                         "session_open (bool, false when the venue is shut), "
                         "asset_class (STK/FUT/OPT/FOP/...), market_value (the "
                         "broker's own valuation of the holding, in the instrument's "
                         "currency), working_qty (signed net quantity of orders "
                         "already working), working_age_hours. Every field is "
                         "optional and every check it feeds is skipped when absent, "
                         "so an older caller keeps working unchanged.")
    ap.add_argument("--closure-grace-hours", type=float, default=96.0,
                    help="How far past its stated expiry a signal may still be acted "
                         "on when the venue has had no trading session since it was "
                         "published. Covers a weekend plus holidays. Default 96.")
    ap.add_argument("--available-funds", type=float, default=None,
                    help="Funds the broker reports as available, in the account's "
                         "currency. When given, buy orders that together exceed it "
                         "are not prepared.")
    a = ap.parse_args()

    if a.nav <= 0:
        sys.exit("Invalid portfolio value.")

    try:
        sig = json.load(open(a.signals))
        prices = json.load(open(a.prices))
        held_map = json.load(open(a.positions)) if a.positions else None
        mkt_map = json.load(open(a.market)) if a.market else {}
    except (OSError, ValueError) as e:
        sys.exit(f"File could not be read: {e}")

    if not isinstance(sig, dict) or not isinstance(sig.get("trades"), list):
        sys.exit("Signal format not recognised.")

    now = datetime.now(timezone.utc)
    results, expired, bad = [], [], []
    # Kept so the total notional of the prepared set can be stated back in the
    # account's own currency: a reader about to approve five orders should be told
    # what they cost against the funds the broker says are available.
    sig_mult, sig_fx = {}, {}

    for t in sig["trades"]:
        if not isinstance(t, dict):
            bad.append("invalid entry"); continue
        t.setdefault("source_nav", sig.get("source_nav"))
        exp = parse_ts(t.get("expires_at") or sig.get("expires_at"))
        if exp is None and (t.get("expires_at") or sig.get("expires_at")):
            bad.append(f"{clean(t.get('desc'))}: expiry unreadable — discarded")
            continue
        cid = t.get("contract_id_ex")
        if not cid:
            bad.append(f"{clean(t.get('desc'))}: identifier missing — discarded")
            continue
        _mk = mkt_map.get(cid) if isinstance(mkt_map, dict) else None
        if not isinstance(_mk, dict):
            _mk = {}
        extended = False
        if exp and exp < now:
            # The validity the source writes against a trade is wall-clock hours, and
            # wall-clock hours run through nights and weekends when nobody could have
            # traded. A position published late on a Friday with a day's validity was
            # dead before the venue next opened: the reader was never once given the
            # chance the source intended to give them. So when the venue has not
            # completed a single session since publication, the signal survives its
            # stated expiry - up to a bounded grace that covers a weekend plus
            # holidays, never indefinitely. It can only ever lengthen a window, never
            # shorten one, so a publication inside a normal trading week behaves
            # exactly as before. If the caller does not say how many sessions have
            # passed, the old rule applies unchanged and the signal expires.
            sessions = _mk.get("sessions_since_publication")
            if sessions == 0 and (now - exp) <= timedelta(hours=a.closure_grace_hours):
                extended = True
            else:
                expired.append({"desc": clean(t.get("desc")),
                                "expired_at": exp.isoformat(timespec="seconds"),
                                "sessions_since_publication": sessions})
                continue
        # A MISSING QUOTE IS NOT A REASON TO DROP THE TRADE. mirror_one handles
        # price_now=None deliberately (see its docstring). An earlier version
        # filtered here first, which made that whole branch unreachable: the
        # feature was documented in three places and never once executed.
        # Accept both an absent key and an explicit null.
        raw_px = prices.get(cid)
        try:
            price_now = float(raw_px) if raw_px is not None else None
        except (TypeError, ValueError):
            price_now = None
        try:
            # IBKR returns no row for instruments you do not hold, so the agent
            # naturally writes {}. Treating a missing key as "unknown" left the
            # guard inert in exactly the case it exists for: absent means ZERO.
            held = held_map.get(cid, 0) if held_map is not None else None
            sig_mult[cid] = t.get("multiplier") or 1
            sig_fx[cid] = t.get("fx") or 1
            r = mirror_one(t, a.nav, price_now, held, a.min_pct, _mk)
            if extended:
                r["expiry_extended_over_closure"] = True
            results.append(r)
        except (KeyError, TypeError, ValueError) as e:
            bad.append(f"{clean(t.get('desc'))}: incomplete data ({e}) — discarded")

    def _notional(r):
        """One prepared order's cost in the ACCOUNT's currency."""
        return (r["quantity"] * abs(r.get("signal_price") or 0)
                * (sig_mult.get(r.get("contract_id_ex")) or 1)
                / (sig_fx.get(r.get("contract_id_ex")) or 1))

    # Buying power. The broker will happily let a margin account buy several times
    # its own net worth, so a reader who approves every order in a run without
    # looking can end up levered without ever deciding to be. Only BUY orders are
    # counted: a sale of something already held releases cash rather than consuming
    # it. When the prepared buys exceed what the broker says is available, orders
    # are dropped LARGEST FIRST - that funds the greatest number of the remaining
    # instructions - until the rest fit.
    unfunded = []
    if a.available_funds is not None:
        buys = [r for r in results
                if r["action"] in ("OK", "WARN") and r.get("side") == "BUY"]
        total = sum(_notional(r) for r in buys)
        for r in sorted(buys, key=_notional, reverse=True):
            if total <= a.available_funds:
                break
            cost = _notional(r)
            total -= cost
            r["action"] = "BLOCK"
            r["message"] = (
                f"Not prepared: your broker reports {a.available_funds:,.0f} "
                f"available and this order alone costs {cost:,.0f} in your account's "
                "currency. The buy orders in this run together exceed the funds "
                "available, so the largest were dropped until the rest could be "
                "funded. Nothing here was sized on margin you had not already "
                "decided to use.")
            unfunded.append({"desc": r.get("desc"), "cost": round(cost, 2)})

    # Orders already working at the broker, echoed back so the reader can see them.
    # The broker connector has no cancel, so a working order whose signal has since
    # expired can only be reported, never cleaned up from here - and it is reported
    # at the top of the run for exactly that reason.
    working_orders = []
    if isinstance(mkt_map, dict):
        for cid, mk in mkt_map.items():
            if isinstance(mk, dict) and mk.get("working_qty"):
                working_orders.append({
                    "contract_id_ex": cid,
                    "working_qty": mk.get("working_qty"),
                    "age_hours": mk.get("working_age_hours"),
                    "desc": mk.get("desc"),
                })

    json.dump({
        "generated_at": now.isoformat(timespec="seconds"),
        "user_nav": a.nav,
        "positions_checked": held_map is not None,
        "signal_published_at": sig.get("published_at"),
        "min_pct": a.min_pct,
        "orders_notional_account_ccy": round(sum(
            r["quantity"] * r["signal_price"] * (sig_mult.get(r["contract_id_ex"]) or 1)
            / (sig_fx.get(r["contract_id_ex"]) or 1)
            for r in results if r["action"] in ("OK", "WARN")), 2),
        "available_funds_account_ccy": a.available_funds,
        "not_prepared_for_lack_of_funds": unfunded,
        "working_orders_at_broker": working_orders,
        "all_skipped": bool(results) and all(r["action"] == "SKIP" for r in results),
        "to_create": [r for r in results if r["action"] in ("OK", "WARN")],
        "blocked": [r for r in results if r["action"] in ("BLOCK", "ERROR")],
        "skipped": [r for r in results if r["action"] == "SKIP"],
        "expired": expired,
        "unusable": bad,
    }, sys.stdout, indent=2, ensure_ascii=False)
    print()


if __name__ == "__main__":
    main()
