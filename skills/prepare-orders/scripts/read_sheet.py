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
Spreadsheet reader — turns the source's Excel/CSV into the signal JSON the engine eats.

Exists so the language model never transcribes a number. The agent points this at the
file; this reads the cells and hands structured data to mirror.py. Nothing in the
chain from the published file to the prepared instruction passes through a model.

Reads .xlsx with the standard library only (it is a zip of XML), so the plugin keeps
zero dependencies and stays inspectable. Also accepts .csv.

Expected workbook. Sheet names are accepted in English or Italian, since a source may
publish the same file in either. Only the two sheets
below are looked for; any others (a positions snapshot, an instructions page) are
ignored. What carries meaning is the COLUMN POSITION: header text is never read, so
translating headers changes nothing and reordering columns breaks everything.

  Sheet "Trades" (or "Operazioni") — header row 8, data from row 9:
    Execution date/time | Publication date/time | Instrument | Action |
    % ptf before | % ptf after | Execution price | Publication price |
    Horizon | Validity (hours) | Rationale | Amends ID
  The sheet may carry both execution and publication timestamps and prices. Only the
  PUBLICATION pair concerns the reader: expiry runs from when the figure was
  published, and the published price is the neutral benchmark. The execution figures
  are the source's own record.
  Weights are expressed as a percentage of a portfolio, negative for shorts, exactly
  as a broker screen shows them. The change is the difference between the two, so no
  quantity column is needed — and no portfolio value is disclosed.
  Sheet "Instruments" (or "Strumenti") — header row 4, data from row 6:
    Instrument | Broker identifier | Multiplier | Currency | Fractionable |
    Tick | Note | ISIN | Ticker | Market

Usage:
  python3 read_sheet.py --file book.xlsx [--fx USD=1.1543]
"""
import argparse, csv, json, os, re, sys, zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
RELNS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def _col(ref):
    m = re.match(r"([A-Z]+)", ref)
    n = 0
    for ch in m.group(1):
        n = n * 26 + (ord(ch) - 64)
    return n - 1


# Excel builtin number-format ids. 9/10 are percentages; 14-22 and 45-47 are
# dates/times. Anything custom is decided by looking at the format string.
PCT_BUILTIN = {9, 10}
DATE_BUILTIN = set(range(14, 23)) | {27, 30, 36, 45, 46, 47, 50, 57}


def read_styles(z):
    """Map cell-style index -> ('pct' | 'date' | None).

    Without this a cell formatted as a percentage reads as 0.025 where the user
    sees 2.50%, and a date reads as the raw serial number. Both are silent
    hundred-fold or nonsense errors that look entirely plausible downstream.
    """
    kinds = {}
    if "xl/styles.xml" not in z.namelist():
        return kinds
    root = ET.fromstring(z.read("xl/styles.xml"))
    custom = {}
    for nf in root.iter(NS + "numFmt"):
        code = (nf.get("formatCode") or "")
        fid = int(nf.get("numFmtId"))
        low = code.lower()
        if "%" in code:
            custom[fid] = "pct"
        elif any(t in low for t in ("yy", "mm-dd", "dd/", "d/m", "h:mm")) and "0.00" not in code:
            custom[fid] = "date"
    xfs = root.find(NS + "cellXfs")
    if xfs is None:
        return kinds
    for i, xf in enumerate(xfs.findall(NS + "xf")):
        fid = int(xf.get("numFmtId", 0))
        if fid in PCT_BUILTIN or custom.get(fid) == "pct":
            kinds[i] = "pct"
        elif fid in DATE_BUILTIN or custom.get(fid) == "date":
            kinds[i] = "date"
    return kinds


def excel_serial_to_iso(v):
    """Excel stores dates as days since 1899-12-30 (its leap-year bug included)."""
    from datetime import date
    days = int(v)
    frac = float(v) - days
    base = datetime(1899, 12, 30, tzinfo=timezone.utc) + timedelta(days=days)
    return (base + timedelta(seconds=round(frac * 86400))).isoformat(timespec="seconds")


def read_xlsx(path):
    """Return {sheet_name: [[cell, ...], ...]} using only the standard library."""
    out = {}
    with zipfile.ZipFile(path) as z:
        styles = read_styles(z)
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")):
                shared.append("".join(t.text or "" for t in si.iter(NS + "t")))
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = {r.get("Id"): r.get("Target") for r in
                ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
        for sh in wb.iter(NS + "sheet"):
            # Relationship targets appear both as "worksheets/sheet1.xml" (relative
            # to xl/) and as "/xl/worksheets/sheet1.xml" (absolute). Normalise both.
            target = (rels.get(sh.get(RELNS + "id")) or "").lstrip("/")
            if not target.startswith("xl/"):
                target = "xl/" + target
            if target not in z.namelist():
                continue
            rows = []
            for row in ET.fromstring(z.read(target)).iter(NS + "row"):
                rownum = int(row.get("r", len(rows) + 1))
                cells = [rownum]          # element 0 carries the true row number
                for c in row.iter(NS + "c"):
                    idx = _col(c.get("r", "A1")) + 1   # +1 for the row-number slot
                    while len(cells) < idx:
                        cells.append(None)
                    v = c.find(NS + "v")
                    isel = c.find(NS + "is")
                    if c.get("t") == "s" and v is not None:
                        val = shared[int(v.text)]
                    elif isel is not None:
                        val = "".join(t.text or "" for t in isel.iter(NS + "t"))
                    elif v is not None and v.text is not None:
                        try:
                            val = float(v.text)
                            kind = styles.get(int(c.get("s", -1)))
                            if kind == "pct":
                                # The user typed 2.5 and Excel stored 0.025.
                                val = val * 100
                            elif kind == "date":
                                val = excel_serial_to_iso(val)
                            elif val == int(val):
                                val = int(val)
                        except ValueError:
                            val = v.text
                    else:
                        val = None
                    cells.append(val)
                rows.append(cells)
            out[sh.get("name")] = rows
    return out


def get(row, i):
    """Column i, 0-based. Element 0 of each row holds the sheet's row number."""
    j = i + 1
    v = row[j] if j < len(row) else None
    return v.strip() if isinstance(v, str) else v


def rownum(row, fallback):
    return row[0] if row and isinstance(row[0], int) else fallback


def key(name):
    """Match instrument names forgivingly: case, spacing and apostrophe style.

    A curly apostrophe from autocorrect must not silently orphan a trade.
    """
    t = str(name).strip().lower()
    for ch in ("\u2019", "\u02bc", "\u00b4", "`"):
        t = t.replace(ch, "'")
    return " ".join(t.split())


TRADES_SHEET = ("operazioni", "trades")
INSTR_SHEET = ("strumenti", "instruments")


def pick(sheets, names):
    """Find a sheet by any of its accepted names, ignoring case.

    The source may publish in Italian or English — the same book, translated for a
    different set of readers. Column POSITIONS carry the meaning here; the header
    text is never read, so a translated file is identical to the engine except for
    the two sheet names. Matching both costs nothing and means one plugin serves
    both audiences, rather than a fork that drifts.
    """
    for n in names:
        for actual in sheets:
            if str(actual).strip().lower() == n:
                return sheets[actual], actual
    return None, None


def num(v, field, where):
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", "."))
    except ValueError:
        raise ValueError(f"{where}: '{field}' is not a number ({v!r})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    ap.add_argument("--audience", choices=["pro", "retail"], default="pro",
                    help="Accepted for backward compatibility and ignored: every "
                         "reader receives every published trade.")
    ap.add_argument("--fx", action="append", default=[], metavar="CCY=RATE")
    ap.add_argument("--out", default="-")
    a = ap.parse_args()

    fx = {}
    for item in a.fx:
        k, _, v = item.partition("=")
        try:
            fx[k.strip().upper()] = float(v)
        except ValueError:
            sys.exit(f"malformed --fx: {item}")

    if not os.path.exists(a.file):
        sys.exit(f"File not found: {a.file}")

    if a.file.lower().endswith(".csv"):
        with open(a.file, newline="", encoding="utf-8-sig") as fh:
            rows = [[i + 1] + row for i, row in enumerate(csv.reader(fh))]
        sheets = {"Operazioni": rows, "Strumenti": []}
        op_first, st_first = 2, 2
    else:
        sheets = read_xlsx(a.file)
        # Select by the sheet's OWN row number, never by list position: xlsx omits
        # empty rows entirely, so a blank line anywhere above the data silently
        # shifts every slice and drops real records.
        op_first, st_first = 9, 6

    rows_trades, _ = pick(sheets, TRADES_SHEET)
    if rows_trades is None:
        sys.exit("Missing sheet 'Trades' (or 'Operazioni'). Sheets found: "
                 + ", ".join(map(str, sheets)))

    # A missing instrument sheet is not a missing instrument: it makes EVERY trade
    # fail with "not in the Instruments sheet", which reads like twenty separate data
    # errors instead of one structural one. Name the real cause here.
    rows_instr, _ = pick(sheets, INSTR_SHEET)
    if rows_instr is None:
        sys.exit("Missing sheet 'Instruments' (or 'Strumenti'). Sheets found: "
                 + ", ".join(map(str, sheets)))

    # ---- instrument registry -------------------------------------------------
    reg = {}
    for r in [x for x in rows_instr if rownum(x, 0) >= st_first]:
        name = get(r, 0)
        if not name:
            continue
        reg[key(name)] = {
            "contract_id_ex": str(get(r, 1) or "").strip(),
            "multiplier": num(get(r, 2), "Moltiplicatore", name),
            "currency": (str(get(r, 3) or "EUR")).strip().upper(),
            "divisible": str(get(r, 4) or "").strip().upper() in ("SI", "SÌ", "YES", "Y"),
            "tick_size": num(get(r, 5), "Tick", name),
            "publico": str(get(r, 6) or "").strip().upper(),
            # Carried through for the record the source has to keep, not used in
            # any calculation.
            "isin": str(get(r, 7) or "").strip().upper(),
            "ticker": str(get(r, 8) or "").strip(),
            "mercato": str(get(r, 9) or "").strip(),
        }

    trades, problems, withheld = [], [], []
    longest = 24.0

    rows_op = [x for x in rows_trades if rownum(x, 0) >= op_first]
    for n0, r in enumerate(rows_op, start=op_first):
        if not any(get(r, i) not in (None, "") for i in range(0, 8)):
            continue
        # The template ships one filled example row, flagged in the column beside
        # the table. It must never be published as a real trade.
        # The marker sits in the first column to the right of the table. Scan a
        # couple of columns rather than one fixed index, so adding a column to the
        # template cannot quietly turn the example into a live trade.
        if any(w in str(get(r, i) or "").upper()
               for i in range(12, 22) for w in ("ESEMPIO", "EXAMPLE")):
            continue
        where = f"row {rownum(r, n0)}"
        name = get(r, 2)
        if not name:
            problems.append(f"{where}: instrument missing"); continue
        info = reg.get(key(name))
        if not info:
            problems.append(f"{where}: '{name}' is not in the Instruments sheet"); continue
        if not info["contract_id_ex"]:
            problems.append(f"{where}: '{name}' has no broker identifier"); continue
        if not info["multiplier"]:
            problems.append(f"{where}: '{name}' has no multiplier"); continue

        # The tick fallback limits the damage but does not remove it: on an
        # instrument with tick 5 it still produced 64098, which is off-tick and
        # would be refused at submission. Treat Tick as required in practice for
        # anything not fractionable, and say so rather than discovering it later.
        if not info["divisible"] and not info["tick_size"]:
            problems.append(f"{where}: '{name}' is not fractionable and has no Tick "
                            f"in the Instruments sheet — the limit price may not be "
                            f"tradable and the order would be rejected on submission. "
                            f"Fill in the Tick column.")

        # No filtering by reader type, deliberately. If an account cannot hold an
        # instrument the broker rejects the order at entry: visible and harmless.
        # Withholding would mean a reader silently never learns a trade existed, and
        # would make the source responsible for deciding who may trade what. The
        # "Pubblico" column in the sheet is the source's own note, nothing more.

        side = str(get(r, 3) or "").strip().upper()
        if side not in ("COMPRA", "VENDI", "BUY", "SELL"):
            problems.append(f"{where}: Action must be BUY or SELL ({side!r})")
            continue

        try:
            pb = num(get(r, 4), "% ptf before", where)
            pa = num(get(r, 5), "% ptf after", where)
            # Column 6 is the source's own execution price: part of the record,
            # never the reader's benchmark. Column 7 is the price at
            # dissemination, which is what they are measured against.
            price = num(get(r, 7), "Publication price", where)
            hours = num(get(r, 9), "Validity", where) or 24.0
        except ValueError as e:
            problems.append(str(e)); continue

        if pb is None or pa is None or not price:
            problems.append(f"{where}: '% ptf before', '% ptf after' and "
                            f"'Publication price' are required")
            continue

        # The stated direction must agree with the change in weight. A mismatch is a
        # typo, and a silent one would size a trade the wrong way round.
        delta = pa - pb
        want_buy = side in ("COMPRA", "BUY")
        if abs(delta) < 1e-12:
            problems.append(f"{where}: '% ptf before' and '% ptf after' are equal — "
                            f"no trade")
            continue
        if (delta > 0) != want_buy:
            problems.append(f"{where}: {side} was written but the weight goes from "
                            f"{pb}% to {pa}% — check the direction")
            continue

        # Expiry runs from DISSEMINATION, not from when the source executed.
        when = get(r, 1)
        published = str(when) if when else None

        ccy = info["currency"]
        if ccy != "EUR" and ccy not in fx:
            problems.append(f"{where}: no FX rate for {ccy} (pass --fx {ccy}=...)")
            continue

        longest = max(longest, hours)
        trades.append({
            "contract_id_ex": info["contract_id_ex"],
            "desc": str(name),
            "multiplier": info["multiplier"],
            "divisible": info["divisible"],
            "currency": ccy,
            "fx": 1.0 if ccy == "EUR" else fx[ccy],
            "tick_size": info["tick_size"],
            "pct_before": pb,
            "pct_after": pa,
            "signal_price": price,
            "published_at": published,
            "expires_at": None,      # filled below from published_at + hours
            "hours": hours,
            "rationale": str(get(r, 10) or ""),
            "orizzonte": str(get(r, 8) or "").strip(),
            "isin": info["isin"],
            "ticker": info["ticker"],
            "mercato": info["mercato"],
            "plain": "",
            "row": rownum(r, n0),
        })

    now = datetime.now(timezone.utc)
    for t in trades:
        base = None
        raw_when = t["published_at"]
        if raw_when:
            for f in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                      "%d/%m/%Y %H:%M", "%Y-%m-%d"):
                try:
                    base = datetime.strptime(str(raw_when)[:19], f)
                    break
                except ValueError:
                    continue
        if base is None:
            # Never silently fall back to "now": that makes expiry start when the
            # file is READ rather than when it was published, so a stale signal
            # never expires - defeating the one protection that matters most.
            problems.append(f"row {t['row']}: date/time unreadable ({raw_when!r}) — "
                            f"expiry would be counted from now, so a stale signal "
                            f"would never expire. Row discarded.")
            t["_drop"] = True
            continue
        base = base.replace(tzinfo=timezone.utc)
        t["expires_at"] = (base + timedelta(hours=t.pop("hours"))).isoformat(timespec="seconds")
    trades = [t for t in trades if not t.pop("_drop", False)]

    signal = {
        "schema_version": "1.0",
        "source_file": os.path.basename(a.file),
        "read_at": now.isoformat(timespec="seconds"),
        "audience": a.audience,
        "trades": trades,
        "withheld": withheld,
        "problems": problems,
    }
    txt = json.dumps(signal, indent=2, ensure_ascii=False)
    if a.out == "-":
        print(txt)
    else:
        open(a.out, "w").write(txt)
    for p in problems:
        print(f"PROBLEM {p}", file=sys.stderr)
    if problems:
        sys.exit(3)


if __name__ == "__main__":
    main()
