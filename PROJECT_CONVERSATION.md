# media_scheduler – Project Conversation Summary

A multi-company social media post scheduler built as a Frappe v15 app.
One post can publish to many networks at once. Any social network can be added by configuration.

---

## 1. Goal and Decisions

| Topic | Decision |
|---|---|
| Source design | `Frappe social media post scheduler.docx` (name `social_bridge` replaced by `media_scheduler`) |
| Tenancy | Multi-company, using **Social Workspace** (no ERPNext, so no Company DocType) |
| Frappe version | v15 (15.126.0) |
| OAuth credentials | Platform-level (Client ID/Secret stored on the Social Platform record) |
| Instagram | Facebook Login for Instagram (recommended) |
| Media storage | **Not decided, parked.** The S3/Drive markdown file was only a reference for the LinkedIn "enter key + secret, click Authorize, token auto-created" experience |
| Current goal | Connect a real LinkedIn account and publish one real post |

User preferences: short, clear, layman answers. Analyse and summarize first, decide together, then change code. No unexpected code actions.

---

## 2. Environment

- Bench: `/home/umar/buildx`, site `media.local`, web port **8004**
- Redis for this bench: cache **13004**, queue **11004** (13000/11000 belong to another bench)
- Browse at `http://localhost:8004` (the OAuth redirect URI is built from the request host)
- Start the app: `cd /home/umar/buildx && bench start` (restart so workers load new code)
- `allow_tests` is true on media.local

---

## 3. What Is Built

### Phase 1: Publishing core
- DocTypes: Social Workspace, Social Bridge Settings, Social Platform, Social Media Account, Social Post, Post Target, Post Media, Publish Log
- Roles: Social Manager, Social Editor (created by `install.ensure_roles`)
- **Tenant isolation** (fail-closed): `permission_query_conditions`, `has_permission` hooks, User Permissions on Social Workspace
- **Scheduler**
  - Cron every minute finds due posts (batch of 50)
  - Atomic claim: `UPDATE ... WHERE status='Scheduled'` plus `ROW_COUNT()`
  - Job runs on the `long` queue
  - Per-target status; parent status computed from targets
  - Retries with backoff 60 / 300 / 1800 s, max 3
  - Stuck-job recovery every 10 minutes
  - "Needs Review" is never auto-retried

### Phase 2: Any platform + OAuth
- **Adapter architecture**
  - `BaseSocialAdapter.publish()` returns `PublishResult` or raises `AdapterError` subclasses (`RetryableError`, `AuthError`, `PublishValidationError`)
  - Python adapters only load through the hook allowlist `social_platform_adapters`
  - **Generic REST adapter**: Jinja templates for headers/body, SSRF guard (https only, public IPs, no redirects), tokens never exposed to templates, `post_id_path` to read the post ID from the response
  - Platform rules (char limit, media required, media types, enabled) live on the Social Platform record
- **One post to many networks**: a post has several targets, each with its own status
- **Generic OAuth 2.0** (authorization-code, optional PKCE)
  - State is random, one-time, 10 minutes, bound to the user, stored in Redis
  - Callback: `/api/method/media_scheduler.api.oauth_callback`
  - Tokens refreshed just-in-time and by a daily job
  - Account identity read from `userinfo_url`
  - Pre-expiry alert (Notification Log, once per expiry)
  - **Test Connection** button on the account
- **UI**
  - Social Post: Schedule, Publish Now, Cancel, Retry, Add Accounts (multi-select)
  - Social Media Account: Authorize / Reauthorize, Test Connection
  - Account list: Connect Account dialog
- **Seeded platforms**: LinkedIn (Python adapter), Instagram and Facebook (Meta stub), Mastodon (Generic REST example, disabled)

### Tests
41 tests passing (scheduler, generic platform, OAuth + multi-network, connection check). HTTP is mocked and DNS is faked.

### Live data in the site
Workspace `test`; platform `Httpbin Test` (Generic REST to httpbin.org); account `SMA-00017`; post `SP-2026-00014` (Published).

---

## 4. Key Files (`media_scheduler/media_scheduler/`)

- `hooks.py`, `install.py`, `permissions.py`, `errors.py`, `tasks.py`, `api.py`
- `adapters/`: `base.py`, `http.py`, `generic.py`, `linkedin.py`, `instagram.py` (MetaAdapter stub)
- `services/`: `publisher.py`, `post_service.py`, `oauth_service.py`
- `doctype/`: the eight DocTypes with controllers and JS
- `tests/`: `test_social_scheduler.py`, `test_generic_platform.py`, `test_oauth_and_multi.py`, `test_connection_check.py`

---

## 5. Problems Hit and Fixes

| Problem | Fix |
|---|---|
| Redis not running on migrate | Started `redis_cache` and `redis_queue` for this bench |
| No `Company` DocType (no ERPNext) | Created `Social Workspace` as tenant |
| Test data surviving rollback (publisher commits), "Account limit (10) reached" | `purge_test_data()` in setUp/tearDown |
| Generator script vanished | Added OAuth fields directly in `social_platform.json` |
| Redis TimeoutError in tests (global DNS patch hit localhost) | `fake_dns` only fakes `*example.com` |
| Platform validation blocked saving seeded LinkedIn | Only require authorize_url and token_url together; client_id checked at Connect time |

---

## 6. Q&A Highlights (plain language)

- **Single vs multi company**: single = one business's data; multi = many businesses in one site, each seeing only its own data (Social Workspace).
- **Instagram vs LinkedIn, which is less work?** LinkedIn. Instagram needs a public media URL, a Professional account linked to a Facebook Page, and Meta app review.
- **Does LinkedIn expire in 60 days?** Yes. Refresh tokens are normally partner-only, so re-authorize about every 60 days. The app warns before expiry.
- **Where do I see the output of a published post?** Open the Social Post's targets and the Publish Log.
- **Storage (S3/Drive)**: not part of this task; revisit with Instagram/media.

---

## 7. LinkedIn Setup (where we are now)

Done:
1. LinkedIn developer app `media Scheduler` created (personal account used because Page creation was blocked: "not enough connections"). Client ID begins `86mybwy…`
2. Products added: **Share on LinkedIn** and **Sign In with LinkedIn using OpenID Connect**

Next:
4. **Auth tab**: copy Client ID and Primary Client Secret (never paste the secret in chat)
5. In the app at `http://localhost:8004` (Administrator): Social Platform → LinkedIn → OAuth → paste both → Save. Redirect URI should read
   `http://localhost:8004/api/method/media_scheduler.api.oauth_callback`
6. Register that exact URL under **Authorized redirect URLs** in LinkedIn's Auth tab. If LinkedIn rejects `http://localhost`, use an https tunnel plus `host_name`
7. **Social Media Account → Connect Account** (workspace `test`, platform LinkedIn) → Allow → confirm Connected and a Platform Account ID → **Test Connection** → create a Social Post targeting it → **Publish Now**

LinkedIn details: uses `/rest/posts`, header `Linkedin-Version` (seeded `202511`, **unverified**) and `X-Restli-Protocol-Version: 2.0.0`. Scopes: `openid profile w_member_social`. Visibility is hardcoded PUBLIC (a setting was offered, not requested).

---

## 8. Known Risks

- Nothing has been tested against live LinkedIn, Meta or Mastodon yet
- `Linkedin-Version` value and localhost redirect acceptance are unverified
- Instagram/Facebook is still a stub; LinkedIn media upload is not implemented
- README and `app_publisher` are scaffold defaults

---

## 9. Pending

- Finish LinkedIn connect and publish one real post (Section 7)
- Later, not yet requested: Instagram/Facebook publishing (long-lived token), LinkedIn media upload, media storage (S3 with signed URLs recommended, undecided), calendar view and notifications, per-workspace API keys and docs, real-account testing, hardening
