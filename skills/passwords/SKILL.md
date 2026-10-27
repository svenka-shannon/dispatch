---
name: passwords
description: Store and retrieve passwords/credentials using macOS Keychain. Trigger words - password, keychain, credentials, secrets, login.
---

# Passwords Skill

All my credentials are stored in macOS Keychain using the `security` CLI. This is the standard secure storage on macOS.

## Stored Credentials

### Account Credentials
| Service | Account | Purpose |
|---------|---------|---------|
| `google-account` | (stored in keychain) | My Google/Gmail account |
| `apple-id-password` | sven | My iCloud/Apple ID |

### API Keys & Secrets
| Service | Purpose |
|---------|---------|
| `google-api-key` | Google Cloud API key |
| `google-oauth-client-id` | OAuth client ID for gogcli |
| `google-oauth-client-secret` | OAuth client secret for gogcli |
| `modal-token-id` | Modal.com API token ID |
| `modal-token-secret` | Modal.com API token secret |
| `fal-api-key` | fal.ai API key (image/3D/video generation) |

### Payment Card (Privacy.com)
| Service | Purpose |
|---------|---------|
| `privacy-card-number` | Virtual card number |
| `privacy-card-exp` | Expiration date |
| `privacy-card-cvv` | CVV code |

### Site accounts
Shopping/booking accounts the assistant uses. Look up phone/address with `identity` instead of copying numbers here.

| Site | Login | Keychain service | Phone on account | Card | Notes |
|------|-------|------------------|------------------|------|-------|
| OpenTable | assistant email (`identity assistant.email`), sign-in by **emailed 6-digit code**, no password | none | owner's (`identity owner.phone`) | saved on account | Profile name is the owner (`identity owner.name`, Eric Maynard). Details in the opentable-watch skill. |
| Walmart | assistant email | `walmart` (account = assistant email) | owner's (`identity owner.phone`) | saved on account | Preferred address = owner home (`identity owner.address`). |
| Amazon | Eric's own account | `amazon-eric` | (Eric's) | (Eric's) | The saved password was **rejected on 2026-10-04**. Ask the owner for the current one before relying on it, and update with `-U`. |

## Create a site account
Use this when a site needs an account to book/buy something.

1. **Generate the password straight into Keychain.** Never echo it, print it, or paste it into chat or logs:
   ```bash
   security add-generic-password -a "$(identity assistant.email)" -s <site> \
     -w "$(openssl rand -base64 24 | tr -d '/+=' | cut -c1-24)" -U
   ```
   Service name = the site in lowercase (e.g. `walmart`). When the signup form needs it, read it at the moment of typing (`security find-generic-password -s <site> -w` into `cliclick t:"..."` or the form fill), never into a variable you print. If the site has password rules (symbols etc.), append a fixed suffix like `!9a` inside the `$(...)`.
2. **Sign up with the assistant's email** (`identity assistant.email`). Email OTPs and verification links arrive in that Gmail. Read them with the `email` skill, or in Chrome's Gmail tab.
3. **Phone number:** skip it if it's optional. If the site requires one, use the owner's (`identity owner.phone`), but **text the owner a heads-up first** ("signing up for <site> with your number, you may get a verification text"), since the code goes to their phone.
4. **Decline every upsell:** memberships and free trials (Walmart+, Prime, etc.), store credit cards, marketing/SMS opt-ins, "save 10% today" offers. Untick pre-ticked marketing boxes.
5. **Record it:** add a row to the "Site accounts" table above (login method, Keychain service, which phone, whether a card is saved, preferred address). No secrets in the table.

## CLI Usage

### Retrieve a password
```bash
security find-generic-password -s "service-name" -w
```

Examples:
```bash
# Get Google password
security find-generic-password -s "google-account" -w

# Get Apple ID password
security find-generic-password -s "apple-id-password" -w

# Get card number
security find-generic-password -s "privacy-card-number" -w
```

### Store a new password
```bash
security add-generic-password -a "account" -s "service-name" -w "password" -U
```
The `-U` flag updates if it already exists.

### List all my services
```bash
security dump-keychain | grep 'svce' | grep -oP '(?<="svce"<blob>=").*(?=")'
```

### Delete a password
```bash
security delete-generic-password -s "service-name"
```

## Security Notes

- **NEVER put actual passwords in code or skill files** - always retrieve from keychain at runtime
- **NEVER log or echo passwords** - only use them in secure contexts
- Keychain is encrypted and requires system login to access
