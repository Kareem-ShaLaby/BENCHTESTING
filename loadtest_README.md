# Load test — quick start

1. Copy `loadtest.py` into a **throwaway folder** alongside a copy of
   your `bot.py` — not your real production directory (it writes real
   local JSON files as a side effect of running your real code; see the
   "DATA SAFETY" section at the top of `loadtest.py`).

2. Use the same Python environment your bot actually runs in (the one
   with `python-telegram-bot`, `reportlab`, `python-docx` installed).

3. Run it:

   ```bash
   # Both scenarios, a few concurrency levels
   python3 loadtest.py --scenario both --users 100 300 500 700

   # Just the Daily Quiz path (the heaviest one) at 700
   python3 loadtest.py --scenario daily_quiz --users 700

   # With a simulated 80ms network round-trip per Telegram call, to get
   # closer to real-world absolute numbers instead of pure code speed
   python3 loadtest.py --scenario both --users 700 --fake-latency-ms 80
   ```

4. Read the p50/p95/p99 numbers printed for each run. The bottom of
   `loadtest.py` has a longer "READING THE RESULTS" section on what to
   actually do with them — short version: watch whether p95 grows
   roughly linearly with user count (fine, just a capacity/vCPU question)
   or shoots up sharply past some threshold (probably a real algorithmic
   bottleneck worth finding, same shape as the earlier O(n) bugs this
   bot already had fixed).

## What this measures vs. doesn't

Measures: how long your bot's own code takes per request, under
concurrency, on the machine you run it on. This is the thing you can
actually change.

Doesn't measure: real Telegram network latency, Telegram's own rate
limits, or Railway's actual CPU ceiling. Combine this script's results
with Railway's CPU/memory dashboard during a real comparable traffic
burst if you want to translate "code latency" into "the vCPU number
Railway shows using."
