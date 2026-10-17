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
| + The shelf by batch | `/shelf`, `/stock-in`, Telegram: expiry, places, put-away, quick counts, where stock came from |

## Using it on a phone

The web app is built for the owner's phone first, in Khmer (one tap switches to English; each phone
keeps its choice). Four places and one button:

| | |
|---|---|
| **Home** (`/`) | What the stock is worth, and **To do now**: one row per thing to do, most urgent first: sales to check, stock past its date to take off, what is out, agents coming today or tomorrow, what to put in front or sell soon, what is running low. Each row opens the place to do it. |
| **Stock** (`/stock`) | Search first, then *Needs you* / *All* / *Out* / *Expiring*. Each product shows how many are left, its status and its first date; tap it for the product: the count, **Count** and **Sold or used**, every delivery on the shelf with its own date, and (folded) sales, the camera's photos and the details. |
| **+** | *What happened?* **Stock came in** (photo of the invoice), **Sold or used** (credit, family, damaged…), **Count a shelf** (one product at a time, then the next), **New product**, or **Count with the camera**. |
| **Orders** (`/orders`) | The agents in the order they come, each with what to buy, what to skip and why. |
| **More** (`/more`) | Shelf check, sales and the camera, deliveries, the stock map, products and suppliers, Stock in Excel, setup, help, language. |

**Help** (`/help`, or the **?** on every page) explains the jobs in a few steps and what the words
mean (past its date, won't sell in time, sleeping money…). On a laptop the same places sit in a rail
on the left. The look and its rules are in [`docs/design/DESIGN.md`](docs/design/DESIGN.md), with the
three concept screens it was built from; every Khmer phrase is listed in
[`docs/design/khmer-to-check.md`](docs/design/khmer-to-check.md) for a native speaker to check. A shop's
default language is `shop.web_lang` in its config (`km` or `en`).

---

## Setup (Windows, ~2 minutes; CLIP weights come from the local HuggingFace cache)

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/Scripts/python.exe -r requirements.txt
mkdir -p models && curl -L -o models/yoloe-26s-seg-pf.pt https://github.com/ultralytics/assets/releases/download/v8.4.0/yoloe-26s-seg-pf.pt
cp .env.example .env            # then fill in the two keys below
.venv/Scripts/python.exe scripts/seed_demo.py --reset
.venv/Scripts/python.exe scripts/simulate_history.py --weeks 8
.venv/Scripts/python.exe -m luydek
```

The detector weights (40 MB, AGPL-3.0, see *How it works*) are optional: without the file, items
are found by the mat difference alone, as before.

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

1. **Live sale.** On `/counter`, a teammate puts 2–3 items on the mat. Each item gets a beep and a
   "+1" with its photo: a high beep when the camera knows it, a low double beep when it will ask.
   Take the items off: a chime, and a **Sold** card lists what was sold, recorded or to be checked.
   The counter camera sends full HD (1920×1080) when it has it; below HD (720 pixels on the short
   side) the Counter page says so and why, usually a digital zoom cropping the picture: zoom out and
   bring the camera closer instead. Items close together need the pixels.
   It stays until the next item is put down (or it is tapped), and every sale stays in *Sales just
   recorded*. Browsers only play sound after the page has been tapped once: **Tap to turn sound on**
   in the Live view header (it remembers **Sound off** too).
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

**The demo shop: Sokha Mini-Mart.** `scripts/demo_shop.py` builds a whole shop as it would look after eight
weeks on LuyDek, with every feature in use. It is made up: real products, simulated history, and the app
says so. Start it with `python -m luydek --config configs/demo.yaml --port 8030` (or `demo-shop` in
`.claude/launch.json`).

- **The shop:** 45 real products sold in Phnom Penh mini-marts (Angkor and Cambodia beer, Vital water, Sting,
  Coca-Cola, Dutch Lady, Milo, Lay's, Oreo, Mama, Nescafé, Colgate, paracetamol by the tablet, Dumex…) with
  their real barcodes where known. They come from six supplier agents and stand on eight places: two
  fridges, three shelves, a medicine cabinet, the counter and the back room, arranged on the floor for the
  3D view.
- **Eight weeks of life:** onboarding from the owner's spreadsheet (every shelf, level by level, and an
  opening count with dates). Then a confirmed invoice at every agent's visit, with lots, expiry dates and
  bonus cases, and one new product added from an invoice and not put away yet. Daily sales, a credit sale,
  breakage, family use, quick counts, and expired Choco Pie taken off at the shelf check.
- **The stories:**
  - Dumex out after a forgotten order: an alert and a gap.
  - A dead green tea and other sleeping money.
  - Strawberry milk past its date still on the shelf.
  - Milo UHT to put in front.
  - Pocari Sweat over-ordered that won't sell in time.
  - Sting at the bottom of the fridge: the suggested move.
  - A lot that doesn't match its invoice.
- **The last two days at the counter:** the counter photos are product photos laid on the real counter
  mat, and they go through the real camera pipeline. What the camera was sure of and what it asked about
  is what it really did with them. The owners answered most questions on Telegram, the end of the day
  turned the rest into best guesses, and this morning's are still waiting. Mama noodles were learned
  while selling.

It needs `data/demo/assets` (not in git): `photos/<sku>.jpg` from Open Food Facts and Wikimedia Commons
(CC BY-SA; see `credits.json`), `cut/` (product cut-outs for the counter photos) and `mat.jpg`.

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
and the Telegram previews on Order lists. Switch it off for a real pilot so the owner sees only the
stock pages (**Today**, **Stock map**, **Shelf check**, **Stock in**, **Deliveries**, **Order lists**,
**Products**), then **Counter camera** and **Camera log** under *Selling*, and **Setup**. On a phone, a
bottom bar keeps Today · Stock map · Orders · More one thumb away.

**Today** leads with the stock:
- **Four numbers:** what it is worth at cost, what is out or running low, how many units are past
  their date (and how many expire within 30 days), and the money sleeping.
- **What to do with the stock,** batch by batch, each with its own date and place. For example,
  *Dutch Lady strawberry: 25 of the 77, expired 05/10, Drinks fridge, level 3* means take those 25
  off, not all 77. Also listed: what to put in front, what won't sell in time, what to order, what is
  running low and what isn't put away yet. Each line opens the shelf it is on.
- **Next agent visits** and the **money sleeping**.
- **The stock table,** with every date on the shelf as its own chip (*25 · 05/10*, *4 · 10/01/27*,
  *48 · 10/02/27*) and where it is.

The counter camera's questions come after the stock, under *From the counter camera*; a slim bar at
the top says when some are waiting.

**One product, several dates.** Each delivery or count is its own batch, with its own date, lot and
place. Sales take the one that expires first. The shelf check, Today, the stock map and the 3D shop
show each batch on its own, never one date for all of a product:
- **Stock map:** a spot shows a strip split by batch and the part that needs doing (*×77 · 25
  expired*).
- **3D shop:** coloured by *What to do* or *Days to expiry*, a product's box is split by batch.
- **Panels and tooltips:** they list every date.

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

## The shelf: batches, expiry dates and places

Stock is kept **by batch**. Everything that comes in (a delivery, a free bonus, a count that found
more) is a batch with its own expiry date, lot number and place in the shop. Nothing stores what
is left of a batch: it is worked out by replaying the ledger. A sale takes from the batch that
expires first, which is how the shop is told to shelve ("first expired, first out"); the weekly
walk checks that this is true.

- **Places.** On **Shelf check**, name the shop's places in walking order (Counter, Shelf A, Fridge…,
  or one tap for the usual ones) and pick where each product lives. New stock goes where its
  product lives unless told otherwise.
- **Put-away.** When a delivery is confirmed (Telegram or the invoice page) or saved on **Stock in**,
  LuyDek says where each part goes: *Dutch Lady ×24 (exp 20/01/27) → Fridge, behind the 6 already
  there (exp 12/10/26)*, or **in front** when the new one expires sooner.
- **The weekly shelf check.** Every week (Monday 08:30, or `/shelf` and **🗓 Shelf check** any time)
  every phone gets the walk, place by place: **take off** what has expired (one tap: *Removed*),
  **put in front** what expires within 30 days, and **won't sell in time**: what, at its pace,
  will still be there on its date (discount it, bundle it, send it back). The same list is on
  `/shelf` and in the Mini App (*Expiring*). Only batches with a printed date are on it.
- **Out of stock, at once.** A product that runs out or runs low is announced to every phone the
  moment it happens, once, until it is back.
- **Quick counts.** Every afternoon (15:00) three products get a one-tap count: *LuyDek has 4. How
  many are on the shelf?* `[3] [4] [5] [✏️ Another number]`. The ones about to be reordered or
  already out come first, then the best sellers, never the same one twice in a week. A count adds
  the sales still waiting to be checked, so nothing is taken off twice. Every count is logged,
  the ones that matched too.

### The stock map (`/map`)

Each shelf face on, the way the owner sees it in the shop: its levels top to bottom, each product in
its spot left to right (as wide as its facings). Each spot shows how many are left and a strip of its
batches (red past its date, amber expiring soon, green fine, grey no date), with the part that needs
doing in words. The panel beside the shelf is a small stock table with every date on it. One shelf at a time, like slides: the arrows, the strip of shelf names, the arrow keys, or a
swipe on a phone. Fridges, the counter and the back room are drawn as what they are.

- **Find anything:** type a product or a lot number. Its spots light up, the map goes to the first
  shelf with one, the strip says how many are on each shelf, and the panel says where each one is
  (*Shelf A · level 2 of 4 · 3rd from the left*), with its batches, dates, lots and value, and
  **Taken off** / **Moved to**.
- **Show only:** expired, expiring within 30 days, won't sell in time, sleeping money, out of stock,
  no place yet. The chips say how many (or how much money).
- **Layers:** *Expiry* (red: expired, amber: expires soon or won't sell in time, green: fine),
  *Dead stock* (darker violet: more money sleeping at cost: no sale in 45 days, or months of stock
  at today's pace, the order lists' rule) and *Stock*. A product out of stock is a dashed gap in its
  spot.
- **Today's walk:** the shelf check shelf by shelf, with the job on each product (*Take off ×6*,
  *Put in front*), and **Next stop**.
- **Edit this shelf:** drag products along a level or to another one, **+** adds one, tap one to move
  it, make it wider or take it off; change the levels, the name, what it is, its place in the walk.

**New shelf**, three ways:

- **From a photo:** one photo of the whole shelf, straight on. The item finder (YOLOE) finds each
  product, CLIP says which it looks like, and the levels come from where their bottoms line up.
  Neighbours that are the same product are one spot, as wide as there are of them. Anything it
  isn't sure of is marked **?** and must be answered (the closest products, another, a new one by
  name, or *Not a product*) before saving; nothing is dropped on its own. A product with fewer than
  5 photos learns from its photo off the shelf (a new one gets its first). Needs the item finder's
  weights in `models/`.
- **From a spreadsheet:** the owner's own list, `.xlsx` or `.csv`, read with no spreadsheet package
  (an old `.xls` must be saved as `.xlsx` first). The columns are guessed from their headings, in
  English or Khmer (*Item*, *Qty*, *Exp*, *ធ្នើ*, *ចំនួន*…), and can be changed. Each product is matched
  to the list by barcode, then by a name it is known by, then the closest name (a guess to check);
  the rest become new products unless the owner picks one, or leaves the row out. Dates as shops
  write them: *31/12/2026*, *12/2026*, *Dec 2026*, Excel dates. An empty shelf cell means the shelf
  above (merged cells); with no level column, a shelf's products are shared out over its levels.
  **Count the stock** sets what is on each shelf in the sheet to the sheet's quantities, as batches
  with their dates; stock on shelves the sheet doesn't mention is kept, so a sheet of the fridge is
  a count of the fridge.
- **Empty:** a name, what it is and how many levels; then fill it in with **+**.

**Shelves as a spreadsheet** (`/map/shelves.csv`) gives every product back as a sheet: a row per
batch on each shelf (level, position, facings, quantity, expiry, lot), the products on no shelf
first. Fill it in or correct it in Excel and bring it back: the same sheet makes the same shelves.

**3D shop** (the switch at the top of `/map`) shows the whole shop the way a warehouse visualisation
shows a warehouse:

- **Racks and boxes:** every place is a rack on the shop floor, drawn from its levels and what stands
  on them. Every product is a box in its spot, coloured by one measure: what to do (expired,
  expires soon, won't sell in time, sleeping, fine), days to the first expiry, units on the shelf,
  money on the shelf, sleeping money, sales in the last 4 weeks, or days since the last sale. Pick
  the colours, the spread (even, square root, log) and turn them round; a legend explains them. An
  out-of-stock product is a see-through gap. Stock on no level sits in a crate in front of its rack,
  and stock with no place on a pallet.
- **Moving around:** drag to turn, scroll or pinch to zoom, and right-drag (or two fingers) to move.
  Hover or tap a box for what it is and where. The panel lists the boxes that need attention first,
  and tapping a rack's name opens it face on. Photos puts each product's photo on its box. Search
  and the filters dim everything else.
- **Arrange the shop:** racks start out laid out by what they are (fridges and the back room along
  the back, shelves in rows of four, the counter at the front). Drag them to where they really
  stand, turn them, and save; *Lay them out on their own* goes back.
- **Suggested move:** the shop's version of re-slotting. A best seller in a spot that is hard to
  reach (low, high, or far from the counter) swaps with a slow seller at hand height near the
  counter, drawn as two animated arcs. It only appears when the ledger's sales are clear enough: at
  least 8 sold in 4 weeks, and 3 times what the other sold. Fridge stock only swaps with fridge
  stock.

It is drawn on a canvas with a small 3D camera of its own, so no 3D library is downloaded and the
page works offline like the rest of LuyDek.

### Bringing stock in (`/stock-in`)

The camera does the counting, for a shop's first day and for any delivery without an invoice.

1. Put a few items on the mat, a little apart, and press **Scan**. Take them off, put the next
   ones on, scan again: each scan adds to one line per product.
2. Something the camera doesn't know comes grouped by look ("Not known yet ×12"), so a dozen of
   something new is one question: pick it from the closest products, type its name, or **Read the
   name** off the pack (Claude). Its photos teach the camera on the spot, so the next scan counts
   it by itself.
3. For dates, press 📅 on a line and hold the date stamp up to the camera (Claude reads EXP vs MFG,
   day-first dates and Thai Buddhist years, and the lot number), or type it. **Another date** splits
   a line's units into batches.
4. Pick the place, then **Add to stock** (a delivery: new batches and the put-away list) or, in
   **Count the shelf**, **Save the count** (the shelf is set to exactly what was counted, batch
   by batch with its dates).

After an invoice is confirmed in Telegram, each product that came with no date and whose shelf life
isn't known gets one question: reply with a photo of the date, or tap how long it keeps
(3 months / 6 months / a year / doesn't expire); it isn't asked again. The confirmed invoice page has
a date field and a **Photo of the date** button for every undated batch.

### Stock in Excel (`/spreadsheet`)

The whole stock in one Excel file, to look at, count on paper or on a laptop, and send back.
**Download** gives `luydek-stock-<date>.xlsx` with four sheets:

- **Summary:** what the stock is worth at cost, what is out, low, past its date or sleeping, by place
  and by supplier.
- **Stock:** a row per product: on hand, worth, status, sales over 7 days and 4 weeks, days of stock
  left, and every date on the shelf. The yellow columns (name, supplier, unit, case size, cost,
  barcode, must-have, how long it keeps) can be edited; a row with no ID at the bottom adds a product.
- **Count:** a row per batch (product, place, level, lot, expiry, what the books say) with an empty
  yellow **Counted** column. Write what you find: empty means *not counted, keep it*, **0** means
  *none were there*. Fix a batch's place, lot or date on its row; add rows at the bottom for stock
  LuyDek doesn't know about (product, place, expiry, how many).
- **How to use:** the same rules, in the file.

**Upload** it back on the same page. Nothing is saved until you have seen the list of what it
changes: each count (before, after, the dates that will be left), each corrected batch, each product
detail old → new, each new product. Rows it can't read (a word in **Counted**, an unknown place or
product) are listed with their sheet and row, and nothing is saved until they are fixed. Counts work
like a count with the camera: per product and place, batch by batch with their dates, so a shelf
counted in Excel leaves the back room alone. Products sold since the file was downloaded are pointed
out (a count made before those sales would put them back), batches that came in after the download
are left as they are, and a file can be used only once.

The file is written and read without Excel or any extra library (`services/xlsx.py`); it was checked
by opening, editing and saving it in Excel itself.

### How sure is the camera?

- **Size on the mat.** From the calibrated counter camera every product photo also records its size.
  A 330ml can and a 1.5L bottle of the same brand look alike, but not at the same size: an item that
  isn't the size of its best match is asked about, and a close runner-up of the right size becomes
  the guess (`recognition.size_check`).
- **Look-alike pairs.** Products the camera mixes up are found from the shop's own photos (each
  matched against all the others) and listed on **Products** and on the product's photo page, with
  what to do: photograph the side where they differ. When the top two guesses are such a pair, the
  item is asked about (and the label reader tries first) unless colour or a clear lead settled it.
- **The accuracy test** (`/accuracy`, under team tools). Put a known product on the mat, say which
  it is, press **Test**, in daylight, under the evening lamp and in shadow. Per light: right, asked,
  and **wrong on its own**: a wrong sale recorded with nobody asked, the number that matters.
- **Copies of a brand** can be the same colour and design as the real thing, so the camera isn't the
  check: the paperwork is. **Where did it come from?** on Shelf check (and in the weekly shelf
  check) lists products that sold more than any delivery brought in (a later count found them, or
  nothing has covered them yet; late paperwork is not flagged), and packs whose lot, read from a
  photo of the date stamp, isn't the one on their invoice. Invoices are read for lot numbers too.

## How it works

```
counter photo ─► inside the marked mat: YOLOE-26 finds each item, the empty-mat photo confirms it
                 (camera moved? → recalibrate); what no box covers is found by the mat difference
             ─► barcode pass (zxing) ─► CLIP ViT-B/32 embedding
             ─► nearest packs in this store's gallery ─► colour tie-break for look-alikes ─► multipack × units ─► tier
   high  (barcode, or score ≥ t_high AND margin to runner-up ≥ m_high) → sale recorded now
   medium (score ≥ t_low) → label reader if look-alikes → "Is this X?"   low → photo grid   ignore → dropped
```

- **Finding items: YOLOE-26 and the mat.** The detector (Ultralytics YOLO26, open vocabulary,
  prompt-free, `models/yoloe-26s-seg-pf.pt`) sees each object, so cans standing side by side are
  separate items, and items stand out off the counter camera too. Its own names for things are no
  use (a Sting can is a "zombie" to it): CLIP and the shop's photos still say which product it is,
  so adding a product is still a few photos with no training. What it finds is tidied by each
  object's outline (the model draws one around everything), not its box, because boxes of items
  close together overlap even when the items don't: only on the mat and off the tablet tray; no
  specks and nothing the size of the whole view; an object within a bigger one's outline (dents
  filled in, since the model may cut a picture printed on a bag out of the bag's outline) is a part
  of it, unless the bigger one is a less sure outline around a group of items; and on the counter
  camera only where the empty-mat photo shows something is there under the outline. Each outline is
  its biggest piece: a stray fleck on a neighbour would otherwise swallow the neighbour. On composed
  scenes with small items tucked against bottles and cans lying at an angle, outlines got 13 of 13
  right where boxes got 8; on the pilot counter's photos, 2 false items instead of 4. The detector
  looks at photos at 960 pixels: 1280 did worse (29 of 44 against 42), 640 is faster and one worse. Whatever the
  mat difference finds that no box covers is still an item, so nothing is lost when the detector
  doesn't see an object. On composed test scenes (real product photos on the real mat, not camera
  photos) it found 35 of 37 items against 27 for the mat difference alone; the two it missed were
  items lying on top of another. It adds about 0.4 s per photo on this laptop's CPU. The Counter page
  says which is in use. Ultralytics YOLO is AGPL-3.0: offering LuyDek to shops as a service means
  publishing its source, or an Ultralytics Enterprise licence.
- **Shadows are not items.** A patch of mat that is only darker or lighter than the empty-mat
  photo, by about the same amount all over (smooth inside, soft at its edge) and in the mat's own
  colour, is a shadow or a change in the light, and is set aside before anything else
  (`segment.shadow_filter`). An item has edges or print inside it, or a sharp outline, so even a
  plain dark-grey box stays an item. On 13 photos from the pilot counter, the mat difference took
  shadows and light patches for 11 items; with the filter, 2 (a cable moving at the edge of the mat),
  with no item lost. With the detector the false items went from 17 to 4. The shadow an item casts
  beside it no longer comes back as a second item either.
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
  services/     catalog · stock (ledger) · batches (expiry, put-away, shelf check) · locations · alerts
                (out of stock, quick counts) · stockin · sources (where stock came from) · accuracy ·
                transactions · invoices (Claude) · learn (new products from sales) · forecast · metrics · report
                · stockmap + shop3d (the stock map, face on and in 3D) · shelfsheet (shelves from a sheet)
                · workbook + xlsx (Stock in Excel: the workbook out and back in)
  recognition/  segment · detector (YOLOE-26) · barcode · encoder (CLIP) · matcher (gallery + tiers) · labels
                (Claude: labels, new-product names, expiry dates) · tray · cutout (clean product photos) · pipeline
  bot/          app (jobs) · handlers · digest (outgoing messages) · keyboards
  web/          server (FastAPI; the bot runs in its event loop) · charts (inline SVG) · templates · static
  i18n.py       Khmer/English strings    render.py  owner-facing message text
scripts/        seed_demo · simulate_history · calibrate · synthetic_demo · demo_shop · sale_from_photo · counter_test
                · build_detector_kit + detector_kit/ (training scenes and the Colab notebook)
                · make_brand (logo and icons)
configs/        config.yaml (thresholds, digest times, forecast rules) · synthetic.yaml · demo.yaml
```

`.venv/Scripts/python.exe -m pytest` runs the tests: the ledger, batches (expiry order, put-away, the
shelf check), forecast math, recognition (size on the mat, look-alike pairs, shadows, the detector's
outlines with a fake detector), touching-item splitting, the tablet tray, the label, date and invoice
readers (mocked Claude), stock in, Stock in Excel (out, edited, back in, used once), the source checks, training scenes, the pilot report, transactions,
bot flows (fake Telegram) including the day's summary, quick counts and expiry questions, and the web
app, including its look (offline assets, cut-out photos, charts). The tests use a colour-histogram
encoder and no detector, so they need neither CLIP nor the YOLO weights.

## Training the item detector (fine-tuning YOLO26 on this shop)

YOLOE-26 is a general model that knows 4,585 kinds of things. A model fine-tuned to one class, "any
product on this shop's mat", can be faster (YOLO26n: 39 ms a photo on CPU in Ultralytics' own
benchmark) and better at items close together. It still doesn't name products, so adding a product
never needs retraining.

1. **Build the kit** from the shop's own photos (read-only):
   ```bash
   .venv/Scripts/python.exe scripts/build_detector_kit.py
   ```
   Look over `training/backgrounds_sheet.jpg` and `training/cutouts_sheet.jpg`, then build it again
   leaving out, by their numbers, empty-mat photos with a person, a hand or an object in them
   (`--skip 3 73 ...`) and cut-outs that took a slab of background with them (`--skip-cutouts 15 ...`).
   An object left in a background teaches the detector that items are background.
2. **Train on Colab:** open `training/item-detector-kit/train.ipynb` in Google Colab, choose a T4 GPU,
   *Run all*, and upload `training/item-detector-kit.zip` when asked. It makes 3,000 training scenes
   (the shop's products, cut out of their photos, on photos of its empty mat: touching, overlapping,
   in changing light), trains `yolo26n-seg` and downloads `item-detector.pt`. Only product and
   empty-mat photos go to Google, no sales photos.
3. **Compare before switching:** put `item-detector.pt` in `models/` and compare it with the current
   detector on real counter photos. Only if it is better: `detector.weights: models/item-detector.pt`
   and `detector.imgsz: 640` in `configs/config.yaml`.

Scenes made from cut-outs never look quite like the real camera: about 150 real HD sales photos,
labelled, close that gap (not built yet: a page to correct the detector's outlines on them).

## Not built yet (and what to say if asked)

- **Detector.** YOLOE-26 is used as it comes, not trained on this shop's products. An item lying on
  top of another, or two identical sachets overlapping, can still be one item (the owner fixes the
  count with −/+). Enrollment photos are still cut out by the mat difference. The detector has only
  been tried on composed scenes so far, not on photos from the counter camera.
- **Loose tablets.** The tray learns one tablet size per medicine, so a medicine sold both loose and
  as cut blister pieces (foil around each tablet makes a piece bigger) goes to the owner more often.
  Two different medicines on the tray at once can't be split between them: the owner checks. Not built
  yet: 1–10 count buttons in Telegram (it uses −/+), a weekly photo count of the open strips, and a
  pocket scale under the tray to count by weight.
- **Label reading offline.** The label reader needs the internet. Without it, look-alikes go to
  the owner as before. A local OCR model is the offline alternative.
- **Multiple stores.** It is single-store (one database per shop). Counts are typed (quick counts,
  stock-in on the mat), not read from a photo of the shelf; there is no Croston's method or
  seasonality, and no warm start from distributor history.
- **Which batch a sale came from.** The camera sees the product, not its date, so sales are assumed
  to come from the batch that expires first; the weekly shelf check is where that is put right.
- **Copies of a brand.** They are not told apart by the camera. The defence is the paperwork: stock
  that came in on no record, and lots that don't match their invoice (see *How sure is the camera?*).
- **Khmer strings.** `luydek/i18n.py` was written by a non-native author. Have a native speaker
  proofread it before a pilot.
