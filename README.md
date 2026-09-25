# order-sizer

Takes a position weight expressed as a percentage of a portfolio, and computes the
corresponding quantity for a portfolio of a different size.

That is the whole function. It is arithmetic.

## Install

1. **Download `order-sizer.plugin`** — click it in the file list above, then the
   download button at the top right of the file view.
2. **Drag it into a conversation** in Cowork, in the Claude desktop app. A panel with
   an install button appears.
3. **Ask it something**, e.g. *"any new trades?"*. On the first run it asks one
   question — where the file it should read is — and never asks again.

You need the Claude desktop app with Cowork, and a broker connector enabled in it.
Nothing else: no API keys, no accounts, no command line.

The rest of this repository is the same plugin unpacked, so that anyone who wants to
read the code before installing it can do so. `order-sizer.plugin` is built from
exactly those files.

## What it is

Three Python files and a skill file, for use with an AI assistant.

| File | What it does |
|---|---|
| `fetch_source.py` | Retrieves a file from a local path or from a git repository the user configures. Decrypts it if the user supplies a key. |
| `read_sheet.py` | Reads instrument, weight and price from that spreadsheet. |
| `mirror.py` | Computes the quantity, the rounding and a limit price. |
| `SKILL.md` | Instructions for the assistant: how to run the three scripts and what to do with what they return. |

The Python has no required third-party dependencies, no obfuscation and no dynamic
code execution. It is written to be read.

## Who does what

This matters, so it is stated exactly:

- **The three scripts read a file and do arithmetic.** They contain no call to any
  broker — `mirror.py` and `read_sheet.py` import nothing capable of reaching the
  network at all.
- **`SKILL.md` instructs the assistant.** It tells the assistant to run the scripts
  and, with the figures they return, to prepare an order instruction.
- **The assistant makes that call through the user's own broker connector**, under
  the user's own credentials, in the user's own account.
- **The instruction is prepared, not sent.** The user reviews it and approves or
  rejects it in their broker's own application. That limit is enforced by the broker,
  not promised by this code.

## What it does not do

- **It does not assess anything.** It forms no view on the merits of a position, does
  not consider whether one suits the user, and collects no information about the
  user's objectives, situation or risk tolerance.
- **It does not execute or transmit orders**, and cannot move money.
- **It sends nothing to the author. No telemetry of any kind.** Nothing reports that
  it was installed, that it ran, or what it was used for.
- **It makes one outbound call, to a destination the user chooses.**
  `fetch_source.py` runs `git clone` against the repository named in the user's own
  configuration, and may call `openssl` to decrypt what it downloaded. That host sees
  the request as any website would.
- **It does not choose the source**, and expresses no opinion about it.

## Example usage

> *any new trades?* · *prepare the orders* · *check the source*

Ask in whatever language you prefer; the assistant replies in it.

## Setup

A broker connector enabled in the assistant, and the location of the file to read.

On first run the plugin asks one question — where that file is — and saves the answer
to `~/order-sizer/config.json` in the user's own home directory. No API keys, no
environment variables.

## The scheduled check

Optional. Instead of asking, the source can be checked for you at an interval you choose,
with nobody watching — it runs on Claude's machines, so it works with your computer
switched off. `SCHEDULED-CHECK.md` has the prompt to use, the three instructions in it
that should not be rewritten, and what turning it on does and does not change.

## Design notes

**The quantity.** If the source expresses a position as a percentage of a portfolio,
before and after, then for a portfolio of value `NAV` the quantity is:

```
(pct_after - pct_before) / 100 x NAV x fx / (price x multiplier)
```

A second form is accepted, for files that publish a quantity and a portfolio value
instead of percentages.

**Rounding.** Fractional instruments keep four decimals. Anything not fractional
rounds to a whole contract, **ties away from zero** — Python's built-in rounding is
half-to-even, so 2.5 goes to 2 while 3.5 goes to 4, which is indefensible in a sizing
calculation. Any residual difference is reported, never hidden, both in percentage
points of the portfolio and as a proportion of the position.

**The limit price.** Set at the tighter of two bounds: a budget expressed as a share
of the portfolio, and a cap of 0.5% of the price. The budget alone is not sufficient —
dividing a fixed cash budget by position size would grant a small position an enormous
allowed price move. The result is rounded to a tradable increment, always in the
stricter direction.

**Prices.** Figures are measured against the price recorded in the file at
publication, not against any execution price. Where no live quote is available the
published price is used and the output says so.

## Licence

Apache License 2.0. See `LICENSE` and `NOTICE`.

---

## Notice

`order-sizer` is a free, open-source tool released under the Apache License 2.0. It takes the
weight of a position, expressed as a percentage of a portfolio, and scales it to the portfolio size
the user supplies.

It is not investment advice, not a personal recommendation and not an investment service. It does not
assess whether a trade is suitable or appropriate for the user, it neither collects nor requests any
information about objectives, financial situation or risk tolerance, and it expresses no view on the
merits of the trades it scales.

It does not transmit or execute orders. It produces numbers: entering, checking and confirming every
order remains with the user, in the user's own broker and under the user's sole responsibility. It
runs on the user's own device with the user's own credentials, or — where the user sets up a
scheduled check — in the cloud environment of the user's own assistant. In neither case does it send
any data to the author.

It is independent of any subscription: it works with any source that publishes an instrument and a
weight, and anyone may use it. No fee of any kind pays for its supply, installation, configuration or
maintenance.

It is provided "as is", without warranty of any kind. Users must check every figure before placing an
order. Investing in financial instruments carries the risk of loss, including total loss of capital.

The name. `order-sizer` is the author's name for this tool. Anyone who modifies and redistributes it
must state the changes, as the licence requires, and is asked to give it a different name: a modified
version carrying the same name will be attributed to the author.

### Avviso — traduzione italiana

*Traduzione di cortesia della sezione precedente. Il testo inglese è quello di riferimento.*

`order-sizer` è uno strumento gratuito e open source, distribuito con licenza Apache 2.0.
Prende il peso di una posizione, espresso in percentuale di portafoglio, e lo riproporziona sulla
dimensione di portafoglio indicata da chi lo usa.

Non è consulenza in materia di investimenti, non è una raccomandazione personalizzata e non è un
servizio di investimento. Non valuta se un'operazione sia adeguata o appropriata per chi lo usa, non
raccoglie e non richiede informazioni su obiettivi, situazione patrimoniale o tolleranza al rischio,
e non esprime alcun giudizio sul merito delle operazioni che riproporziona.

Non trasmette e non esegue ordini. Produce dei numeri: l'inserimento, la verifica e la conferma di
ogni ordine restano a chi lo usa, nel proprio intermediario e sotto la propria esclusiva
responsabilità. Gira sul dispositivo di chi lo usa, con le sue credenziali, oppure — se viene
attivato il controllo automatico programmato — nell'ambiente cloud del suo assistente. In nessuno dei
due casi trasmette dati all'autore.

È indipendente da qualsiasi abbonamento: funziona con qualunque fonte che pubblichi strumento e peso,
e chiunque può usarlo. Nessun corrispettivo, in nessuna forma, remunera la sua fornitura, la sua
installazione, la sua configurazione o la sua manutenzione.

È fornito "così com'è", senza garanzie di alcun tipo. Chi lo usa è tenuto a verificare ogni numero
prima di inserire un ordine. Gli investimenti in strumenti finanziari comportano il rischio di
perdita, anche integrale, del capitale.

Il nome. `order-sizer` è il nome che l'autore dà a questo strumento. Chi lo modifica e lo
redistribuisce è tenuto a dichiarare le modifiche, come previsto dalla licenza, e gli si chiede di
dargli un nome diverso: una versione modificata che porta lo stesso nome viene attribuita all'autore.
