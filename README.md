# LuyDek · លុយដេក

**Camera-verified stock tracking for Cambodian mini-marts and pharmacies.**
A camera over the counter photographs every sale and identifies the items. The owner checks only
the uncertain ones, a few taps at a time, in Telegram. Stock is kept from sales and supplier
invoices, and every supplier gets a weekly list before the agent visits: **order**, **skip**,
or **💤 sleeping money** (លុយដេក, "money sleeping on the shelf").

Hackathon MVP for the *Dead Stock, Empty Shelf* challenge. It runs all four demo beats:

| Demo beat | Where |
|---|---|
| 1. Live sale at the counter | `/counter`: webcam, automatic capture when items are still, sale closes when the counter clears |
| 2. A judge verifies an item on their phone | Telegram digest: crop photo + ✅ / look-alike / 🔍 Other / ✖ buttons |
| 3. A photographed invoice raises stock | Send the photo to the bot (or `/invoices`); Claude reads it, the owner confirms with one tap |
| 4. Weekly order list per supplier | `/orders` and Telegram, built from **clearly labelled SIMULATED** history |
| + The 60-day proof | `/report`: before (watching weeks) vs now, per pilot metric |

---

## Setup (Windows, ~2 minutes; CLIP weights come from the local HuggingFace cache)

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/Scripts/python.exe -r requirements.txt
cp .env.example .env            # then fill in the two keys below
.venv/Scripts/python.exe scripts/seed_demo.py --reset
.venv/Scripts/python.exe scripts/simulate_history.py --weeks 8
.venv/Scripts/python.exe -m luydek
```

Open <http://localhost:8000>. For a phone as the counter camera, run with `--host 0.0.0.0`.
Browsers only give camera access on `localhost` or HTTPS, so for the counter page itself
prefer a USB webcam or Iriun Webcam on the laptop.

**`.env`**
- `TELEGRAM_BOT_TOKEN`: create a bot with [@BotFather](https://t.me/BotFather) (`/newbot`). The bot
  uses long polling, so no public URL is needed. Without a token the dashboard still works and its
  review queue replaces the bot.
- `ANTHROPIC_API_KEY`: used only to read invoice photos (`claude-opus-5`, about 3 cents an invoice).
  Without it, invoices are typed into the manual form on `/invoices`.

Verifiers (owner, family, staff) join by sending the bot `/start 2468` (the PIN is in
`configs/config.yaml`). `/lang` switches between Khmer and English.

## Adding your products

1. Put a **plain, matte, dark or saturated mat** under the camera. White pharmacy boxes on a pale
   mat differ only in brightness, the hardest case for spotting items.
2. `/counter` → clear the mat → **Calibrate empty counter**.
3. `/catalog` → **➕ Add product**. Three short steps: the name (with its size), who supplies it and
   what it costs, and how many are on the shelf now. No SKU to invent.
4. **Save and take photos** opens the camera page. Put the item on the mat and press the red
   button 5 times, turning it a little each time. The progress bar turns green at 5 and a
   **Done ✓** button appears.
5. Also enroll the things that sit on counters (`/catalog` → *Things the camera should ignore*):
   phone, cash, calculator, plastic bag.
6. `.venv/Scripts/python.exe scripts/calibrate.py` runs leave-one-out over your photos. It prints
   top-1 accuracy, which products get confused, and the `t_high` / `m_high` / `t_low` values to
   paste into `configs/config.yaml`. **These are the accuracy numbers to quote**: real, from your
   own products, and slightly optimistic because the enrollment photos come from a single session.

Each product has its own page (click it in the catalog or the stock table):
- **Photos:** 🗑 on each photo deletes it after a second tap, or tick several and delete them
  together.
- **Details:** edit name, supplier, cost, case size and shelf life.
- **Stock:** "I counted N" sets the level, with the latest changes listed underneath.
- **Multipacks:** a 6-pack the camera should count as 6.
- **Other names:** "Coca 330", "កូកា" and so on.
- **Remove product:** hides it. Its history is kept.

**🔢 Count stock** on the catalog page counts the whole shop at once (install day, monthly
recount). Suppliers and their visit days are edited at the bottom of the same page.

## Demo script

1. **Live sale.** On `/counter`, a teammate puts 2–3 items on the mat. Boxes appear: green means
   auto-accepted, amber means it will ask. Take the items off and the sale closes.
2. **Telegram check.** On the dashboard, click **Send digest now**. The judge's phone (registered
   with `/start 2468`) gets the uncertain crop, "Is this Ichitan green tea 420ml?", and taps
   **Oishi 380ml**. The dashboard, the stock and every other verifier's copy of the message update,
   and the crop joins the gallery as a new training example.
3. **Stock in.** Send the bot a photo of a supplier invoice → lines matched to products →
   **✅ Correct, add to stock**. New spellings ("Coca 330") are remembered as aliases.
4. **Order lists.** `/orders`, or tap 📦 in the bot. With the seeded history you get, for example, Coca-Cola 2 cases (reason shown), Sprite
   SKIP, Ichitan 💤 $37.95 sleeping, Dumex ORDER "⚠️ was out 4 days, customers went across the
   street". **Say that the history is simulated.** The page and the messages say it too.

**Backups.**
- `scripts/synthetic_demo.py` builds a separate synthetic shop (painted look-alike products already
  enrolled, calibrated mat, history). Run it with
  `python -m luydek --config configs/synthetic.yaml --port 8010`. It never touches `data/luydek.db`,
  and its Telegram bot is off, so it can run next to the real shop without fighting over the bot.
- The **Upload a photo** box on `/counter` (and `scripts/sale_from_photo.py`) treats one photo as
  one sale.
- The dashboard's review queue does everything the Telegram digest does.
- Record a full run as a video.

## How it works

```
counter photo ─► segment (vs. empty-mat photo) ─► split touching items ─► barcode pass (zxing)
             ─► CLIP ViT-B/32 embedding ─► nearest packs in this store's gallery ─► multipack × units ─► tier
   high  (barcode, or score ≥ t_high AND margin to runner-up ≥ m_high) → sale recorded now
   medium (score ≥ t_low) → label reader if look-alikes → "Is this X?"   low → photo grid   ignore → dropped
```

- **Tiers use the margin as well as the score.** Raw CLIP similarities sit in a narrow 0.75–0.95
  band, so the handoff's 0.90/0.60 can't be used as-is. Look-alikes both score high, and it is the
  small gap between them that sends an item to the owner.
- **Touching items.** A blob whose outline is dented (solidity below 0.93) is split at the neck
  between items: distance-transform cores seed a watershed, and the neighbour is painted out of
  each crop. A convex blob is never split, so one long bottle stays one item. Two boxes pushed
  flush edge to edge have no dent and still merge.
- **Label reader.** When a pending item's top guesses score within 0.05 of each other (Paracetamol
  250mg vs 500mg), just the product crop goes to Claude (`claude-opus-5`, low effort, well under a
  cent) with those options, to read the printed size or strength. A clear reading records the sale
  automatically (method `label`). Anything unclear stays with the owner, with what was read shown
  on the card. It runs right after the sale closes, again before each digest, and every 5 minutes
  in case the internet was down. It needs `ANTHROPIC_API_KEY` and can be switched off
  (`labels.enabled`).
- **Transactions.** Every still frame is recognised on its own. At close, each product counts at
  its maximum across frames, not the sum, because items get bagged while more arrive.
- **Ledger.** `stock_movements` holds signed rows: opening, sale, delivery, freebie, adjustment.
  Credit sales and phone orders count as sales (they are demand). Family use, damage and expiry
  are adjustments. Negative stock is shown as "investigate", never as a number.
- **Forecast** (`luydek/services/forecast.py`):

  ```
  avg_daily = weighted average of the last 4 weeks (weights 4-3-2-1), counting only days the item
              was in stock, so a stockout never reads as zero demand
  target    = avg × (days to visit + visit cycle + lead) + avg × safety days (3 critical, 2 others)
  order     = ceil(max(0, target − stock) / case size), capped at what sells before expiry
  ```

  - **SKIP:** no sale in 30 days, or stock covers the cycle plus 14 days.
  - **💤 SLEEPING:** no sale in 45 days, or more than 90 days of stock, valued at cost.
  - **Market run:** a full case would expire before it sold.
  - **Follow-rate** is settled automatically by comparing the next delivery with the
    recommendation, with no taps.
- **Privacy.** Once every item in a sale is settled, the full counter photo is deleted and only the
  product crops are kept.

## Pilot report (`/report`)

The handoff's 60-day proof, week by week. **Before** is the observation weeks (weeks 1–2: camera
and checks on, no order advice). **Now** is the last two full weeks.
- Headline tiles:
  - empty-shelf days on must-have items
  - dead stock (dollars at cost, no sale in 45+ days)
  - order advice followed
  - owner taps per day
  - camera right without help
- Charts: one small chart per metric. Watching weeks are grey, pilot weeks blue, and the week in
  progress is faded and never compared.
- **Show the numbers** holds the full table. **Print or save as PDF** gives a clean handout.

Day 0 is the first day stock was recorded, or set `pilot.start` in `configs/config.yaml`. Every
number comes from the ledger and the logs. When the window includes simulated history, the page
says so in a banner. The simulator keeps ordering "by feeling" throughout, so it shows no
improvement, as it shouldn't.

## Layout

```
luydek/
  services/     catalog · stock (ledger) · transactions · invoices (Claude) · forecast · metrics · report
  recognition/  segment · barcode · encoder (CLIP) · matcher (gallery + tiers) · labels (Claude) · pipeline
  bot/          app (jobs) · handlers · digest (outgoing messages) · keyboards
  web/          server (FastAPI; the bot runs in its event loop) · charts (inline SVG) · templates · static
  i18n.py       Khmer/English strings    render.py  owner-facing message text
scripts/        seed_demo · simulate_history · calibrate · synthetic_demo · sale_from_photo
configs/        config.yaml (thresholds, digest times, forecast rules) · synthetic.yaml
```

`.venv/Scripts/python.exe -m pytest` runs 91 tests covering the ledger, forecast math, recognition,
touching-item splitting, the label reader and invoices (mocked Claude), the pilot report, transactions, bot flows (fake Telegram) and the web app. The tests use a
colour-histogram encoder, so they don't need CLIP.

## Not built yet (and what to say if asked)

- **Detector.** Segmentation against the empty mat stands in for a trained detector. Touching
  items are split when their outline has a neck, but boxes pushed flush edge to edge still merge,
  and pale boxes on a pale mat are only partly seen. A YOLO detector slots in behind
  `segment.segment()`.
- **Label reading offline.** The label reader needs the internet. Without it, look-alikes go to
  the owner as before. A local OCR model is the offline alternative.
- **Multiple stores.** It is single-store (one database per shop). There is no weekly shelf-photo
  spot check yet, no Croston's method or seasonality, and no warm start from distributor history.
- **Khmer strings.** `luydek/i18n.py` was written by a non-native author. Have a native speaker
  proofread it before a pilot.
