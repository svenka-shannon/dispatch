---
name: sven-pages
description: Publish HTML folders with per-folder ACLs. Default is PRIVATE (admin-only). Supports public access or email-based access control. Trigger words - deploy, pages, report, html hosting, publish.
---

# sven-pages

Publish HTML folders to Cloudflare with per-folder access control. **Default is PRIVATE (admin-only).**

## Privacy-First Policy

**Pages are PRIVATE by default.** Only use `--public` when the user explicitly asks to make something public.

- **In a chat context**: Always use `--acl` with the chat participants' emails
- **For personal/admin pages**: Use default (no flags) for admin-only access
- **Public pages**: Only use `--public` when explicitly told "make it public"

## Quick Start

```bash
# Publish a folder (private, admin-only — this is the default)
~/.claude/skills/sven-pages/scripts/publish ./my-report

# Publish shared with specific users (use this in chat contexts)
~/.claude/skills/sven-pages/scripts/publish ./my-report --acl "alice@gmail.com,bob@gmail.com"

# Publish publicly (ONLY when explicitly asked)
~/.claude/skills/sven-pages/scripts/publish ./my-report --public

# List all published pages
~/.claude/skills/sven-pages/scripts/publish --list

# Update ACL for existing folder
~/.claude/skills/sven-pages/scripts/publish --set-acl my-report --acl "new-user@gmail.com"

# Make existing folder public
~/.claude/skills/sven-pages/scripts/publish --set-acl my-report --public

# Delete a folder
~/.claude/skills/sven-pages/scripts/publish --delete my-report
```

## Access Control

Each folder can have its own ACL:

- **No ACL (default)**: Only admin can access
- **Email list (`--acl "a@x.com,b@x.com"`)**: Only listed emails can access
- **Public (`--public`)**: Anyone with the URL can access

## Two Publishing Systems

### System 1: sven-pages-worker (Cloudflare Workers KV)

- **URL**: `https://<worker-subdomain>.workers.dev/{folder}/`
- **Storage**: Cloudflare KV namespace `SVEN_PAGES`
- **CLI**: `~/.claude/skills/sven-pages/scripts/publish`
- ACLs managed per-folder via the CLI

### System 2: svenflow.ai (Cloud Run + Hono)

- **URL**: `https://svenflow.ai/pages/{folder}/`
- **Storage**: Filesystem `./pages/` dir on Cloud Run
- **ACLs**: Stored in `ACL_JSON` env var as JSON
- **Auth**: Google OAuth login for protected pages
- Pages without an ACL entry are **PRIVATE** (require admin login)
- Pages with `["*"]` in ACL are **PUBLIC**
- The `/pages` listing only shows PUBLIC pages

To make a page public on svenflow.ai, add `"pagename": ["*"]` to the `ACL_JSON` env var.

## For Group Chats

When publishing a report in a group chat, always share with participants:

```bash
# Get emails of group members, then publish with ACL
~/.claude/skills/sven-pages/scripts/publish ./report --acl "participant1@gmail.com,participant2@gmail.com"
```

## Architecture

- **Worker**: `sven-pages-worker` on Cloudflare Workers
- **Storage**: Cloudflare KV namespace `SVEN_PAGES`
- **URL**: `https://<worker-subdomain>.workers.dev/{folder}/`

## Admin Notes

The API key is stored at `~/.claude/skills/sven-pages/.api-key` and was set as a secret on the worker.

### Adding Google OAuth (Optional)

To enable Google login instead of email OTPs:

1. Go to [Google Cloud Console](https://console.cloud.google.com/apis/credentials)
2. Create OAuth Client ID (Web application)
3. Add redirect URI: `https://your-team.cloudflareaccess.com/cdn-cgi/access/callback`
4. In Cloudflare Zero Trust > Settings > Authentication > Add new > Google
5. Paste client ID and secret

Then create an Access Application protecting the worker domain.

## Legacy Deploy Script

The old `deploy` script still works for simple Cloudflare Pages deployments (no ACLs):

```bash
~/.claude/skills/sven-pages/scripts/deploy ./folder
```


## Google Analytics (GA4)

svenflow.ai has GA4 tracking on all pages. The gtag snippet is in `client/index.html` `<head>`.

- **Measurement ID**: Use `identity ga4.measurement_id` (stored in `config.local.yaml`, not checked in)
- **Dashboard**: https://analytics.google.com (Chrome profile 0)
- **Tracks**: Page views, scrolls, outbound clicks (enhanced measurement enabled)

## This box's deployment (set up 2026-09-25)

- **Account:** Cloudflare account under the assistant's own Google login (`identity assistant.email`), created via "Continue with Google". `npx wrangler login` is done on this Mac.
- **Worker:** `svenka-pages`, source copied to `~/code/svenka-pages-worker` from `~/dispatch/skills/sven-pages/worker`. It has its own KV namespace id in that `wrangler.toml`. Don't reuse the repo's KV id, because that belongs to a different account.
- **URL:** set in `~/dispatch/config.local.yaml` → `sven_pages.url`. The API key is in `.api-key` (0600). The secrets `API_KEY` and `ADMIN_EMAILS` are set on the worker.
- **Gotcha:** `publish` reads config.local.yaml with PyYAML. PyYAML was missing from the script's inline deps, so the import failed silently and printed "Set SVEN_PAGES_URL". It's fixed now (pyyaml added).
- **Private/ACL pages need Cloudflare Access (Zero Trust)** in front of the worker. The worker takes the viewer's email from the `Cf-Access-Jwt-Assertion` header, so without Access every non-public page returns **401**. Until Access is set up, pages are either public (`--public`) or unviewable.
- **Verify like a browser:** after publishing, curl the page *and every local asset* (JS/CSS). A page can return 200 while its JS returns 401/302, and you get a blank page. A single `curl` of the HTML won't catch that.
- **New workers.dev subdomain:** the TLS cert takes ~1–2 min after the first deploy. Until then curl fails with an SSL handshake error.
- **Default here is `--public` for non-sensitive pages** (Eric, 2026-09-25: "sven's link is public that's pretty cool"). Anything with account numbers or personal info stays private.
- **Zero Trust Free ($0, 50 seats) still requires a payment card on file** at checkout (`/zero-trust/checkout/payment`). Get admin approval before entering one. Nothing has been entered yet (2026-09-25).

## Caching
HTML, JS, CSS and JSON are served `Cache-Control: no-cache` (worker `cacheControlFor`, deployed 2026-10-04), so republished pages show up right away. Images and assets cache for 1h. If you replace an image under the same filename, rename it or add `?v=N` to its `src`. Worker source: `~/code/svenka-pages-worker` (`npx wrangler deploy`).
