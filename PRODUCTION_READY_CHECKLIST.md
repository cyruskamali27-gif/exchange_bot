# PRODUCTION READY CHECKLIST
**Exchange AI System — Cyrus Bot**  
**Last updated:** 2026-05-11

Run this checklist before executing `/go_live`.  
All ✅ items must be confirmed. Any ❌ blocks production.

---

## SECTION 1 — Price Feed

- [ ] `current_price.json` exists and has all 3 currencies (USDT, USD, CAD)
  ```
  cat /var/www/exchange_bot/current_price.json
  ```
- [ ] USDT price is fresh (updated within last 30 minutes)
- [ ] USD price is fresh — **critical**: stale USD causes wrong customer quotes
- [ ] CAD price either has direct feed or USDT→CAD derivation is acceptable
- [ ] `exchange-price` restart count under 10 (check: `pm2 list`)
- [ ] `exchange-ocr` restart count under 10
- [ ] At least 2 price channels actively sending data to `market_prices` table

**Verify:**
```bash
python3 -c "
import sqlite3, time
conn = sqlite3.connect('/var/www/exchange_bot/exchange.db')
rows = conn.execute('SELECT currency, our_sell, our_buy, updated_at FROM current_rates').fetchall()
now = int(time.time())
for r in rows:
    age_min = (now - r[3]) // 60
    fresh = '✅' if age_min < 60 else '❌ STALE'
    print(f'{fresh} {r[0]}: sell={r[1]:,} buy={r[2]:,} (age {age_min}m)')
"
```

---

## SECTION 2 — AI Brain

- [ ] Gemini API key valid (test: `pm2 logs exchange-scanner --lines 5`)
- [ ] No Gemini quota errors in last 24h
- [ ] `exchange_brain.py` returns prices from DB (not invented)
- [ ] `learned_mistakes` table has at least some entries (learning is active)
- [ ] `successful_patterns` table has at least some entries

**Verify:**
```bash
python3 -c "
import sqlite3
conn = sqlite3.connect('/var/www/exchange_bot/exchange.db')
print('learned_mistakes:', conn.execute('SELECT COUNT(*) FROM learned_mistakes').fetchone()[0])
print('successful_patterns:', conn.execute('SELECT COUNT(*) FROM successful_patterns').fetchone()[0])
print('admin_feedback:', conn.execute('SELECT COUNT(*) FROM admin_feedback').fetchone()[0])
"
```

---

## SECTION 3 — Voice Pipeline

- [ ] ElevenLabs API key has remaining credits
- [ ] `VOICE_REPLIES_ENABLED=true` in .env
- [ ] Groq API key valid (STT works)
- [ ] `ffmpeg` is installed (`which ffmpeg`)
- [ ] `assets/office_ambience.mp3` exists (`ls assets/`)
- [ ] Voice note sends without error in test group

---

## SECTION 4 — Admin Panel

- [ ] Admin bot responds to `/start`
- [ ] `/price` shows fresh rates
- [ ] Feedback buttons (👍 👎 ✏️) save to DB when pressed
- [ ] `/corrections` shows at least 1 entry (means learning loop is running)
- [ ] Admin ID is correct in `.env`

---

## SECTION 5 — Scanner Agent

- [ ] Test group (-5124476784) responding correctly to messages
- [ ] Voice messages transcribed (send a Farsi voice note, check reply)
- [ ] Price questions return live numbers (not "نرخ تابلوئه")
- [ ] Admin receives notification with feedback buttons for each reply
- [ ] No Telethon session conflicts (`exchange_agent.session` not locked)
- [ ] IRAN_AGENT_ID receives deal confirmations when [CONFIRMED] fires

---

## SECTION 6 — Blockchain Monitor

- [ ] `exchange-blockchain` online with fewer than 30 restarts
- [ ] TRON_WALLET correct in `.env`
- [ ] TRON_API_KEY configured (empty = rate-limited on public trongrid API)
- [ ] Test by sending a small amount to the TRON wallet and checking admin notification

---

## SECTION 7 — Learning System

- [ ] `nightly_learning.py` ran successfully in last 24h
- [ ] `daily_learning` table has at least 2 rows
- [ ] Admin has rated at least 5 replies (feedback buttons used)
- [ ] `successful_patterns` not empty (admin marked some replies 👍)

---

## SECTION 8 — Security & Safety

- [ ] `SAFE_MODE=false` confirmed (production mode)
- [ ] Target groups in `.env` are the correct production groups
- [ ] `EXCLUDED_IDS` in `config.py` includes any test accounts
- [ ] Admin has tested `/safemode on` and `/safemode off` (can emergency-stop)
- [ ] `ADMIN_APPROVAL_THRESHOLD=1000` — deals over 1000 CAD require manual approval
- [ ] Turkey and China agent IDs configured if those markets are active

---

## GO-LIVE PROCEDURE

### Step 1 — Verify all sections above are ✅

### Step 2 — Confirm in test group
Send these test messages to the test group and verify correct responses:
- "سلام"
- "قیمت تتر چنده"
- "میخوام ۱۰۰ دلار بفروشم"
- (send a voice note in Farsi)

### Step 3 — Enable production mode
In the admin bot:
```
/go_live
```

### Step 4 — Restart scanner
```bash
pm2 restart exchange-scanner
```

### Step 5 — Monitor for 60 minutes
```bash
pm2 logs exchange-scanner --lines 50
```

Watch for:
- Telethon session errors
- Gemini API errors
- Any unexpected group messages being processed

### Step 6 — Emergency stop
If anything goes wrong:
```
/safemode on
```
Then: `pm2 restart exchange-scanner`

---

## CURRENT STATUS (as of 2026-05-11)

| Check | Status | Action Needed |
|-------|--------|---------------|
| current_price.json | ❌ Missing | Fixed in price_engine.py — restart exchange-price |
| USDT price feed | ✅ Active | — |
| USD price feed | ❌ Stale (24h) | Find active USD channel or fix tahran_sabza parsing |
| CAD direct feed | ❌ Derived only | Add a CAD/toman Telegram channel |
| Admin feedback | ❌ 0 rows | Start using 👍 👎 ✏️ buttons on every reply |
| Learning patterns | ❌ 0 rows | Follows from admin feedback |
| Hume AI | 🟡 Configured | Cooldown added — check logs after restart |
| OCR confidence | 🟡 Improved | Restart exchange-ocr to apply fix |
| Test group | ✅ Responding | — |
| /go_live command | ✅ Ready | Use when above items are resolved |

**Estimated production readiness: 45%**  
**Blocker:** USD price staleness + zero learning data
