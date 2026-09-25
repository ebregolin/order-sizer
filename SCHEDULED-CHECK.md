# The scheduled check

Optional, and off unless you set it up. Normally you ask — *any new trades?* — and the
assistant checks the source then and there. This page is for having it checked on a
schedule instead, so that you do not have to remember to.

It changes **when** the arithmetic happens and **where** it happens. It does not change
who approves the orders.

## The prompt

In Cowork, ask for a scheduled task at whatever interval suits you, with this text.
Replace everything in angle brackets with your own coordinates and leave the rest as it
is written.

```
Check the source and prepare the orders: use the order-sizer plugin's
`prepare-orders` skill and follow its procedure to the letter.

Source to configure, without asking anyone:
- repository: <the address your source gave you>
- file: <the file name your source gave you>
- key: <your key, if your source publishes an encrypted file - delete this line otherwise>
- language: en
- source contact: <where you can reach your source, if it gave you an address - otherwise leave empty>

This is an automated run and nobody is watching. Ask no questions, never replace a
figure that is missing with an estimate, and if something does not add up report it
instead of working around it: a problem nobody sees is a trade that disappeared in
silence.

If the markets are closed and there are no current prices, say so and give the
timestamp of the last quote available, rather than passing it off as current.

Report compactly. If there is nothing new, two lines are enough.
```

## The coordinates

| Line | What goes in it |
|---|---|
| `repository` | Where your source publishes. A git address, or a path on your own disk. |
| `file` | The name of the file to read inside it. |
| `key` | Only if your source publishes an encrypted file. Otherwise the line goes. |
| `language` | The language the run should report in. See below. |
| `source contact` | An address to point you at if a figure looks wrong. Empty is fine. |

The plugin asks for these once, on its first ordinary run, and remembers the answers in
`~/order-sizer/config.json`. Naming them in the prompt anyway matters for one reason: a
scheduled run cannot ask you anything, so whatever it needs has to be there already.

**The key is a secret.** Putting it in a prompt puts it in a task that is stored and
read again every time it fires. That is a decision, not a formality: if you would rather
not, check by hand, or use a source that publishes in the clear.

## Why the wording is what it is

Three of those sentences are not padding. They are the reason an unattended run is
acceptable at all:

- **Ask no questions.** There is nobody to answer, and a run waiting for an answer is a
  run that did nothing.
- **Never estimate a missing figure.** A number invented to fill a hole reaches the
  arithmetic looking exactly like a real one.
- **A stale price carries its timestamp.** Outside market hours there may be no current
  quote. Said plainly, that is information. Passed off as current, it is a wrong limit
  price.

Rewrite the prompt in your own words if you prefer, but rewrite around those three.

## Where it runs

A scheduled task runs on Claude's machines rather than on your computer. That is why it
works with your computer switched off, and it is the real trade-off here.

Run by hand, everything happens on your own device. Run on a schedule, the arithmetic,
the file that was downloaded and the key you wrote into the prompt are on a machine you
do not own. Nothing reaches the author of this tool either way — it has no telemetry, on
a schedule or not. But "entirely on your own device", which is true of ordinary use,
stops being true here, and you should know that before turning it on.

## What does not change

The orders are prepared, not sent. A scheduled run cannot reach past that: the
instruction sits in your broker's own application until you send it or cancel it, and
that limit is enforced by the broker, not promised by this code. You get the broker's own
notification, the same one you would get asking by hand. If you never open the app,
nothing happens.

Nothing here assesses whether a trade suits you, on a schedule any more than otherwise.
The arithmetic is proportional to the capital you set, and that is all it is.

## Language

`language: en` is the default. Put `it`, `fr`, `de`, `es` or anything else and the run
reports in that language — the assistant is not limited to these. Asking by hand, you do
not need the line at all: the reply comes back in whatever language you wrote in. A
scheduled run has nobody writing to it, which is why it has to be told.

## Turning it off

Ask in Cowork to pause or delete the scheduled task. Nothing else has to be undone: the
configuration stays, and asking by hand keeps working.
