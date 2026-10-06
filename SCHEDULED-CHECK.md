# The scheduled check

Optional, and off unless you set it up. Normally you ask — *any new trades?* — and the
assistant checks the source then and there. This page is for having it checked on a
schedule instead, so that you do not have to remember to.

It changes **when** the arithmetic happens and **where** it happens. It does not change
who approves the orders.

## The prompt

In Cowork, ask for a scheduled task with this text. Replace everything in angle
brackets with your own coordinates and leave the rest as it is written.

**A cadence that works, if you want one to start from:** every hour **at minute 55**,
from an hour before your first market opens until an hour after your last one closes,
on the days those markets trade — and, if your source trades across time zones,
something lighter at weekends, every six hours. Set all of it in **your own local
time**, not UTC.

Minute 55 rather than on the hour for two reasons: scheduled tasks everywhere fire at
:00, and a check that lands a few minutes before the hour gives you the result while
you still have time to act on it rather than just after you have looked away.

How often is a judgement, not a rule. A source that publishes two or three times a
week does not need seventeen checks a day; a source that trades intraday does. What
actually protects you is not frequency — signals stay valid for hours — but
regularity.

```
Check whether my research source has published anything new and, if it has, work out
what the published weights mean for the size of my own account and leave the
resulting order instructions in my broker for me to look at. Use the order-sizer
plugin's `prepare-orders` skill and follow its procedure to the letter.

To be explicit about what this does and does not do: nothing is sent to the market.
The skill can only PREPARE an instruction, which then sits in my Interactive Brokers
app until I open it and either approve it or reject it, one by one. That limit is
imposed by the broker, not by this prompt, and it is not to be worked around: if any
step here seems to require transmitting an order, stop and tell me instead.

Source to configure, without asking anyone:
- repository: <the address your source gave you>
- file: <the file name your source gave you>
- key: <your key, if your source publishes an encrypted file - delete this line otherwise>
- language: en
- time zone: <your own, as an IANA name, e.g. Europe/Rome or America/New_York>
- source contact: <where you can reach your source, if it gave you an address - otherwise leave empty>

This is an automated run and nobody is watching. Ask no questions, never replace a
figure that is missing with an estimate, and if something does not add up report it
instead of working around it: a problem nobody sees is a trade that disappeared in
silence.

If a venue is closed, nothing is prepared for the instruments that trade there — say
which ones and that the next run finding the venue open will prepare them against a
live price. That is a wait, not a failure, and the reader has nothing to do about it.
Never pass a closing price off as a current one.

Report compactly. If there is nothing new, two lines are enough.
```

## Set the times in your own local time

Give the schedule in the time you live in, not in UTC. Then a check you set for 8am
stays at 8am when the clocks change in March and October, instead of drifting an
hour twice a year.

The source's file is written in UTC — that is deliberate, it is the one clock that
does not move — and the plugin converts every time it shows you into yours, with the
offset spelled out, so you never have to do the arithmetic. That is what the time
zone line above is for.

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

- **It says what the run may and may not do, in the first paragraph.** An instruction
  that opens with "prepare the orders" reads, to anyone and to any assistant, like a
  request to trade — and a request to trade on a schedule, unattended, is one that
  deserves to be refused. It is also not what happens: the skill can only leave an
  instruction in your broker, and Interactive Brokers will not move it until you
  approve it yourself. Saying so plainly is not decoration. It is the difference
  between a prompt that describes what it really does and one that appears to ask for
  something dangerous.
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
