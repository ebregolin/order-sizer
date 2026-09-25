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
from datetime import datetime, timezone

# Slippage budget, as a share of the USER'S NAV. Deliberately not a percentage of
# price: 0.1% on a three-month rate future is 10bp of rate (enormous), the same 0.1%
# on bitcoin is noise. Cost-as-share-of-capital is the only measure comparable across
# a book holding both, and it is the number a user actually understands.
SLIP_WARN_NAV = 0.0015   # 0.15% — warn, still allow
SLIP_BLOCK_NAV = 0.0030  # 0.30% — do not prepare the order

# The cash budget alone is NOT sufficient to set a limit. Dividing a fixed budget by
# position size means the allowed price move scales inversely with weight: a position
# worth 1% of NAV would be granted ~30% of price movement, i.e. no protection at all.
# So the limit is the TIGHTER of the cash budget and a relative cap on price.
REL_CAP = 0.005          # never allow more than 0.5% of price, whatever the budget
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


def mirror_one(t, nav, price_now, held=None):
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

    if pb is not None and pa is not None:
        # Percentage form. Convert the source's weight change into the notional it
        # represents for THIS user, then into contracts.
        if not ref or not mult:
            return {"action": "ERROR", "desc": desc0,
                    "message": "Incomplete signal: price or multiplier missing."}
        delta_pct = pa - pb
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

    qty = round(raw, 4) if t.get("divisible") else round_half_away(raw)

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
            qty = round(have, 4) if t.get("divisible") else int(have)

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
        "time_in_force": "DAY",
        "scale": round(scale, 4) if scale else None,
        "delta_pct_portafoglio": round(delta_pct, 3) if delta_pct is not None else None,
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
        "tolerance_source": "budget" if budget_tol <= REL_CAP * abs(ref) else "cap 0.5%",
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
    ap.add_argument("--positions", default=None,
                    help="JSON mapping contract_id_ex -> quantity currently held. "
                         "Strongly recommended: without it, reductions cannot be "
                         "checked against what you actually own.")
    a = ap.parse_args()

    if a.nav <= 0:
        sys.exit("Invalid portfolio value.")

    try:
        sig = json.load(open(a.signals))
        prices = json.load(open(a.prices))
        held_map = json.load(open(a.positions)) if a.positions else None
    except (OSError, ValueError) as e:
        sys.exit(f"File could not be read: {e}")

    if not isinstance(sig, dict) or not isinstance(sig.get("trades"), list):
        sys.exit("Signal format not recognised.")

    now = datetime.now(timezone.utc)
    results, expired, bad = [], [], []

    for t in sig["trades"]:
        if not isinstance(t, dict):
            bad.append("invalid entry"); continue
        t.setdefault("source_nav", sig.get("source_nav"))
        exp = parse_ts(t.get("expires_at") or sig.get("expires_at"))
        if exp is None and (t.get("expires_at") or sig.get("expires_at")):
            bad.append(f"{clean(t.get('desc'))}: expiry unreadable — discarded")
            continue
        if exp and exp < now:
            expired.append({"desc": clean(t.get("desc")),
                            "expired_at": exp.isoformat(timespec="seconds")})
            continue
        cid = t.get("contract_id_ex")
        if not cid:
            bad.append(f"{clean(t.get('desc'))}: identifier missing — discarded")
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
            results.append(mirror_one(t, a.nav, price_now, held))
        except (KeyError, TypeError, ValueError) as e:
            bad.append(f"{clean(t.get('desc'))}: incomplete data ({e}) — discarded")

    json.dump({
        "generated_at": now.isoformat(timespec="seconds"),
        "user_nav": a.nav,
        "positions_checked": held_map is not None,
        "signal_published_at": sig.get("published_at"),
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
