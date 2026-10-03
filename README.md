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

**Setup** (`/setup`) lists every install-day step with a button each and ticks them off as they
are done: calibrate, mark the mat, the tablet tray (pharmacies), product photos, the stock count,
suppliers, Telegram and the Claude key. The steps below are the same, in detail.

1. Put a **plain, matte, dark or saturated mat** under the camera. White pharmacy boxes on a pale
   mat differ only in brightness, the hardest case for spotting items.
2. **Counter camera** (`/counter`) → **Camera setup** → clear the mat → **Calibrate empty counter**,
   then **Mark the mat**: drag a box from
   one corner of the mat to the other. Only items inside it count, so hands, the phone by the till
   and the desk around the mat are never taken for sales.
3. **Products** (`/catalog`) → **➕ Add product**. Three short steps: the name (with its size), who supplies it and
   what it costs, and how many are on the shelf now. No SKU to invent.
4. **Save and take photos** opens the camera page. Put the item on the mat and **hold the
   button down** while turning it: front, back, sides, standing and lying down, letting go of it
   for a moment at each position. A photo is taken each time it comes to rest somewhere new
   (15–20 in half a minute). Frames with the hand in them, anything running into the edge of the
   mat, and near-copies of a photo it already has are skipped, so a long hold never floods the
   gallery (`recognition.enroll_duplicate`, 0.985, measured on this shop's photos). A quick tap
   still takes one photo. The progress bar turns green at 5 and a **Done ✓** button appears.
5. Also enroll the things that sit on counters (`/catalog` → *Things the camera should ignore*):
   phone, cash, calculator, plastic bag.
6. `.venv/Scripts/python.exe scripts/calibrate.py` runs leave-one-out over your photos. It prints
   top-1 accuracy with and without the colour tie-break, which products get confused, and the
   `t_high` / `m_high` / `t_low` values to paste into `configs/config.yaml`. `--include-removed`
   also tests photos of removed products. **These are the accuracy numbers to quote**: real, from
   your own products, and slightly optimistic because the enrollment photos come from a single
   session.

Each product has its own page (click it in the catalog or the stock table):
- **Photos:** 🗑 on each photo deletes it after a second tap, or tick several and delete them
  together.
- **Details:** edit name, supplier, cost, case size and shelf life.
- **Stock:** "I counted N" sets the level, with the latest changes listed underneath.
- **Multipacks:** a 6-pack the camera should count as 6.
- **Other names:** "Coca 330", "កូកា" and so on.
- **Remove product:** hides it, and the camera stops matching it. Its history is kept.
- **💊 Sold by the tablet:** for medicine cut from the strip. Stock is then counted in tablets
  (the stock so far is multiplied by the tablets per box), and the box's photos and barcode stay on
  the box. See *Pharmacies: loose tablets* below.

**🔢 Count stock** on the Products page counts the whole shop at once (install day, monthly
recount). Suppliers and their visit days are edited at the bottom of the same page.

### Faster onboarding: the product list builds itself

Nobody has to type in and photograph 300 products before day one.

- **From invoices.** Photograph a few recent supplier invoices on **Deliveries**. A line that
  isn't a product yet comes with a clean name, the case size and the price, so **Add as new**
  (or **Add all N as new products**) makes it in one tap, under that invoice's supplier; a new
  supplier printed on the invoice is added with its visit day. A case whose size isn't printed
  asks "How many in one case?". For old invoices choose **Only add the products**: products,
  names and prices are kept, no stock is added twice. In Telegram the same lines get a
  **➕ Add …** button.
- **While selling.** Something the camera can't place gets a **New product** button on Today
  and in Telegram. The package is read for its name first (Claude, `labels.name_new`), so it's
  usually one tap: **Add Pocari Sweat 500ml**. Otherwise type it, or reply to the bot's
  "What is it?". The sale is recorded, the photo teaches the camera, and other unanswered sales
  that look like it are re-asked as it, so one **All correct** settles them. **Not now** keeps it
  in *New products to name* for two weeks. A name that is already a product ("Coke") records that
  product instead of a twin.
- **What they still need.** Products learned this way say where they came from on Products
  (*New from sales and invoices*). A product with no supplier is on no order list, so Today says
  so. A product learned at the counter shows *not counted · N sold* until it's counted: its sales
  count as demand, but its order line says "count the shelf once first" rather than guessing.

### Pharmacies: loose tablets

A pharmacist often sells 1 or 2 tablets cut from a strip. Those are too small for the camera to
name, so the counter gets a **tablet tray**: a small matte black card in one corner of the mat.

1. Put the card down, then on `/counter` **Calibrate** (card empty) and press **💊 Mark the tablet
   tray**. Drag a box just inside the card.
2. On the medicine's product page, switch on **Sold by the tablet** (say 100 per box), and take
   photos of the **box**. The tablets themselves need no photos.
3. Selling loose: put the cut pieces or tablets on the card and the **box they came from beside
   it**. The box says which medicine; the tray counts the tablets. The box is not sold.
   A box on its own, with nothing on the tray, is a whole-box sale.

The tray counts by size: each piece is so many tablets of a size it learned from earlier checks. Until
it has measured a medicine's tablet (2 checked tablets), its counts go to Claude, then to the owner as
"💊 Sold loose: 3 × Cetirizine?", and that answer teaches it. A forgotten box asks "which medicine?"
with the loose-sold medicines as buttons.

## Demo script

1. **Live sale.** On `/counter`, a teammate puts 2–3 items on the mat. Boxes appear: green means
   auto-accepted, amber means it will ask. Take the items off and the sale closes.
2. **Telegram check.** In the navigation bar, **Demo → Send the check digest now**. The judge's phone (registered
   with `/start 2468`) gets the uncertain crop, "Is this Ichitan green tea 420ml?", and taps
   **Oishi 380ml**. The Today page, the stock and every other verifier's copy of the message update,
   and the crop joins the gallery as a new training example.
3. **Stock in.** Send the bot a photo of a supplier invoice → lines matched to products →
   **✅ Correct, add to stock**. New spellings ("Coca 330") are remembered as aliases.
4. **Loose tablets** (synthetic shop). Upload `sale4_cetirizine_first_loose_sale.jpg` with
   *Taken with this counter camera* ticked: it asks to check "2 × Cetirizine". Answer ✅, then upload
   `sale5_cetirizine_three_tablets.jpg`: 3 tablets (a cut piece of two and a single) are recorded
   without asking, and the box is not sold. `sale6_tablets_without_the_box.jpg` asks which medicine.
5. **Order lists.** `/orders`, or tap 📦 in the bot. With the seeded history you get, for example, Coca-Cola 2 cases (reason shown), Sprite
   SKIP, Ichitan 💤 $37.95 sleeping, Dumex ORDER "⚠️ was out 4 days, customers went across the
   street". **Say that the history is simulated.** The page and the messages say it too.

**Backups.**
- `scripts/synthetic_demo.py` builds a separate synthetic shop (painted look-alike products already
  enrolled, calibrated mat with a marked tablet tray, history). Run it with
  `python -m luydek --config configs/synthetic.yaml --port 8010`. It never touches `data/luydek.db`,
  and its Telegram bot is off, so it can run next to the real shop without fighting over the bot.
- The **Demo: upload a photo** box on `/counter` (and `scripts/sale_from_photo.py`) treats one photo as
  one sale.
- The Today page's check list does everything the Telegram digest does, including **All correct**.
  Answers take effect at once with **Undo** for 10 seconds (the answer is only sent when the time is up,
  or straight away if the page closes), and new items appear by themselves every 20 seconds. With no
  Wi-Fi, answers wait in an outbox on the phone (a notice says how many) and send themselves when the
  connection is back, from whichever page is open.
- Record a full run as a video.

**On a phone.** Start with `python -m luydek --host 0.0.0.0` and scan the QR code on **Setup** →
*Open LuyDek on your phone*, then *Add to Home screen*. Anyone on the same Wi-Fi can open it (there is
no login yet), so use the shop's own Wi-Fi. Over plain Wi-Fi the counter camera page stays on the
counter computer: browsers only allow the camera on `localhost` or HTTPS.

**Phone as the counter camera.** The laptop keeps running LuyDek (it does the recognising); the phone
is only the eye. It needs an `https://` address, and Tailscale gives one that only your own devices
can open, from any network:
1. On the laptop: open Tailscale and sign in. In the Tailscale admin page, under **DNS**, turn on
   **MagicDNS** and **HTTPS certificates** (the command in step 2 prints the link if they are off).
2. `tailscale serve --bg 8000` publishes LuyDek to your devices at `https://<laptop-name>.<tailnet>.ts.net`
   (`tailscale serve status` shows the exact address; `tailscale serve reset` takes it down).
3. Start LuyDek as usual (it can stay on `127.0.0.1`), adding that address so Setup's QR code uses it:
   `python -m luydek --public-url https://<laptop-name>.<tailnet>.ts.net`
4. On the phone: install Tailscale, sign in with the same account, scan the QR code on **Setup**, and
   open **Counter camera**. It opens the back camera and keeps the screen on; pick another camera in
   Camera setup if needed (the choice is remembered). Then calibrate and mark the mat from the phone.

**The camera, on any phone or webcam.** Both camera pages open the camera's largest picture in its
own shape (4:3, 16:9, or portrait on a phone held upright), and the viewfinder takes that shape, so
nothing is stretched or cut off. Camera setup shows what it got ("Camera picture 3840×2160 (16:9)").
Photos are *sent* at most 1920 pixels on the long side: the recognizer works at 960 (and 224 per
product), so bigger would only make every photo slower to send.
- **Zoom:** pinch the picture, tap **1× / 2× / 3×** on it, or pinch a laptop trackpad. Phones whose
  browser offers the camera's own zoom (most Android phones) use it, with a below-1× button if the
  camera reports one; elsewhere it is a digital zoom (the middle of the picture, enlarged). A phone's
  extra lenses (an iPhone's ultra wide) show up as cameras of their own under **switch camera**. What is sent
  is exactly what the viewfinder shows, and the zoom is remembered per camera.
- **Counter camera:** the zoom buttons appear only while **Camera setup** is open, so a stray touch
  can't reframe the counter. Changing the zoom (or the camera) after calibrating pauses capture and
  asks you to calibrate again, since the empty-mat photo no longer matches.
- **Product photos** work like the phone's camera app: the shutter in the middle (tap for one, hold
  for many), the last photo and the count on the left, **switch camera** on the right, **flash** and
  **full screen** on top where the phone allows them, tap the picture to focus, and a white blink for
  each photo. Photos from a phone that isn't the counter camera (or a zoomed-in one) are cut out from
  their own background: put the item on a plain surface. A photo in which nothing stands out is
  refused, never learned.

Keep the phone plugged in, on a stand looking straight down at the mat, with LuyDek in front: the
camera stops if the phone locks or another app is opened, and comes back when LuyDek is shown again.
Don't use `tailscale funnel`: that would put LuyDek on the open internet, and it has no login yet.

**Looks ("Night market").** One palette: night violet for the frame (sidebar, the Sales tile) and
lamp amber for the one thing to press (confirm, *All correct*); violet solid buttons save, outlines do
everything else, and green/red only ever mean OK/problem. The colour tokens sit at the top of
`web/static/app.css`. Text is Inter, with Kantumruy Pro for Khmer.
- **Works offline.** Bootstrap 5.3, Bootstrap Icons and both fonts are stored in `web/static/vendor/`
  (licences alongside), so the look never depends on the internet.
- **Brand.** `scripts/make_brand.py` draws the mark (an amber coin with a violet crescent: money that
  sleeps) and writes `web/static/brand/`: the logo, favicons and the phone home-screen icons used by
  `manifest.webmanifest`.
- **Product photos.** Lists, pickers and the check cards show each product cut out onto white, like a
  shop catalogue (`recognition/cutout.py`, made once per photo under `media/thumbs/`). A cut is used
  only if it is one solid piece whose outline follows real edges in the photo; otherwise the next photo
  of that product is tried, then the plain photo. Wide shots with the table in them stay plain, so
  retake those on the mat for a cleaner look.
- **Charts.** Inline SVG, no chart library (`web/charts.py`): the shop's last 14 days in the Sales
  tile, a small strip per product in the stock table, busy hours (sales per hour on a usual day) on
  Today, and daily sales on each product page. Every bar has a hover/tap tooltip.
- Empty lists say what to do next, and slow buttons (reading an invoice, saving counts) show a spinner
  and what is happening: give a form `data-busy="…"`.

**Owner mode.** `shop.team_tools` in `configs/config.yaml` (on for the demo) shows the tools for the
install team and judges: the Demo menu of send-now buttons, the photo upload box, the pilot report
and the Telegram previews on Order lists. Switch it off for a real pilot so the owner sees only:
**Today** (items to check first, with one **All correct** button; then stock alerts and agent visits),
**Order lists**, **Deliveries**, **Products**, **Counter camera** and **Setup**. On a phone, a bottom
bar keeps Today · Check · Orders · More one thumb away.

### When the camera isn't sure: nothing is lost

- **Asked, not dropped.** Things you taught it to ignore (phone, cash, a hand) are set aside only on
  a clear match. Anything that looks like "other" clutter is asked about ("It looked like clutter to
  the camera. If it's a product, say which"), because clutter is sometimes a product. The answer
  teaches it. Only a patch of mat nearly identical to one you already said wasn't a sale is set aside.
- **The mat itself.** Every item is compared with the same spot of the empty-mat photo. One that
  looks like the mat (a shadow, a paper that moved, a band of light) is never recorded without
  asking, so a paper strip can't become a "pen" sale.
- **Several items in one photo.** Items the camera sees as one outline are split when they were
  apart, even by a finger's width. Pieces that match different products become separate items;
  pieces of one product (a pen's cap and tip) are put back together. Items that really touch still
  come as one photo: answer **Several items** and pick each one.
- **Camera log** (`/log`, in the menu): every sale of a day, each item with the photo the camera saw,
  how it was settled and by whom, and everything it set aside. **Fix** corrects a recorded sale
  (another product, the count, not a sale): the stock and the camera are both corrected. **It was a
  sale** turns something set aside into a sale, dated when it happened. Today says how many things
  were set aside, so they are never out of sight.
- **A product's history** (its page, *Full history*): every time the camera saw it or thought it
  did, and every stock change with the level it left.
- **Why isn't it capturing?** Under the counter camera, a live line says how much of the mat differs
  from the empty photo, how much is moving, and how long the last photo took to recognise. When the
  mat has been empty and still for 30 s but the light has drifted, the empty-mat photo refreshes
  itself (only if nothing item-sized is on the mat), so slow light changes stop causing false items.

### Telegram Mini App: swipe to check

With `telegram.miniapp_url` set to an https address (the Tailscale one from *Phone as the counter
camera*), each digest is one message with **Check N items**, and the chat's menu button opens the
app. `/check` opens it too.
- **Cards:** the photo the camera saw, its guess and the count. **Swipe right** (or the tick) if it
  is right, **left** (or the cross) to correct it. **Undo** takes the last answer back.
- **The correction sheet:** the camera's other guesses as photo tiles, a search of every product,
  the count, **Several items**, **New product** (the name read off the package is offered first),
  **Not a sale** and **Not now**.
- **Recorded today:** tap any sale to fix it, as in the camera log.
- **Stock:** today's sales in numbers, then every product with what is left, what sold today and
  about how many days it lasts. Filter to **Low or out** or **Sold today**, search by name, and tap a
  product for its 7- and 28-day sales, earliest expiry and agent. The tab's badge counts the products
  running low or out.
- It follows Telegram's light or dark theme, answers survive bad signal (sent when it's back), and
  only phones registered with the shop PIN get in: every request carries Telegram's signed launch
  data, checked against the bot token, and photos load through links signed for a few hours.
- On a *public* address (a tunnel) set `miniapp_host_only: true`: that address then serves only the
  Mini App, since the dashboard has no login yet.

### Today's summary and stock left

- **Every evening,** right after `end_of_day` (22:00) settles the unchecked guesses, every registered
  phone gets the day in one message: sales and items sold, what came in, the products sold with what
  is left of each, and what is running low or out (must-have items first). Turn it off with
  `verification.daily_summary: false`.
- **On request:** tap **📈 Sold today** or send `/today` for the same message at any time. Items still
  waiting to be checked are counted separately, not guessed.
- **Stock left:** tap **📋 Stock left** or send `/stock` for a list of products, each showing its count
  (best sellers first). Tap one for its detail, or **Every item** for the whole list, grouped out /
  running low / expiring / not counted / in stock (split over several messages if it's long). Or just
  type a name: `sprite` answers with Sprite's stock, `coca` offers both Coca-Colas.
- With a Mini App address, the summary also has a button that opens the Mini App's **Stock** tab.
- Typing `/` in the chat lists these commands, in Khmer on a Khmer phone.

## How it works

```
counter photo ─► segment inside the marked mat (vs. empty-mat photo; camera moved? → recalibrate)
             ─► split touching items ─► barcode pass (zxing) ─► CLIP ViT-B/32 embedding
             ─► nearest packs in this store's gallery ─► colour tie-break for look-alikes ─► multipack × units ─► tier
   high  (barcode, or score ≥ t_high AND margin to runner-up ≥ m_high) → sale recorded now
   medium (score ≥ t_low) → label reader if look-alikes → "Is this X?"   low → photo grid   ignore → dropped
```

- **Tiers use the margin as well as the score.** Raw CLIP similarities sit in a narrow 0.75–0.95
  band, so the handoff's 0.90/0.60 can't be used as-is. Look-alikes both score high, and it is the
  small gap between them that sends an item to the owner.
- **Mat area and a changed scene.** Only the marked mat is searched, and item sizes are shares of
  the mat. If 85% or more of it differs from the empty-counter photo (`scene_change_frac`), the
  camera moved or the light changed: nothing is recorded, a red banner asks for a recalibration and
  automatic capture pauses. Real sales in the first test covered up to ~70% of the view, hence 85%.
- **Colour tie-break.** CLIP pays little attention to colour, so a blue and a black pen score
  alike. When the top candidates are within 0.05 of each other, a colour histogram of the product
  (black, greys, and 12 hues pale or vivid, with the mat around it left out) picks one if it beats
  the runner-up by 0.15 (method `colour`). The tier then uses that product's own score and its lead
  over everything outside the tie, so colour decides *which* look-alike, never *whether* the item is
  known.
- **Learning from answers.** A "Yes" or a correction adds that crop to the gallery; "not an item"
  teaches the ignore class. The newest 20 learned photos per product are kept
  (`recognition.learn_cap`), so the gallery follows today's packaging and light. Enrollment photos
  are never dropped, and end-of-day best guesses teach nothing.
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
  services/     catalog · stock (ledger) · transactions · invoices (Claude) · learn (new products from
                sales) · forecast · metrics · report
  recognition/  segment · barcode · encoder (CLIP) · matcher (gallery + tiers) · labels (Claude: labels
                and new-product names) · tray · cutout (clean product photos) · pipeline
  bot/          app (jobs) · handlers · digest (outgoing messages) · keyboards
  web/          server (FastAPI; the bot runs in its event loop) · charts (inline SVG) · templates · static
  i18n.py       Khmer/English strings    render.py  owner-facing message text
scripts/        seed_demo · simulate_history · calibrate · synthetic_demo · sale_from_photo · counter_test
                · make_brand (logo and icons)
configs/        config.yaml (thresholds, digest times, forecast rules) · synthetic.yaml
```

`.venv/Scripts/python.exe -m pytest` runs 211 tests covering the ledger, forecast math, recognition,
touching-item splitting, the tablet tray, the label reader and invoices (mocked Claude), the pilot
report, transactions, bot flows (fake Telegram) including the day's summary and stock left, and the web app, including its look (offline assets,
cut-out photos, charts). The tests use a colour-histogram encoder, so they don't need CLIP.

## Not built yet (and what to say if asked)

- **Detector.** Segmentation against the empty mat stands in for a trained detector. Touching
  items are split when their outline has a neck, but boxes pushed flush edge to edge still merge,
  and pale boxes on a pale mat are only partly seen. A YOLO detector slots in behind
  `segment.segment()`.
- **Loose tablets.** The tray learns one tablet size per medicine, so a medicine sold both loose and
  as cut blister pieces (foil around each tablet makes a piece bigger) goes to the owner more often.
  Two different medicines on the tray at once can't be split between them: the owner checks. Not built
  yet: 1–10 count buttons in Telegram (it uses −/+), a weekly photo count of the open strips, and a
  pocket scale under the tray to count by weight.
- **Label reading offline.** The label reader needs the internet. Without it, look-alikes go to
  the owner as before. A local OCR model is the offline alternative.
- **Multiple stores.** It is single-store (one database per shop). There is no weekly shelf-photo
  spot check yet, no Croston's method or seasonality, and no warm start from distributor history.
- **Khmer strings.** `luydek/i18n.py` was written by a non-native author. Have a native speaker
  proofread it before a pilot.
