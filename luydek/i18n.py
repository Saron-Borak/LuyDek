"""Owner-facing strings, Khmer first.

The Khmer here was written by a non-native author and should be proofread by a
native speaker before a pilot. Keep the owner's own phrases: "money sleeping on
the shelf" (លុយដេកលើធ្នើ), "the customer went across the street".
"""

from __future__ import annotations

STRINGS: dict[str, dict[str, str]] = {
    # --- onboarding ---
    "welcome": {
        "km": "សួស្តី {name}! 👋\nខ្ញុំនឹងផ្ញើរូបទំនិញដែលខ្ញុំមិនប្រាកដ ពេលហាងស្ងាត់ (៣ដងក្នុងមួយថ្ងៃ)។\nគ្រាន់តែចុចប៊ូតុង — មិនបាច់វាយអក្សរទេ។",
        "en": "Hello {name}! 👋\nAt quiet times (3 times a day) I'll send the few items I'm not sure about.\nJust tap a button — no typing.",
    },
    "need_pin": {
        "km": "សូមផ្ញើ /start រួចលេខកូដហាង។ ឧទាហរណ៍៖ /start 1234",
        "en": "Send /start followed by the shop PIN, e.g. /start 1234",
    },
    "not_registered": {
        "km": "អ្នកមិនទាន់ចុះឈ្មោះទេ។ សូមផ្ញើ /start និងលេខកូដហាង។",
        "en": "You're not registered yet. Send /start with the shop PIN.",
    },
    "lang_set": {"km": "✅ ភាសាខ្មែរ", "en": "✅ English"},
    "menu_invoice": {"km": "🧾 វិក្កយបត្រ", "en": "🧾 Invoice"},
    "menu_adjust": {"km": "➖ កែស្តុក", "en": "➖ Adjust stock"},
    "menu_orders": {"km": "📦 បញ្ជីបញ្ជាទិញ", "en": "📦 Order lists"},
    "menu_week": {"km": "📊 សប្តាហ៍នេះ", "en": "📊 This week"},
    "menu_today": {"km": "📈 លក់ថ្ងៃនេះ", "en": "📈 Sold today"},
    "menu_stock": {"km": "📋 ស្តុកនៅសល់", "en": "📋 Stock left"},

    # --- digest ---
    "digest_header": {
        "km": "🧾 ការលក់ {window}៖ {txns} ដង\n✅ {auto} ស្គាល់ដោយស្វ័យប្រវត្តិ\n⚠️ {pending} ត្រូវពិនិត្យបន្តិច",
        "en": "🧾 Sales {window}: {txns} transactions\n✅ {auto} recognized automatically\n⚠️ {pending} need a quick check",
    },
    "digest_nothing": {
        "km": "✅ គ្មានអ្វីត្រូវពិនិត្យទេ។ ការលក់ {txns} ដង ស្គាល់ទាំងអស់។",
        "en": "✅ Nothing to check. All {txns} sales were recognized.",
    },
    "digest_waiting": {
        "km": "⏳ នៅសល់ {n} មុខពីមុន មិនទាន់ពិនិត្យ។",
        "en": "⏳ {n} earlier items are still waiting.",
    },
    "show_again": {"km": "🔁 បង្ហាញម្តងទៀត", "en": "🔁 Show them again"},
    "ask_medium": {"km": "#{n} · {time}\nតើនេះជា {name} មែនទេ? ×{count}", "en": "#{n} · {time}\nIs this {name}? ×{count}"},
    "ask_low": {"km": "#{n} · {time}\nតើនេះជាអ្វី? (ប្រហែលជា {name}) ×{count}", "en": "#{n} · {time}\nWhat is this? (maybe {name}) ×{count}"},
    "ask_tray": {"km": "#{n} · {time}\n💊 ថ្នាំលក់រាយ៖ {name} ចំនួន {count} គ្រាប់ មែនទេ?",
                 "en": "#{n} · {time}\n💊 Sold loose: {count} × {name}?"},
    "ask_tray_low": {"km": "#{n} · {time}\n💊 ថ្នាំលក់រាយ {count} គ្រាប់ — ថ្នាំអ្វី? (ប្រហែលជា {name})",
                     "en": "#{n} · {time}\n💊 Loose tablets ×{count} — which medicine? (maybe {name})"},
    "yes": {"km": "✅ ត្រូវហើយ", "en": "✅ Yes"},
    "other": {"km": "🔍 ផ្សេងទៀត", "en": "🔍 Other"},
    "not_item": {"km": "✖ មិនមែនទំនិញ", "en": "✖ Not an item"},
    "new_product": {"km": "🆕 ទំនិញថ្មី", "en": "🆕 New product"},
    "app_open": {"km": "✅ ពិនិត្យ {n} មុខ", "en": "✅ Check {n} items"},
    "app_open_none": {"km": "📋 បើក LuyDek", "en": "📋 Open LuyDek"},
    "app_intro": {
        "km": "អូសទៅស្ដាំ បើកាមេរ៉ាត្រូវ អូសទៅឆ្វេង ដើម្បីកែ។",
        "en": "Swipe right when the camera is right, left to correct it.",
    },
    "add_named": {"km": "➕ បន្ថែម {name}", "en": "➕ Add {name}"},
    "looks_new": {
        "km": "\n🆕 នៅលើកញ្ចប់សរសេរថា «{name}» — មិនទាន់មានក្នុងបញ្ជីទំនិញរបស់អ្នកទេ",
        "en": "\n🆕 The package reads “{name}” — not in your products yet",
    },
    "ask_name": {
        "km": "#{n} · តើវាជាទំនិញអ្វី? សូមឆ្លើយតបសារនេះដោយសរសេរឈ្មោះ ម៉ាក និងទំហំ (ឧ. Oishi តែបៃតង 380ml)។",
        "en": "#{n} · What is it? Reply to this message with its name, brand and size (e.g. Oishi green tea 380ml).",
    },
    "added_product": {
        "km": "🆕 បានបន្ថែម {name}។ កាមេរ៉ានឹងស្គាល់វានៅពេលក្រោយ។ ដាក់អ្នកផ្គត់ផ្គង់ និងតម្លៃនៅពេលណាក៏បាន។",
        "en": "🆕 Added {name}. The camera will know it next time. Add its supplier and price whenever you like.",
    },
    "added_more": {
        "km": "មានការលក់ {n} ទៀតដែលមើលទៅដូចវា — នឹងសួរក្នុងការត្រួតពិនិត្យបន្ទាប់។",
        "en": "{n} more sale(s) look like it — they'll be asked about in the next check.",
    },
    "confirm_all": {"km": "✅ យល់ព្រមទាំងអស់តាមការទាយ", "en": "✅ Confirm all as guessed"},
    "confirmed": {"km": "✅ បានបញ្ជាក់៖ {name} ×{count}", "en": "✅ Confirmed: {name} ×{count}"},
    "corrected": {"km": "✏️ បានកែ៖ {name} ×{count}", "en": "✏️ Corrected: {name} ×{count}"},
    "removed": {"km": "✖ មិនរាប់ជាការលក់ទេ", "en": "✖ Not counted as a sale"},
    "unknown": {"km": "❓ ទំនិញមិនមានក្នុងបញ្ជី — នឹងបន្ថែមពេលក្រោយ", "en": "❓ Not in the catalog — flagged to add later"},
    "best_guess": {"km": "🌙 យកតាមការទាយ៖ {name} ×{count}", "en": "🌙 Accepted as best guess: {name} ×{count}"},
    "already": {"km": "បានពិនិត្យរួចហើយ", "en": "Already checked"},
    "confirmed_all": {"km": "✅ បានបញ្ជាក់ {n} មុខ", "en": "✅ Confirmed {n} items"},
    "which_one": {"km": "#{n} ជាលេខមួយណា?", "en": "Which one is #{n}?"},
    "more": {"km": "▶ ច្រើនទៀត", "en": "▶ More"},
    "not_in_list": {"km": "❓ មិនមានក្នុងបញ្ជី", "en": "❓ Not in the list"},
    "window_morning": {"km": "ពេលព្រឹក", "en": "this morning"},
    "window_midday": {"km": "ពេលថ្ងៃ", "en": "at midday"},
    "window_evening": {"km": "ពេលល្ងាច", "en": "this evening"},
    "window_now": {"km": "ថ្មីៗ", "en": "since the last check"},

    # --- adjustments ---
    "adjust_why": {"km": "ហេតុអ្វីបានជាស្តុកប្តូរ?", "en": "Why did stock change?"},
    "adjust_which": {"km": "មុខទំនិញណា?", "en": "Which product?"},
    "adjust_howmany": {"km": "{name} — ចំនួនប៉ុន្មាន?", "en": "{name} — how many?"},
    "adjust_done": {"km": "✅ បានកត់ត្រា៖ {reason} · {name} · {qty}", "en": "✅ Recorded: {reason} · {name} · {qty}"},
    "reason_credit_sale": {"km": "💳 លក់ជំពាក់", "en": "💳 Credit sale"},
    "reason_phone_order": {"km": "📱 កុម្ម៉ង់តាមទូរស័ព្ទ", "en": "📱 Phone/Telegram order"},
    "reason_family_use": {"km": "🏠 គ្រួសារប្រើ", "en": "🏠 Family use"},
    "reason_damaged": {"km": "💥 ខូច", "en": "💥 Damaged"},
    "reason_expired": {"km": "⌛ ផុតកំណត់", "en": "⌛ Expired"},
    "reason_put_back": {"km": "↩️ ភ្ញៀវដាក់វិញ", "en": "↩️ Put back"},
    "prev": {"km": "◀", "en": "◀"},
    "next": {"km": "▶", "en": "▶"},
    "cancel": {"km": "✖ បោះបង់", "en": "✖ Cancel"},
    "cancelled": {"km": "បានបោះបង់", "en": "Cancelled"},

    # --- invoices ---
    "invoice_help": {
        "km": "🧾 ថតរូបវិក្កយបត្ររបស់ភ្នាក់ងារ ហើយផ្ញើមកទីនេះ។ ខ្ញុំនឹងអាន ហើយបញ្ចូលស្តុកឲ្យ។",
        "en": "🧾 Take a photo of the agent's invoice and send it here. I'll read it and add the stock.",
    },
    "invoice_reading": {"km": "📖 កំពុងអានវិក្កយបត្រ...", "en": "📖 Reading the invoice…"},
    "invoice_queued": {
        "km": "📶 អ៊ីនធឺណិតមិនដំណើរការ។ ខ្ញុំនឹងអានម្តងទៀតពេលវាមកវិញ។",
        "en": "📶 No internet right now. I'll read it again when the connection is back.",
    },
    "invoice_failed": {
        "km": "😕 ខ្ញុំអានវិក្កយបត្រនេះមិនបានទេ។ សូមបញ្ចូលនៅលើផ្ទាំងគ្រប់គ្រង (Invoices)។",
        "en": "😕 I couldn't read this invoice. Please enter it on the dashboard (Invoices).",
    },
    "invoice_no_key": {
        "km": "ការអានវិក្កយបត្រស្វ័យប្រវត្តិមិនទាន់បើក។ សូមបញ្ចូលនៅលើផ្ទាំងគ្រប់គ្រង (Invoices)។",
        "en": "Automatic invoice reading isn't set up. Please enter it on the dashboard (Invoices).",
    },
    "invoice_title": {"km": "🧾 វិក្កយបត្រពី {supplier}", "en": "🧾 Invoice from {supplier}"},
    "invoice_unknown_supplier": {"km": "ភ្នាក់ងារមួយណា?", "en": "Which supplier is this from?"},
    "invoice_line": {"km": "{text} → {name}៖ {qty} {unit} = +{units}", "en": "{text} → {name}: {qty} {unit} = +{units}"},
    "invoice_line_free": {"km": "🎁 {text} → {name}៖ ថែម +{units}", "en": "🎁 {text} → {name}: free +{units}"},
    "invoice_line_unmatched": {"km": "❓ {text} — មុខទំនិញណា?", "en": "❓ {text} — which product?"},
    "invoice_line_skipped": {"km": "⏭ {text} (រំលង)", "en": "⏭ {text} (skipped)"},
    "invoice_expiry": {"km": " · ផុត {date}", "en": " · exp {date}"},
    "invoice_confirm": {"km": "✅ ត្រឹមត្រូវ បញ្ចូលស្តុក", "en": "✅ Correct, add to stock"},
    "invoice_reject": {"km": "✖ មិនត្រឹមត្រូវ", "en": "✖ Wrong, discard"},
    "invoice_bonus": {"km": "🎁 មានថែមក្រៅវិក្កយបត្រ", "en": "🎁 Free bonus not on invoice"},
    "invoice_skip_line": {"km": "⏭ រំលង", "en": "⏭ Skip"},
    "invoice_added": {"km": "✅ បានបញ្ចូលស្តុក +{units} ({lines} មុខ)", "en": "✅ Added +{units} units to stock ({lines} items)"},
    "invoice_rejected": {"km": "✖ បានបោះបង់វិក្កយបត្រនេះ", "en": "✖ Invoice discarded"},
    "invoice_needs_lines": {"km": "សូមជ្រើសមុខទំនិញដែលនៅសល់សិន", "en": "Pick the remaining products first"},
    "bonus_which": {"km": "🎁 ថែមមុខអ្វី?", "en": "🎁 Which product came free?"},
    "bonus_done": {"km": "🎁 បានបញ្ចូលថែម៖ {name} +{qty}", "en": "🎁 Free stock added: {name} +{qty}"},
    "unit_case": {"km": "កេស", "en": "case"},
    "unit_pack": {"km": "កញ្ចប់", "en": "pack"},
    "unit_piece": {"km": "ដុំ", "en": "pc"},

    # --- order lists ---
    "orders_title": {"km": "{emoji} {supplier} (មក{day})", "en": "{emoji} {supplier} ({day})"},
    "orders_simulated": {
        "km": "⚠️ ប្រវត្តិលក់ខ្លះជាទិន្នន័យសាកល្បង (simulated)",
        "en": "⚠️ Includes SIMULATED sales history",
    },
    "orders_observing": {
        "km": "👀 សប្តាហ៍ដំបូង៖ ខ្ញុំកំពុងរៀនពីការលក់ មិនទាន់ណែនាំទេ។",
        "en": "👀 First weeks: I'm still learning your sales, no recommendations yet.",
    },
    "orders_head_order": {"km": "✅ ទិញ៖", "en": "ORDER:"},
    "orders_head_market": {"km": "🛒 ទិញរាយនៅផ្សារ៖", "en": "MARKET RUN (buy loose):"},
    "orders_head_check": {"km": "❔ ពិនិត្យ៖", "en": "CHECK:"},
    "orders_head_skip": {"km": "⏸ កុំទិញ៖", "en": "SKIP:"},
    "orders_head_sleeping": {"km": "💤 លុយដេកលើធ្នើ៖", "en": "💤 SLEEPING MONEY:"},
    "orders_case": {"km": "{n} កេស", "en": "{n} case"},
    "orders_cases": {"km": "{n} កេស", "en": "{n} cases"},
    "orders_boxes": {"km": "{n} ប្រអប់", "en": "{n} {label}"},
    "orders_units": {"km": "{n} ដុំ", "en": "{n} units"},
    "orders_total": {"km": "សរុប ≈ ${value}", "en": "Total ≈ ${value}"},
    "orders_min": {"km": " (តិចជាងការបញ្ជាទិញអប្បបរមា ${min})", "en": " (below the ${min} minimum order)"},
    "orders_sleeping_total": {"km": "💤 លុយដេកសរុប៖ ${value}", "en": "💤 Money sleeping on the shelf: ${value}"},
    "orders_none": {"km": "គ្មានអ្វីត្រូវទិញទេ", "en": "Nothing to order"},
    "r_order": {
        "km": "លក់បាន {last_week} សប្តាហ៍មុន, នៅសល់ {stock}, អ្នកលក់មកម្តងទៀតក្នុង {cycle} ថ្ងៃ",
        "en": "sold {last_week} last week, {stock} left, next visit in {cycle} days",
    },
    "r_no_data_critical": {
        "km": "អស់ស្តុកពេញ ៤ សប្តាហ៍ — ទំនិញសំខាន់ ទិញ ១ កេសសាកល្បង",
        "en": "out of stock all 4 weeks — critical item, order 1 case to test demand",
    },
    "r_no_data": {"km": "អស់ស្តុកពេញ ៤ សប្តាហ៍ — គ្មានទិន្នន័យលក់", "en": "out of stock all 4 weeks — no sales data"},
    "r_observing": {"km": "កំពុងរៀន", "en": "still observing"},
    "r_count_first": {
        "km": "ទំនិញថ្មី — សូមរាប់ស្តុកលើធ្នើម្តងសិន",
        "en": "new product — count the shelf once first",
    },
    "r_market": {
        "km": "ត្រូវការ {units} តែមួយកេសលក់មិនទាន់មុនផុតកំណត់",
        "en": "need {units}, but a full case won't sell before it expires",
    },
    "r_enough": {"km": "នៅសល់ {stock}, លក់បាន {last_week} សប្តាហ៍មុន", "en": "{stock} left, sold {last_week} last week"},
    "r_overstock": {"km": "នៅសល់ {stock} គ្រប់ {cover} ថ្ងៃ", "en": "{stock} left covers {cover} days"},
    "r_no_sales": {"km": "នៅសល់ {stock}, គ្មានលក់ {since} ថ្ងៃ", "en": "{stock} left, no sale in {since} days"},
    "r_sleeping": {
        "km": "នៅសល់ {stock}, លក់បាន {sold_4w} ក្នុង ៤ សប្តាហ៍, ${value} ដេកលើធ្នើ",
        "en": "{stock} left, {sold_4w} sold in 4 weeks, ${value} on the shelf",
    },
    "r_stockout": {
        "km": " ⚠️ អស់ស្តុក {days} ថ្ងៃ — ភ្ញៀវទៅហាងទល់មុខ",
        "en": " ⚠️ was out {days} days — customers went across the street",
    },
    "r_capped": {"km": " (កំណត់ដោយថ្ងៃផុតកំណត់)", "en": " (capped by expiry)"},
    "r_expiring": {"km": " ⌛ ផុតកំណត់ {date}", "en": " ⌛ expires {date}"},
    "r_negative": {"km": " ❗ ស្តុកអវិជ្ជមាន — ពិនិត្យ", "en": " ❗ negative stock — investigate"},

    # --- the day's summary and stock left ---
    "sum_title": {"km": "📊 ថ្ងៃនេះ · {day}", "en": "📊 Today · {day}"},
    "sum_sales": {"km": "🧾 លក់ {txns} ដង · ទំនិញ {units}", "en": "🧾 {txns} sales · {units} items sold"},
    "sum_none": {
        "km": "ថ្ងៃនេះមិនទាន់មានការលក់ទេ។ តើកាមេរ៉ានៅកន្លែងលក់បានបើកទេ?",
        "en": "No sales recorded today. Was the counter camera on?",
    },
    "sum_pending": {
        "km": "⏳ នៅសល់ {n} មុខត្រូវពិនិត្យ — នឹងរាប់ពេលពិនិត្យរួច",
        "en": "⏳ {n} still to check — counted once checked",
    },
    "sum_received": {"km": "📥 ទទួលបាន +{units} ({products} មុខ)", "en": "📥 Received +{units} ({products} products)"},
    "sum_head_sold": {"km": "លក់ថ្ងៃនេះ៖", "en": "SOLD TODAY:"},
    "sum_sold_line": {"km": "{name} — លក់ {sold} · {left}", "en": "{name} — {sold} sold · {left}"},
    "sum_more": {"km": "… និង {n} មុខទៀត", "en": "… and {n} more"},
    "sum_head_watch": {"km": "⚠️ ជិតអស់ ឬអស់ហើយ៖", "en": "⚠️ RUNNING LOW OR OUT:"},
    "sum_value": {"km": "💰 ទំនិញលើធ្នើ ≈ ${value} (តម្លៃទិញ)", "en": "💰 Stock on the shelf ≈ ${value} at cost"},
    "sum_open_app": {"km": "📋 មើលស្តុកក្នុង LuyDek", "en": "📋 See stock in LuyDek"},
    "left": {"km": "នៅសល់ {n}", "en": "{n} left"},
    "left_out": {"km": "អស់ហើយ", "en": "none left"},
    "left_negative": {"km": "❗ ស្តុកក្រោមសូន្យ — សូមរាប់ធ្នើ", "en": "❗ below zero — count the shelf"},
    "left_new": {"km": "មិនទាន់រាប់ស្តុក", "en": "not counted yet"},
    "left_days": {"km": " (គ្រប់ប្រហែល {days} ថ្ងៃ)", "en": " (about {days} days)"},
    "must_have": {"km": " ‼️ ត្រូវតែមាន", "en": " ‼️ must-have"},
    "stock_title": {"km": "📋 ស្តុកនៅសល់ · {day}", "en": "📋 Stock left · {day}"},
    "stock_head_out": {"km": "❌ អស់ស្តុក៖", "en": "❌ OUT:"},
    "stock_head_low": {"km": "🟠 ជិតអស់៖", "en": "🟠 RUNNING LOW:"},
    "stock_head_expiring": {"km": "⌛ ជិតផុតកំណត់៖", "en": "⌛ EXPIRING SOON:"},
    "stock_head_new": {"km": "🆕 មិនទាន់រាប់៖", "en": "🆕 NOT COUNTED YET:"},
    "stock_head_ok": {"km": "✅ មានស្តុក៖", "en": "✅ IN STOCK:"},
    "stock_sold_today": {"km": " · លក់ {n} ថ្ងៃនេះ", "en": " · {n} sold today"},
    "stock_pick": {
        "km": "📋 ស្តុកនៅសល់ — ជ្រើសទំនិញមួយ ឬមើលទំនិញទាំងអស់។ អាចវាយឈ្មោះទំនិញមកក៏បាន។",
        "en": "📋 Stock left — pick a product, or see every item. You can also just type a product's name.",
    },
    "stock_every": {"km": "📋 ទំនិញទាំងអស់", "en": "📋 Every item"},
    "stock_back": {"km": "◀ ទំនិញ", "en": "◀ Products"},
    "stock_nomatch": {
        "km": "រកមិនឃើញទំនិញ «{q}» ទេ។ ជ្រើសពីបញ្ជីខាងក្រោម៖",
        "en": "No product matches “{q}”. Pick one below:",
    },
    "stock_matches": {"km": "ទំនិញដែលត្រូវនឹង «{q}»៖", "en": "Products matching “{q}”:"},
    "p_out": {"km": "❌ អស់ស្តុក", "en": "❌ out of stock"},
    "p_low": {"km": "🟠 ជិតអស់", "en": "🟠 running low"},
    "p_sold": {
        "km": "🧾 លក់ {today} ថ្ងៃនេះ · {week} ក្នុង ៧ ថ្ងៃ · {month} ក្នុង ២៨ ថ្ងៃ",
        "en": "🧾 Sold {today} today · {week} in 7 days · {month} in 28 days",
    },
    "p_cover": {"km": "⏱ គ្រប់លក់ប្រហែល {days} ថ្ងៃទៀត តាមល្បឿនលក់សប្តាហ៍នេះ", "en": "⏱ Lasts about {days} more days at this week's pace"},
    "p_pending": {
        "km": "⏳ {n} ទៀតកំពុងរង់ចាំពិនិត្យ (មិនទាន់ដកពីស្តុក)",
        "en": "⏳ {n} more waiting to be checked (not taken off yet)",
    },
    "p_uncounted": {
        "km": "🆕 មិនទាន់រាប់ស្តុក៖ លក់បាន {n} តាំងពីបន្ថែម។ រាប់ធ្នើម្តង ដើម្បីតាមដានស្តុក។",
        "en": "🆕 Never counted: {n} sold since it was added. Count the shelf once to track it.",
    },
    "p_expiry": {"km": "⌛ ផុតកំណត់មុនគេ {date}", "en": "⌛ Earliest expiry {date}"},
    "p_agent": {"km": "🚚 {supplier} មក{day}", "en": "🚚 {supplier} comes {day}"},
    "p_agent_today": {"km": "ថ្ងៃនេះ", "en": "today"},
    "p_value": {"km": "💰 តម្លៃ ${value} (តម្លៃទិញ)", "en": "💰 Worth ${value} at cost"},
    "p_last": {"km": "លក់ចុងក្រោយ {when}", "en": "Last sold {when}"},

    # --- weekly ---
    "week_summary": {
        "km": "📊 សប្តាហ៍នេះ\n🎯 ខ្ញុំស្គាល់ត្រូវ {acc}%\n👆 ការចុចក្នុងមួយថ្ងៃ៖ {taps}\n🧾 ការលក់៖ {txns} ដង\n💤 លុយដេកលើធ្នើ៖ ${sleeping}",
        "en": "📊 This week\n🎯 I was {acc}% right\n👆 Taps per day: {taps}\n🧾 Sales recorded: {txns}\n💤 Money sleeping on the shelf: ${sleeping}",
    },
    "weekday_0": {"km": "ថ្ងៃច័ន្ទ", "en": "Monday"},
    "weekday_1": {"km": "ថ្ងៃអង្គារ", "en": "Tuesday"},
    "weekday_2": {"km": "ថ្ងៃពុធ", "en": "Wednesday"},
    "weekday_3": {"km": "ថ្ងៃព្រហស្បតិ៍", "en": "Thursday"},
    "weekday_4": {"km": "ថ្ងៃសុក្រ", "en": "Friday"},
    "weekday_5": {"km": "ថ្ងៃសៅរ៍", "en": "Saturday"},
    "weekday_6": {"km": "ថ្ងៃអាទិត្យ", "en": "Sunday"},
}


def t(lang: str, key: str, **kwargs) -> str:
    entry = STRINGS[key]
    text = entry.get(lang) or entry["en"]
    return text.format(**kwargs) if kwargs else text
