# Kalshi Sports Trading Assistant

A Python tool that scans **Kalshi** sports markets (NFL, college football, major soccer), estimates outcome probabilities with statistical models, compares them with the **actual executable prices**, and ranks trades by **expected value after Kalshi fees**. It re-prices games **while they are being played** (enter / hold / sell on every check, with momentum measured and reported), writes **halftime reports**, tracks a **$25 entertainment bankroll**, **paper-trades** every recommendation, and sends **Telegram** alerts to your phone.

> **Recommendation-only.** This version cannot place orders on Kalshi. Paper trading is the default; real positions are only *tracked* when you enter them yourself.

---

## 1. What it does

| Area | What you get |
|---|---|
| Market discovery | Finds Kalshi sports series via `GET /series?category=Sports`, classifies them (sport, competition, market type, period), loads open events/markets. No hardcoded tickers. |
| Markets | Winner / moneyline, soccer draw, full-game / half / quarter totals, team totals, spreads, both-teams-to-score — whatever Kalshi actually lists. |
| Models | NFL & college: ridge-regression margin ratings + offense/defense points model. Soccer: Poisson attack/defence model with Dixon-Coles correction (win/draw/loss, exact goal lines, BTTS, team totals, handicaps). |
| Value engine | Executable ask, model vs market-implied probability, edge, gross & net EV (Kalshi fee formula, per-order rounding), max loss, profit if right, spread, depth, data age, reliability. |
| Live & halftime | Probabilities recomputed from score, clock, period, possession (optional), red cards. Halftime gives three separate decisions: **enter new**, **keep holding**, **sell**. |
| In-game momentum | Scoring runs, price moves and model moves over the last N minutes. Flags when the market looks **over-reacting** or **lagging** relative to the updated probability. |
| Hold vs sell | Sweeps the real order book to see what you could actually sell for, after the selling fee, and compares it with the estimated value of holding. |
| Bankroll | Paper and real (tracked) accounts, reserved funds, exposure, per-game correlation cap, daily/weekly loss limits, fractional-Kelly sizing clamped to $0.50–$1.00. |
| Alerts | Telegram, with deduplication, thresholds and an order-book re-check right before any "buy" alert. |
| Automation | GitHub Actions for scans, best-effort live checks and a daily summary; or a continuous background service. |

## 2. What it does NOT guarantee

- **No profit is promised.** Simple rating models are usually *less* accurate than liquid betting markets. Most "edges" a simple model finds are model error. That is why probabilities are shrunk toward the market price by default (`value.model_weight: 0.6`) and why everything starts in paper mode.
- Injuries, lineup news and weather are **reported, not modeled**. Check them yourself before acting.
- GitHub Actions schedules are **not real-time**. Runs can be late or skipped, so halftime alerts are not guaranteed there.
- Kalshi's sports series names are not an official taxonomy. Classification is heuristic. Anything it can't parse is skipped, never guessed. Verify it with `discover`.
- Not financial advice. Only use money you have set aside for entertainment.

## 3. Requirements

Python **3.10+** (3.12 recommended), git, and internet access to Kalshi and the data providers.

## 4. Install

```bash
git clone https://github.com/<you>/kalshi-sports-assistant.git
cd kalshi-sports-assistant
python -m venv .venv
# Windows PowerShell:  .venv\Scripts\Activate.ps1      macOS/Linux:  source .venv/bin/activate
pip install -e ".[dev]"
copy config\settings.example.yaml config\settings.yaml      # Windows  (cp on macOS/Linux)
copy .env.example .env                                       # then edit .env
```

Dependencies: `requests` (HTTP), `PyYAML` (config), `numpy` (model fitting), `cryptography` (Kalshi request signing, only used with an API key), `pytest` (tests).

## 5. Kalshi access

- **Market data needs no account or key.** Scanning, analysis, live monitoring and paper trading all use public endpoints (`/series`, `/events`, `/markets`, `/markets/{ticker}/orderbook`).
- *Optional:* for read-only balance and positions (`kalshi-assistant kalshi-account`), create an API key at kalshi.com → Account → API Keys. Put the Key ID in `KALSHI_API_KEY_ID`. Keep the private key **outside the repo** and set `KALSHI_PRIVATE_KEY_PATH`, or paste the PEM into `KALSHI_PRIVATE_KEY` (GitHub secret). Requests are signed exactly as documented (timestamp + method + path without the query string; RSA-PSS SHA-256 or Ed25519).
- `KALSHI_ENV=demo` points at Kalshi's demo environment.

## 6. Sports data sources

| Source | Used for | Cost / access |
|---|---|---|
| ESPN public site API (`site.api.espn.com`) | Schedules, live scores, clock, period, linescores, possession, red cards, injuries, box-score stats, historical results | Free, no key. **Unofficial and undocumented.** It can change or rate-limit without notice. |
| football-data.org v4 | Official soccer results (PL, CL, La Liga, Bundesliga, Serie A, Ligue 1) | Free tier with key (`FOOTBALL_DATA_API_KEY`), 10 requests/min. MLS needs a paid plan. Enable with `sports.soccer_results_provider: football-data`. |
| Open-Meteo | Kickoff weather (wind, temperature, rain) for outdoor football | Free, documented, no key. |
| Anthropic API (optional) | Plain-English, facts-only explanation (`game --explain`) | Paid API key (`ANTHROPIC_API_KEY`). Never used for probabilities. |
| **Not included** | Soccer expected goals (xG), confirmed lineups, NFL play-by-play/EPA | Needs paid providers (e.g. Opta/Stats Perform, API-Football paid tiers, SportsDataIO). Add one by implementing the `SportsDataProvider` protocol in `sports_data.py`. |

Every fact shown carries its source name. Missing data is labeled **unavailable**, never filled in.

## 7. Telegram

1. In Telegram, message **@BotFather** → `/newbot` → copy the **bot token**.
2. Open your new bot and send it any message (e.g. "hi").
3. Put the token in `.env` as `TELEGRAM_BOT_TOKEN`, then run `kalshi-assistant telegram-chat-id`. Put the number in `TELEGRAM_CHAT_ID`.
4. Run `kalshi-assistant test-alert`. Your phone should buzz.

For GitHub Actions, add the same two values as repository **secrets** (step 12). Never commit them.

## 8. The $25 bankroll

```bash
kalshi-assistant set-bankroll 25            # your real entertainment money (tracking only)
kalshi-assistant set-bankroll 25 --paper    # paper-trading money
kalshi-assistant set-bankroll 30 --reserve 5   # $30 cash, $5 of it off-limits
kalshi-assistant bankroll
```

Defaults (change in `settings.yaml` → `risk`): $0.50–$1.00 per position, at most 40% of equity in open positions, $2.00 max on any one game (correlated positions count together), $5/day and $10/week loss limits. Stakes are sized from the *current* bankroll, so they shrink after losses and never grow to chase them.

## 9. Using the CLI

```bash
kalshi-assistant help
kalshi-assistant discover                  # what Kalshi series were found and how they were classified
kalshi-assistant scan                      # all sports; add --all to see non-qualifying candidates
kalshi-assistant best --limit 5
kalshi-assistant nfl | college | soccer
kalshi-assistant game "Arsenal vs Chelsea"          # full report(s); live games show the in-game view
kalshi-assistant compare "Arsenal Chelsea" --types winner,total --available 25
kalshi-assistant plan "Bills" "over 44.5" --side yes --amount 1   # shows max loss first
kalshi-assistant live "Ohio State"         # ENTER / HOLD / SELL right now, with momentum
kalshi-assistant halftime "Ohio State" --send
kalshi-assistant positions                 # open positions
kalshi-assistant cashout <position-id>     # hold vs sell
kalshi-assistant watch "Ohio State" --follow   # monitor this game in your terminal
kalshi-assistant history
kalshi-assistant backtest --sport nfl
```

Example request: *"I have $25 available. Compare the favorite moneyline and over 2.5 goals for this soccer match."*

```bash
kalshi-assistant compare "Liverpool Everton" --types winner,total --available 25
```

This pulls the actual listed contracts, prices both teams, and shows the best side of each market type with probability, implied probability, net EV per dollar and max loss. It then says which offers better estimated value, or prints **"No qualifying trades found."**

## 10. Paper trading

```bash
kalshi-assistant plan "Bills" "Bills" --side yes --paper-buy   # record one qualifying idea
kalshi-assistant paper auto --max 3       # paper-trade the top qualifying scan results
kalshi-assistant paper sell <id>          # hypothetical sale at the current best bid
kalshi-assistant paper settle             # settle finished markets from Kalshi results
kalshi-assistant paper report             # all trades incl. losses, Brier vs market
```

Did you place a real trade on Kalshi yourself? Track it so the hold/sell engine follows it:
`kalshi-assistant --real positions add --ticker <TICKER> --side yes --contracts 1 --price 0.55 --fees 0.02`

## 11. GitHub Actions

1. Create a GitHub repo and push this project (see "Putting it on GitHub" below).
2. Repo → **Actions** tab → enable workflows.
3. Workflows:
   - `tests.yml`: runs the test suite on every push.
   - `scan_markets.yml`: hourly scan + dashboard + paper trades + settlement, and every ~10 min during game windows.
   - (live/halftime checks are part of `scan_markets.yml`: every ~10 min in game windows, best effort).
   - `daily_summary.yml`: morning summary to Telegram.
4. Actions settings come from `config/settings.example.yaml` (no secrets in it). Edit that file to change thresholds for Actions.
5. State (bankroll, positions, alerts) lives in SQLite. It is saved to a separate **`bot-state`** branch after each run (`scripts/state.sh`). Runs share one `concurrency` group, so they never overlap.

**Minutes/cost:** public repos get free Actions minutes. Private repos get 2,000 free minutes/month. The default live schedule uses roughly 1,500–3,000 minutes/month, so for a private repo either trim the cron lines in `monitor_games.yml` or run live monitoring on your own machine. Note that a public repo's `bot-state` branch (paper trades and alerts) is public too.

## 12. Secrets

Repo → Settings → Secrets and variables → Actions → **New repository secret**:
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (required for alerts); optional `KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY`, `FOOTBALL_DATA_API_KEY`, `ANTHROPIC_API_KEY`.
Locally they go in `.env` (git-ignored). Known secret values are redacted from logs.

## 13. Continuous monitoring (for reliable in-game/halftime alerts)

`kalshi-assistant monitor --loop --interval 60` checks every minute. Options:

| Host | Cost | Notes |
|---|---|---|
| Your Windows PC | Free | `deploy/run_monitor_windows.bat`. The PC must stay awake during games. |
| Raspberry Pi | ~$60–100 one-time | `deploy/kalshi-assistant.service` (systemd). |
| Small cloud VM (e.g. Oracle Cloud Always Free, or a $4–6/month VPS) | $0–6/month | Docker: `docker build -t ksa . && docker run -d --env-file .env -v ksa-data:/app/data ksa` |

Prices are approximate and change; check the provider. With a background service, keep its database separate from the Actions `bot-state` copy (or disable `monitor_games.yml`), so you don't run two independent ledgers.

## 14. Reading the reports

- **Executable price**: what you would pay *now* (the ask), not the last trade.
- **Implied probability** = that price. **Estimated probability** = model output blended with the market mid (labeled *estimate*).
- **Edge** = estimated probability − price. **Net EV** subtracts the actual fee for your order size. Kalshi rounds fees up to the cent per order, so tiny orders pay proportionally more.
- **Reliability** (0–1) measures *how much data backs the model*. It is **not** the chance of winning.
- **Max loss** = everything you paid, including fees. A $1 contract settles at $1 or $0.
- A trade **qualifies** only if the market is open, data is fresh, spread and depth are acceptable, reliability is high enough, and edge and net EV clear your thresholds. Otherwise the tool says why, or prints **"No qualifying trades found."**
- **Momentum:** a run by one team matters only through the price. If the market moved further than the updated probability, it may be over-reacting and any value is on the other side. If the market lags, value may be on the team that scored. The momentum tilt (`live.momentum_weight`) is **0 by default**, because "hot hand" persistence is weakly supported. Raise it only if your paper results justify it.

## 15. History & performance

`kalshi-assistant history`, `paper report`, `summary`. Reports include every trade (wins and losses), win rate, net P/L, ROI, fees, average estimated edge vs realized result, Brier score/log loss vs market-implied probabilities, and splits by sport, market type, pregame/live and model version. `backtest` runs a walk-forward test (no look-ahead) and reports sample size and calibration. It measures the probability model, **not** trading profit, because historical Kalshi prices are not in that dataset.

## 16. Known limitations

- ESPN's API is unofficial. Field changes can make games show as DATA UNAVAILABLE until fixed.
- Kalshi series classification is heuristic. Use `discover --verbose` and `sports.series_overrides`.
- Kalshi event page URLs are not documented, so links go to the series page.
- Football uses a normal approximation (ignores NFL key numbers 3/7). Overtime is approximated. Soccer ignores extra time.
- Red-card and possession effects use stated assumptions (configurable).
- College ratings for teams with little data (often FCS) start below average (`cfb_unknown_team_prior`, an assumption). Reliability is reduced.
- UCL ratings only use UCL results unless you also load domestic data.
- Fee `fee_multiplier` semantics are not precisely documented by Kalshi. Verify fees on https://kalshi.com/fee-schedule.
- GitHub Actions timing is best-effort. Kalshi and ESPN rate limits apply; the client throttles and backs off.

## 17. Tests

```bash
pytest -q
```

They cover fee math, EV, qualification, bankroll limits and sizing, model consistency, live/halftime re-pricing, hold-vs-sell, paper accounting, alert dedup and recheck, request signing, pagination/retries, market parsing, ESPN parsing, locks, momentum, and an end-to-end run. All test data is **mock data**, labeled as such in `tests/helpers.py`.

## 18. Stopping

- Local loop: `Ctrl+C`, or `kalshi-assistant stop-monitor` from another terminal.
- Alerts: set `notify.enabled: false`, or remove the Telegram secrets.
- GitHub Actions: Actions tab → each workflow → "…" → **Disable workflow**. Or set a repository *variable* `KSA_DISABLED=true` to make every scheduled job skip.

## Architecture

```
src/kalshi_assistant/
  config.py            settings + env secrets + log redaction
  kalshi_client.py     Kalshi REST client (throttle, retries, pagination, signing), order book
  market_discovery.py  series classification, contract parsing (ContractSpec), SportsMarket
  fees.py              Kalshi fee formulas
  sports_data.py       provider protocol, ESPN / football-data.org / Open-Meteo, team matching
  football_model.py    shared NFL/CFB rating model;  nfl_model.py, college_football_model.py
  soccer_model.py      Poisson + Dixon-Coles
  live_model.py        in-game windows: banked score + remaining expectation
  calibration.py       Brier, log loss, calibration, walk-forward backtest
  analysis.py          markets + games + models -> probabilities -> opportunities
  value_engine.py      EV after fees, qualification, ranking
  bankroll.py          ledger accounts, exposure, limits, sizing
  positions.py         position records
  paper_trading.py     paper fills, sells, settlement, performance
  cashout.py           hold vs sell
  live_decisions.py    in-game ENTER/HOLD/SELL + momentum
  halftime.py          halftime reports
  notifications.py     Telegram + dedup
  monitoring.py        tick/loop orchestration, alerts, daily summary
  report.py            report formatting
  execution.py         real-order guard rails (disabled, not implemented)
  explain.py           optional facts-only Claude explanation
  storage.py           SQLite (WAL), locks, audit log
  cli.py               commands
```

## Putting it on GitHub

```bash
git init -b main && git add . && git commit -m "Kalshi sports assistant v1"
gh repo create kalshi-sports-assistant --public --source . --push   # or create it on github.com and `git push`
```
