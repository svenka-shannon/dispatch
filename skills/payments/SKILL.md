---
name: payments
description: Make online purchases using stored Privacy.com virtual card. Use when buying things online, checking out, making payments, or needing credit card details. Trigger words - buy, purchase, checkout, pay, credit card, payment.
---

# Payments Skill

Make online purchases using Sven's Privacy.com virtual card stored in macOS Keychain.

## Sven's Credentials (macOS Keychain)

All of Sven's account credentials are stored in macOS Keychain:

```bash
# Google account
security find-generic-password -s "google-account" -w

# Apple ID / iCloud password
security find-generic-password -s "apple-id-password" -w
# (email is in ~/.claude/secrets.env as APPLE_ID_EMAIL)
```

## Card Details Location

Card details are stored securely in macOS Keychain:

```bash
# Get card number
security find-generic-password -a "sven" -s "privacy-card-number" -w

# Get expiration date
security find-generic-password -a "sven" -s "privacy-card-exp" -w

# Get CVV
security find-generic-password -a "sven" -s "privacy-card-cvv" -w
```

## Card Information

- **Type:** Privacy.com virtual card
- **Cardholder:** the admin, `identity owner.name`. RESOLVED 2026-09-01.
  This doc previously said "AMBIGUOUS — ask before using". That was stale: on
  2026-08-27 the admin stated the Privacy card is "a real card in my name", and
  Privacy.com issues virtual cards against the account holder's verified
  identity. On 2026-09-01 he pushed back — *"You don't know my name?"* — when
  asked again. **Do not re-ask.** Use `identity owner.name` on cardholder-name fields.
  (The name is not in Keychain, only number, exp and CVV.)
- **Limits:** Set by admin in Privacy.com dashboard

## Usage Guidelines

### Before Making a Purchase

1. **Always confirm with admin** before making any purchase
2. Check the card's spending limit in Privacy.com if needed
3. Verify the merchant is legitimate
4. **Approval only covers what was quoted:** if the final total is >15% over the approved figure, or any item was substituted/added/changed, stop before paying and get a fresh yes (see amazon skill Safety Rule 5)

### Making a Purchase

Never put card number, CVV or passwords into shell variables, argv (`cliclick t:"$(security ...)"`,
`chrome insert-text <tab> "$(security ...)"`), echo, or chat. Type them with tools that read
Keychain in-process:
- Card fields: `scripts/fill-card-iframe` (below).
- Any single secret field: `chrome os-type <tab> --at X,Y --clear --secret-service privacy-card-cvv --secret-account sven`
  (chrome-control skill, "Last-resort OS-level typing fallback"), or `type-secret` for normal inputs.

### Timed holds (OpenTable 5-min, ticket queues)

**Save the card on the site account BEFORE the hold starts.** On 2026-10-18 an OpenTable
hold expired while the card was being typed into split Stripe fields. Lost the reservation. Add the card
under the account's payment methods first. When the slot appears, booking is then one click.

### Billing Address

Use the admin's billing address: `identity owner.address` (name: `identity owner.name`).
ZIP only: `identity owner.address | grep -oE '[0-9]{5}$'`.

## Security Rules

1. **Never log or display full card details** in responses to non-admin users
2. **Never share card details** with third parties or other sessions
3. **Only use for purchases explicitly approved by admin**
4. **Report any suspicious charges** to admin immediately

## Privacy.com Features

This card has these protections:
- Spending limits (total, per-transaction, monthly)
- Can be paused/closed anytime
- Single-merchant locking available
- Real-time transaction notifications

## Updating Card Details

If the card is replaced or updated:

```bash
# Update card number
security add-generic-password -a "sven" -s "privacy-card-number" -U -w   # -w last with no value = prompts, keeps it out of argv (NEW_NUMBER)

# Update expiration
security add-generic-password -a "sven" -s "privacy-card-exp" -U -w   # -w last with no value = prompts, keeps it out of argv (MM/YY)

# Update CVV
security add-generic-password -a "sven" -s "privacy-card-cvv" -U -w   # -w last with no value = prompts, keeps it out of argv (XXX)
```

## Stripe / cross-origin card iframes — `fill-card-iframe` (updated 2026-10-19)
Reads number/exp/cvv from Keychain inside the process (never argv, never printed). It checks each
field by **length only**, never reading back the value.

```bash
F=~/.claude/skills/payments/scripts/fill-card-iframe
ZIP=$(identity owner.address | grep -oE '[0-9]{5}$')

# Split forms (separate number / MM-YY / CVC / ZIP boxes): RECOMMENDED. Take a screenshot,
# read each field's center (screenshot pixels), pass all of them:
$F <tab> --zip "$ZIP" --at-number X,Y --at-exp X,Y --at-cvc X,Y --at-zip X,Y

# Legacy (single combined Stripe card element): click the number field, then:
$F <tab> --zip "$ZIP"
```
- With coords, fields are filled one at a time: number → exp (MMYY) → cvc → zip. In `--mode auto` (default)
  each field gets a CDP click + debugger `iframe_type`, then a length check. If the field is empty or
  can't be checked, it is cleared and retyped with OS-level keystrokes (`chrome os-type` path: Chrome
  must be frontmost, else exit 3). `--mode os` goes straight to OS typing, which is what worked on OpenTable and
  Walmart 2026-10-18. `--mode debugger` uses CDP only. `--coords css` if your coords are viewport px.
- Without coords it relies on Stripe auto-advance. On split forms that filled ONLY the card number
  (2026-10-18), so always pass coords on split forms.
- Output per field: `filled (N chars)` / `EMPTY` (exit 2) / `could not verify` (non-Stripe frame; check with a
  screenshot). `--verify-pattern <iframe-url-substring>` for other processors.
- Then delete any screenshot that showed the card: `chrome shot-purge --since 15m` (safe when nothing matches).

## Walmart account (created 2026-10-18)
- Login is svenka.shannon@gmail.com. The password is in Keychain: service `walmart`, account = the login (type it with `chrome os-type <tab> --at X,Y --secret-service walmart --secret-account <login>`; never `-w` into argv). The account phone is the admin's (`identity owner.phone`), so a new device or login may text him a code; give him a heads-up first.
- The Privacy card is saved as the default payment (last 4: see Keychain). The preferred address is the admin's (`identity owner.name`, `identity owner.address`).
- **Automation:** Walmart's identity/checkout pages ignore chrome-control `insert-text`. Use `chrome os-type <tab> --at X,Y --text ...`, which checks that Chrome is frontmost, clicks and types. Native `<select>` menus: `chrome os-type <tab> --at X,Y --select --text CA`. Type the option label as shown: state is the abbreviation ("CA", not "California", which picks "CO"), and card year is 2 digits.
- Decline the Walmart+ $1 trial, OnePay and donation upsells.
- First order: #200015679307212, Oreo 3" wafers (no creme), 1 lb, $21.49, arriving Oct 23-24.
- Walmart charges a $6.99 shipping fee on orders under $35 for items Walmart sells itself. Some marketplace sellers ship free. The 2026-10-19 order was CR2032 batteries for Eric's GR86 fob (fob HYQ14AHK takes CR2032), #200015540034495, $14.12.
- The saved address has delivery instructions set to "Apartment", which nobody chose. Edit it in Account > Addresses if it matters.

## After every purchase: add it to the delivery watcher
In the same turn as the purchase, add an `{"order", "label", "ordered", "contact"}` entry to `~/dispatch/state/package-watch.json`. Add `tracking` once it ships. See the packages skill, "Every purchase goes on the watcher". Skipping this step means the owner gets no shipping notifications at all.
