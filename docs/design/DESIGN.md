# Design System: LuyDek (លុយដេក)

The look and the rules for every LuyDek screen. The concepts in this folder (`concept-1-home.png`,
`concept-2-stock.png`, `concept-3-what-happened.png`) show it on a phone.

## 1. Visual Theme & Atmosphere

A calm shop ledger on a phone. Warm off-white paper, white rows on hairlines, charcoal ink, one black for
the thing to do next. Colour is never decoration: it only says *past its date*, *running low*, *fine*,
*sleeping money*. The owner is busy, standing behind a counter, often one-handed, often reading Khmer;
every screen answers one question and offers one obvious next tap.

- **Density:** Daily App Balanced (4). Lists are roomy (one product per 72 px row), never cramped.
- **Variance:** Predictable Symmetric (3). The same row, the same sheet, the same button everywhere, so
  nothing has to be learned twice.
- **Motion:** Static Restrained (3). Things move only to show a change: the sheet rises, a done task
  slides away, a tap presses in. Nothing loops.

## 2. Color Palette & Roles

- **Paper** (#FBFBFA) - the page.
- **Shelf White** (#FFFFFF) - rows, cards, sheets.
- **Hairline** (#EAEAEA) - every border and divider, 1 px.
- **Ink** (#111111) - the primary button, the selected tab, the + button. The one strong colour.
- **Charcoal** (#2F3437) - body text.
- **Stone** (#6F6E69) - secondary text (5.3:1 on Paper; the old #787774 was too light for small text).
- **Status pastels**, used only for meaning, always with words, never colour alone:
  - **Past its date / out** - Pale Red (#FDEBEC), text Brick (#9F2F2D)
  - **Soon / low / check** - Pale Yellow (#FBF3DB), text Ochre (#8A5C00)
  - **Fine / done** - Pale Green (#EDF3EC), text Leaf (#346538)
  - **Sleeping money / information** - Pale Blue (#E1F3FE), text Lake (#1F6C9F)
- **Banned:** purple, gradients on surfaces, neon, pure black (#000), any second accent.

## 3. Typography Rules

- **UI and titles:** Geist (stored in the app). Titles 600-650 weight, tight tracking (-0.02em), never
  bigger than 28 px on a phone. Hierarchy comes from weight and ink, not size.
- **Khmer:** Kantumruy Pro, the same weights. Khmer runs about 15% taller: line height 1.7 for Khmer
  text, and labels never clipped to one line when they are Khmer.
- **Numbers:** Geist with tabular figures; the big counts (stock on hand, money) at 32-40 px, 600 weight.
- **Mono:** JetBrains Mono, only for lot numbers, barcodes and dates inside batch chips.
- **Body:** 16 px minimum on a phone (15 px on a laptop), line height 1.6, max 65 characters per line.
- **Banned:** Inter in the web app, serif in the app screens, all-caps sentences (short status tags only).

## 4. Component Stylings

- **Bottom tab bar (phone):** five slots: Home, Stock, **+**, Orders, More. 64 px tall plus the safe
  area. Icons 24 px with labels under them; the open tab is Ink, the rest Stone. The **+** is a 56 px
  Ink circle, lifted 12 px, white plus sign.
- **Side rail (laptop, 992 px and up):** the same five destinations in the same order, the + as a full
  "What happened?" button at the top. Nothing on a laptop that a phone can't reach.
- **What happened? sheet:** rises from the bottom (white, 20 px top corners, grab handle). Title, one
  line of help, a 2 x 2 grid of choice tiles (icon, bold label, grey hint), one full-width row under it,
  "Close" at the bottom. Closes on the scrim, on Close and on Escape.
- **Task row (Home):** 72 px, a 48 px tinted tile on the left (pastel of its status, with the product
  photo or an icon), a bold one-line title saying what to do ("Take 25 milk off the shelf"), one grey
  line saying why, a chevron. The whole row is one link.
- **Product row (Stock):** 56 px photo tile, name (bold), a status tag, one grey line; the count on the
  right, big, with its unit under it.
- **Status tag:** pill, 11 px, uppercase, 0.05em tracking, pastel background; always words.
- **Buttons:** 48 px tall on a phone (44 px minimum anywhere), 10 px radius. Primary: Ink fill, white
  text, one per screen. Secondary: white with a Hairline border. Press: scale(0.98). Labels are verbs,
  two or three words, never wrap.
- **Stepper (counting):** - and + as 56 px squares either side of a big number field; tap to change by
  one, type for a big change.
- **Cards:** only for the hero number block on Home (a "double bezel": a Paper shell, 6 px, holding a
  white core) and for grouped settings. Lists are plain rows on hairlines, not cards.
- **Inputs:** label above, 48 px tall, 10 px radius, Hairline border, Ink focus ring. Error text under the
  field, in Brick, saying what to do.
- **Empty states:** one icon tile, one line saying what is good about it ("Nothing to do. The shelves
  are in order."), and the next useful action.
- **Loading:** the tapped button says what it is doing ("Saving…"); lists show grey skeleton rows.

## 5. Layout Principles

- **Phone first.** One column, 16 px side margins, content max 640 px wide, centred on bigger screens.
- **Top bar:** the page title on the left (or a back arrow and title on inner pages), and on the right
  the language switch (ខ្មែរ / EN), search and help. Sticky, white, hairline under it.
- **Order of a screen:** the answer first (a number, a list), the actions under it, explanations last
  and folded away. Nothing that explains how LuyDek works sits above what the owner came for.
- **Laptop:** the rail on the left, the same single column at up to 960 px; long lists may show two
  columns of information per row, never two columns of rows.
- **Touch:** every target 44 px or more; nothing important within 12 px of the screen edge.

## 6. Motion & Interaction

- Spring-like easing `cubic-bezier(.32, .72, 0, 1)`, 280 ms for the sheet, 160 ms for presses.
- A finished task fades and slides out (opacity and transform only), and the count above it updates.
- Undo instead of "Are you sure?" wherever the action can be undone; a confirm only for what can't.
- `prefers-reduced-motion`: everything is instant.

## 7. Language

- **Khmer first.** A shop's default is set in `shop.web_lang`; each phone can switch with one tap and
  keeps its choice.
- Product names stay as the shop wrote them (Khmer and English names both shown in lists).
- Words of the shop, not of the software: "came in", "sold", "left", "past its date", "agent".
  Never "batch", "ledger", "SKU", "transaction" on a screen. A batch is "a delivery with its own date".
- Every title is a plain answer or a plain question ("What happened?", "Stock", "Order for Friday").
- No em-dashes; at most one "·" per line.

## 8. Anti-Patterns (Banned)

- Emojis on web screens (Telegram keeps them).
- Paragraphs above the content. Help text lives behind the "?" and in folded sections.
- More than one primary button on a screen; two buttons with the same intent.
- Pure black, purple, gradients, glass on scrolling content, neon glows.
- Cards inside cards; a card around every list.
- Colour as the only signal (every status has words).
- Text under 13 px; anything a thumb can't hit.
- Hidden essentials behind hover (there is no hover on a phone).
