---
name: grubhub
description: Order food delivery from Grubhub via Chrome automation. Trigger words - grubhub, food delivery, order food, delivery.
---

# Grubhub Food Ordering

Order food delivery from Grubhub using Chrome automation.

## Prerequisites

- Chrome control skill (`/chrome-control`)
- Valid delivery address
- Account sign-up / login codes: use the assistant's own phone and `sms-otp`, never ask the owner to relay a code (see sms-assistant, "Accounts, sign-ups & verification codes")

## Workflow

### 1. Open Restaurant Page

```bash
chrome open "https://www.grubhub.com/restaurant/RESTAURANT-NAME-CITY/RESTAURANT-ID"
```

Or search for a restaurant:
```bash
chrome open "https://www.grubhub.com/search?orderMethod=delivery&locationMode=DELIVERY&facetSet=uma498&pageSize=20&hideHat498=true&queryText=RESTAURANT_NAME"
```

### 2. Set Delivery Address

The address modal appears when you click "Enter an address" or when the page loads without a saved address.

**CRITICAL: Google Places Autocomplete**

Grubhub uses Google Places autocomplete. You MUST:
1. Type the address into the input field
2. Wait for autocomplete dropdown to appear
3. Use ArrowDown key to highlight first suggestion
4. Press Enter to select it

```javascript
// Type address
const input = document.querySelector('input[placeholder*="Street address"]');
input.focus();
input.value = '123 Main St, Boston, MA';
input.dispatchEvent(new Event('input', {bubbles:true}));
```

Then use Chrome key commands:
```bash
chrome key TAB_ID ArrowDown   # Highlight first autocomplete suggestion
chrome key TAB_ID Enter       # Select it
```

After selecting, click the "Update" button to confirm.

### 3. Find Menu Items

Search for items:
```javascript
const input = document.querySelector('input[placeholder*="Search"]');
input.value = 'pepperoni';
input.dispatchEvent(new Event('input', {bubbles:true}));
```

Or scroll through categories on the left sidebar.

### 4. Add Items to Cart

Click the + button next to an item to add it. For items with customization options (size, toppings), a modal will appear.

### 5. Checkout

Navigate to checkout and fill in:
- Contact info (name, phone)
- Payment method
- Tip amount

**ALWAYS screenshot checkout before entering payment info for user confirmation.**

## Tips

- Delivery availability depends on the address - set address FIRST
- Some restaurants are pickup-only via their own site but available for delivery via Grubhub
- Prices on Grubhub may differ from restaurant's direct ordering
- Check "Pricing & fees info" for delivery fees and service charges

## Common Issues

### "This store doesn't deliver to your address"
- Update the delivery address using the modal
- Make sure to SELECT from the Google autocomplete dropdown, not just type

### Slow page loads
- Grubhub can be slow; add `sleep 3-4` after navigation
- Use timeouts when waiting for elements

## Example: Order Pizza

```bash
# 1. Open restaurant
chrome open "https://www.grubhub.com/restaurant/ziggys-583-washington-st-brighton/2893749"

# 2. Set address (see workflow above)

# 3. Search for item
# Use JS to type in search box

# 4. Click on item to add
# Use JS to find and click the + button or item card

# 5. Customize if needed (size, toppings)

# 6. Go to checkout
# Screenshot and confirm with user before payment
```
