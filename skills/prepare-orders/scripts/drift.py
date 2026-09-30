#!/usr/bin/env python3
"""
Drift report — the reader's own book against the one the source published.

Why this exists. Every instruction this tool prepares comes from a published row,
and nothing is ever prepared without one. That is the right rule, and it has a
consequence: when a reader misses a leg — asleep, partly filled, order rejected,
broker refused the instrument — nothing corrects it until the source happens to
trade that name again. Until then the reader believes they are tracking the source
and they are not. This is the only thing in the tool that tells them otherwise.

It PREPARES NOTHING. It compares two sets of percentages and prints the difference.
No order, no suggestion, no "you should". The reader decides what, if anything, to
do — the same discipline as everywhere else here.

The source's percentages come from the Positions sheet of its own workbook and are
only as fresh as the last time the source refreshed it; the timestamp is printed so
that staleness is visible rather than assumed.

Usage:
  python3 drift.py --signals signals.json --positions positions.json \
      --prices prices.json --nav 123456 [--min-pct 0.75]
"""
import argparse, json, sys


def weight(qty, price, mult, fx, nav):
    """A holding as a percentage of the reader's capital, in their own currency."""
    if not price or not nav:
        return None
    return qty * price * (mult or 1) / (fx or 1) / nav * 100.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--signals", required=True, help="read_sheet.py output")
    ap.add_argument("--positions", required=True, help="contract_id_ex -> quantity held")
    ap.add_argument("--prices", required=True, help="contract_id_ex -> current price")
    ap.add_argument("--nav", required=True, type=float)
    ap.add_argument("--min-pct", type=float, default=0.75,
                    help="Differences smaller than this are not worth a line. "
                         "Default 0.75, the same threshold that governs orders.")
    a = ap.parse_args()

    try:
        sig = json.load(open(a.signals))
        held = json.load(open(a.positions))
        prices = json.load(open(a.prices))
    except (OSError, ValueError) as e:
        sys.exit(f"File could not be read: {e}")

    if a.nav <= 0:
        sys.exit("Invalid portfolio value.")

    book = sig.get("source_positions") or []
    if not book:
        json.dump({"comparable": False,
                   "reason": sig.get("source_positions_note")
                             or "The source published no book to compare against."},
                  sys.stdout, indent=2, ensure_ascii=False)
        print(); return

    fx_rates = sig.get("fx_rates") or {}
    missing_price, unmatched, lines = [], [], []
    seen = set()

    for p in book:
        cid = p.get("contract_id_ex")
        src = p.get("pct_nav")
        if not cid:
            # Cash, or a line whose instrument is not in the Instruments sheet.
            unmatched.append({"instrument": p.get("instrument"), "source_pct": src})
            continue
        seen.add(cid)
        px = prices.get(cid)
        mine = weight(float(held.get(cid, 0) or 0), px, p.get("multiplier"),
                      fx_rates.get(p.get("currency"), 1.0), a.nav)
        if mine is None:
            missing_price.append(p.get("instrument"))
            continue
        gap = (src or 0) - mine
        if abs(gap) >= a.min_pct:
            lines.append({"instrument": p.get("instrument"),
                          "asset_class": p.get("asset_class"),
                          "source_pct": round(src, 2),
                          "your_pct": round(mine, 2),
                          "gap_pct": round(gap, 2),
                          "state": "not held" if abs(mine) < 0.01 else
                                   ("below the source" if gap > 0 else "above the source")})

    # Held by the reader, absent from the source's book. Worth naming: it is the
    # other half of "am I tracking this source", and it is the half a reader is
    # least likely to notice.
    extra = []
    for cid, q in held.items():
        if cid in seen or not q:
            continue
        px = prices.get(cid)
        mine = weight(float(q or 0), px, 1, 1.0, a.nav)
        if mine is not None and abs(mine) >= a.min_pct:
            extra.append({"contract_id_ex": cid, "your_pct": round(mine, 2)})

    lines.sort(key=lambda x: -abs(x["gap_pct"]))
    json.dump({
        "comparable": True,
        "source_book_as_of": sig.get("source_file"),
        "min_pct": a.min_pct,
        "positions_in_source_book": len(book),
        "matched": len(seen),
        "differences": lines,
        "held_but_not_in_source_book": extra,
        "no_price_available": missing_price,
        "not_matched_to_an_instrument": unmatched,
    }, sys.stdout, indent=2, ensure_ascii=False)
    print()


if __name__ == "__main__":
    main()
