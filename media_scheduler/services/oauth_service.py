"""Generic OAuth 2.0 (authorization-code, optional PKCE) driven entirely by Social Platform records."""

import base64
import hashlib
import re
import secrets
from datetime import timedelta
from urllib.parse import parse_qsl, urlencode

import frappe
from frappe.utils import get_datetime, get_url, now_datetime

from media_scheduler.adapters.generic import _loads
from media_scheduler.adapters.http import assert_public_url, dig, redact, request
from media_scheduler.errors import AdapterError, AuthError, OAuthFlowError
from media_scheduler.permissions import assert_workspace_access

STATE_TTL = 600
REDIRECT_PATH = "/api/method/media_scheduler.api.oauth_callback"


def _key(state):
	return f"media_scheduler_oauth:{state}"


def redirect_uri():
	return get_url(REDIRECT_PATH)


def _platform(name):
	p = frappe.get_cached_doc("Social Platform", name)
	if not p.enabled:
		frappe.throw(frappe._("Platform {0} is disabled").format(name))
	if not (p.authorize_url and p.token_url and p.client_id):
		frappe.throw(frappe._("OAuth is not configured for {0}. Paste a token manually or fill the OAuth section").format(name))
	return p


def start_connect(platform, workspace=None, account=None, account_name=None):
	"""Return the provider authorize URL. The state is bound to the current user and stored server-side."""
	p = _platform(platform)
	if account:
		acc = frappe.get_doc("Social Media Account", account)
		acc.check_permission("write")
		if acc.platform != platform:
			frappe.throw(frappe._("Account belongs to a different platform"))
		workspace = acc.workspace
	else:
		frappe.has_permission("Social Media Account", "create", throw=True)
	if not workspace:
		frappe.throw(frappe._("Workspace is required"))
	assert_workspace_access(workspace)

	state = secrets.token_urlsafe(32)
	verifier = secrets.token_urlsafe(64) if p.use_pkce else None
	frappe.cache().set_value(_key(state), {
		"user": frappe.session.user, "platform": platform, "workspace": workspace,
		"account": account, "account_name": account_name, "verifier": verifier,
	}, expires_in_sec=STATE_TTL)

	scopes = [s for s in re.split(r"[,\s]+", p.scopes or "") if s]
	params = {"response_type": "code", "client_id": p.client_id, "redirect_uri": redirect_uri(), "state": state}
	if scopes:
		params["scope"] = ("," if p.scope_separator == "Comma" else " ").join(scopes)
	if verifier:
		digest = hashlib.sha256(verifier.encode()).digest()
		params["code_challenge"] = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
		params["code_challenge_method"] = "S256"
	params.update({str(k): str(v) for k, v in _loads(p.extra_authorize_params, "Extra authorize params").items()})
	sep = "&" if "?" in p.authorize_url else "?"
	return f"{p.authorize_url}{sep}{urlencode(params)}"


def _token_request(platform, fields):
	assert_public_url(platform.token_url)
	secret = platform.get_password("client_secret", raise_exception=False) or ""
	headers = {"Accept": "application/json"}
	body = dict(fields)
	if platform.token_auth_method == "Basic Auth Header":
		raw = f"{platform.client_id}:{secret}".encode()
		headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()
	else:
		body["client_id"] = platform.client_id
		body["client_secret"] = secret
	resp = request("POST", platform.token_url, headers=headers, data=body, allow_redirects=False)
	try:
		data = resp.json()
	except ValueError:
		data = dict(parse_qsl(resp.text))
	if not isinstance(data, dict) or not data.get("access_token"):
		raise AuthError("Token endpoint did not return an access_token", code="no_access_token")
	return data


def _store_tokens(account, data):
	account.access_token = data["access_token"]
	if data.get("refresh_token"):
		account.refresh_token = data["refresh_token"]
	expires_in = str(data.get("expires_in") or "")
	account.token_expires_at = now_datetime() + timedelta(seconds=int(expires_in)) if expires_in.isdigit() else None
	account.status = "Connected"
	account.expiry_notified_at = None  # new token: allow a fresh alert next time


def _identify(platform, token):
	if not platform.userinfo_url:
		return None, None
	assert_public_url(platform.userinfo_url)
	resp = request("GET", platform.userinfo_url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
		allow_redirects=False)
	info = resp.json()
	ident = dig(info, platform.account_id_path or "id")
	name = dig(info, platform.account_name_path or "name")
	return (str(ident) if ident is not None else None), (str(name) if name else None)


def handle_callback(code, state, error=None):
	"""Complete the flow. Returns the Social Media Account name. Raises OAuthFlowError(code)."""
	if not state:
		raise OAuthFlowError("state_missing")
	saved = frappe.cache().get_value(_key(state))
	frappe.cache().delete_value(_key(state))  # one-time use: a replayed callback finds nothing
	if not saved:
		raise OAuthFlowError("state_invalid")
	if saved["user"] != frappe.session.user:
		raise OAuthFlowError("state_user_mismatch")
	if error or not code:
		raise OAuthFlowError("provider_denied")

	platform = frappe.get_cached_doc("Social Platform", saved["platform"])
	fields = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri()}
	if saved.get("verifier"):
		fields["code_verifier"] = saved["verifier"]
	try:
		data = _token_request(platform, fields)
		ident, name = _identify(platform, data["access_token"])
	except AdapterError as e:
		frappe.log_error(title="Social OAuth: token exchange failed", message=redact(e, 2000))
		raise OAuthFlowError("token_exchange_failed")

	account = _upsert_account(saved, platform, ident, name)
	_store_tokens(account, data)
	account.save()
	_notify_adapter(platform, account, data)
	return account.name


def _upsert_account(saved, platform, ident, name):
	if saved.get("account"):
		account = frappe.get_doc("Social Media Account", saved["account"])
		if account.platform_account_id and ident and account.platform_account_id != ident:
			raise OAuthFlowError("identity_mismatch")  # reconnecting must not swap the account
		if ident and not account.platform_account_id:
			account.platform_account_id = ident
		return account
	existing = ident and frappe.db.get_value("Social Media Account", {
		"workspace": saved["workspace"], "platform": platform.name, "platform_account_id": ident})
	if existing:
		return frappe.get_doc("Social Media Account", existing)
	return frappe.get_doc({
		"doctype": "Social Media Account", "workspace": saved["workspace"], "platform": platform.name,
		"account_name": saved.get("account_name") or name or f"{platform.name} {ident or ''}".strip(),
		"platform_account_id": ident, "owner_user": frappe.session.user,
	})


def _notify_adapter(platform, account, data):
	"""Python adapters may define on_connected(account, token_data), e.g. Meta long-lived token exchange."""
	if platform.adapter_type != "Python Adapter":
		return
	from media_scheduler.adapters import get_adapter

	hook = getattr(get_adapter(platform.name), "on_connected", None)
	if hook:
		hook(account, data)


# --- refresh --------------------------------------------------------------------


def refresh_access_token(account):
	platform = frappe.get_cached_doc("Social Platform", account.platform)
	refresh = account.get_password("refresh_token", raise_exception=False)
	if not refresh:
		raise AuthError("No refresh token", code="no_refresh_token")
	data = _token_request(platform, {"grant_type": "refresh_token", "refresh_token": refresh})
	_store_tokens(account, data)
	account.save(ignore_permissions=True)


def ensure_fresh_token(account, skew_seconds=300):
	"""Refresh just before use. If it cannot be refreshed the caller's expiry check reports it."""
	if not account.token_expires_at:
		return
	if get_datetime(account.token_expires_at) - now_datetime() > timedelta(seconds=skew_seconds):
		return
	if not account.get_password("refresh_token", raise_exception=False):
		return
	try:
		refresh_access_token(account)
	except AdapterError as e:
		frappe.log_error(title="Social OAuth: refresh failed", message=redact(e, 2000))
		frappe.db.set_value("Social Media Account", account.name, "status", "Needs Reauthorization")
		raise AuthError("Token refresh failed. Reconnect the account", code="token_expired")


def notify_reauthorize(account, subject):
	"""Desk notification (and email if the user has that enabled) with a link to the account."""
	user = account.owner_user or account.owner
	frappe.get_doc({
		"doctype": "Notification Log", "for_user": user, "type": "Alert", "subject": subject,
		"document_type": "Social Media Account", "document_name": account.name,
	}).insert(ignore_permissions=True)


def refresh_expiring_tokens(days=7):
	"""Daily. Refresh what can be refreshed; alert ONCE per expiry for what needs a human to click Authorize."""
	soon = now_datetime() + timedelta(days=days)
	for name in frappe.get_all("Social Media Account", filters={"status": "Connected",
			"token_expires_at": ["<=", soon]}, pluck="name"):
		account = frappe.get_doc("Social Media Account", name)
		expired = get_datetime(account.token_expires_at) <= now_datetime()
		try:
			if account.get_password("refresh_token", raise_exception=False):
				refresh_access_token(account)
				continue
		except AdapterError as e:
			frappe.log_error(title="Social OAuth: scheduled refresh failed", message=redact(e, 2000))
			frappe.db.set_value("Social Media Account", name, "status", "Needs Reauthorization")
			notify_reauthorize(account, f"{account.account_name}: automatic token refresh failed. Please authorize again.")
			continue
		# No refresh token: only a person can renew it.
		if expired:
			frappe.db.set_value("Social Media Account", name, "status", "Expired")
		if not account.expiry_notified_at:
			when = "has expired" if expired else f"expires on {get_datetime(account.token_expires_at):%d %b %Y}"
			notify_reauthorize(account, f"{account.account_name}: the {account.platform} connection {when}. Please authorize again.")
			frappe.db.set_value("Social Media Account", name, "expiry_notified_at", now_datetime(), update_modified=False)
	frappe.db.commit()


def test_connection(account):
	"""Prove the stored token still works, and belongs to this account."""
	platform = frappe.get_cached_doc("Social Platform", account.platform)
	if not account.get_password("access_token", raise_exception=False):
		return {"ok": False, "message": "No access token yet. Click Authorize."}
	try:
		ensure_fresh_token(account)
		token = account.get_password("access_token", raise_exception=False)
		if not platform.userinfo_url:
			return {"ok": True, "message": "A token is stored. This platform has no userinfo URL, so it cannot be verified."}
		ident, name = _identify(platform, token)
	except AdapterError as e:
		return {"ok": False, "message": f"{e.code}: {redact(e, 200)}"}
	if ident and account.platform_account_id and ident != account.platform_account_id:
		return {"ok": False, "message": "The token belongs to a different account than this record."}
	return {"ok": True, "message": f"Connected as {name or ident or 'unknown'}"}
