---
name: doordash
description: Order DoorDash pickup or delivery for the owner from the assistant's own DoorDash account via Chrome. Trigger words - doordash, door dash, order food, pickup order, takeout, food pickup.
---

# DoorDash

DoorDash orders go through **the assistant's own account**. Chrome is signed into
it, so you never need the owner's login.

## Account (set up 2026-11-03)
- Login: **Continue with Google**, using the assistant's Gmail (`identity assistant.email`).
  There is no DoorDash password. If you're signed out, use that button, pick the
  assistant's account tile, then press Continue. Google's tiles ignore synthetic
  clicks, so `chrome screenshot` and `chrome click-at` the tile and the Continue
  button. That works where `click-by-name` and `iframe-click` don't.
- Profile name: the **owner's name**, so pickup orders are under the name they'll
  give at the counter.
- Phone: **the assistant's own number** (`identity assistant.phone`, the tethered
  iPhone line). NEVER use the owner's number for sign-ups or OTPs; that pings them
  for nothing. Codes texted to that number land in chat.db:
  `START=$(date +%s)` → trigger the code → `~/.claude/skills/sms-assistant/scripts/sms-otp --since $START --match DoorDash`
- Payment: the Privacy.com card is saved. To add a card again:
  /consumer/payment/ → Credit/Debit Card. The fields are VGS cross-origin iframes,
  so fill them with `axctl type "Google Chrome" --title "Card Number Input" | "Card expiration date" | "Card Security Code" | "Text input"(ZIP)`,
  reading values from Keychain (`privacy-card-*`, account `sven`). Never echo them.
- Address: owner's home (`identity owner.address`) is saved.

## Pickup order flow (verified end to end 2026-11-03, Honeybear Boba)
1. **Find the store.** In-app search only covers stores near the saved Pacifica
   address, so SF shops don't show up. Web-search `"<store name>" site:doordash.com`
   to get the `/store/<id>` URL. Check that the page title or address matches; a
   similarly named store is a common trap.
2. Open `https://www.doordash.com/store/<id>/?pickup=true`. The Delivery/Pickup
   `role=radio` pair should show `Pickup` with `aria-checked="true"`. The hours are in
   the page text, e.g. "11:30 am - 6:25 pm", so check the shop is still open.
3. **Menu items.** `click-by-name` hits the wrong node. Click the item's container
   div instead: find the `div` whose text starts with the item name and is
   < 250 chars. Menu sections load lazily as you scroll.
4. **Options modal** (`[role=dialog]`): each group ("Size", "Milk Option",
   "Ice Level", "Sweetness", "Toppings") is a heading. Walk up from the
   heading to the section that holds its inputs, click the `label` whose first
   line equals the option, then check `input:checked` → nearest text. The
   "Add to cart - $X" button is in the dialog.
5. **Cart.** `/consumer/checkout/` with no cart id is a 404. Open the cart drawer
   (the button whose text matches "items in cart"), then click **Continue**. Use
   `click-at` from a screenshot; the button isn't found by name. That loads
   `/consumer/checkout/?...order_cart_id=...`.
6. **Checkout page.** Read the "Pickup at <address>", the items with their
   options, and the Total. Then click the button "Place Pickup Order $X".
7. **Confirmation.** ALWAYS attach a `chrome screenshot` of the order page to the confirmation text (owner asked for this 2026-11-03), and check it before sending. The page goes to `/orders/<uuid>/`, and its text has
   "Pickup at <time>" plus the totals. Text the owner the drink/items, ready time,
   address, total, and "skip the line, show the order" (DoorDash's pickup
   instructions).

## Rules
- **One message to the owner.** Do every step that might need them (sign-in,
  OTP, captcha) before you text, and bundle the questions. On 2026-11-02/03 the
  owner was rightly annoyed by six pings for one Amazon order.
- Confirm items and the real checkout total before placing the order. Don't
  quote menu prices alone; take the total from the checkout page.
- **Dietary / allergy constraints are hard requirements.** Check the owner's
  preference facts (`~/dispatch/scripts/fact list --contact <owner> --active`)
  and apply everything the request states. Save any new constraint with
  `fact save --type preference`. Only order an item you can confirm meets the
  constraint from the menu text, the options modal, or the store's own allergen
  info. **A non-dairy milk swap does NOT make the drink dairy-free:** the base
  may still contain dairy (Thai/milk tea mixes, lattes, creamers, powders,
  foam/cheese tops). Prefer drinks that comply on their own (fruit, jasmine or
  green tea with no milk). If nothing confirmable fits, send ONE question with
  1-2 compliant options instead of guessing. Check by web/menu research only,
  NEVER by phone (see phone-calls). The confirmation text says which
  constraints you checked and how. (2026-11-03: "nondairy boba" → Thai tea +
  oat, base never verified.)
- If they named the food but not the place, pick the closest well-rated store
  that has it, and say which one in the confirmation.
