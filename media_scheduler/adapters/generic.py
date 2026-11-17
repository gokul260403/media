"""Config-driven adapter: publish to any simple REST API from a Social Platform record."""

import json

import frappe
from frappe.utils import now_datetime

from media_scheduler.adapters.base import BaseSocialAdapter, PublishResult
from media_scheduler.adapters.http import assert_public_url, dig, request
from media_scheduler.errors import AuthError, PublishValidationError


def _loads(text, what):
	text = (text or "").strip()
	if not text:
		return {}
	try:
		value = json.loads(text)
	except ValueError as e:
		raise PublishValidationError(f"{what} is not valid JSON after rendering: {e}", code="bad_template")
	if not isinstance(value, dict):
		raise PublishValidationError(f"{what} must be a JSON object", code="bad_template")
	return value


def build_context(platform, account, content, media, idempotency_key):
	"""Templates never see the token or any Password field."""
	return {
		"content": content,
		"media": media or [],
		"idempotency_key": idempotency_key,
		"account": {
			"name": account.name,
			"account_name": account.account_name,
			"platform_account_id": account.platform_account_id,
			"account_type": account.account_type,
		},
		"account_config": _loads(account.extra_config, "Account extra_config"),
		"platform": {"name": platform.platform_name, "api_version": platform.api_version},
		"platform_config": _loads(platform.platform_config, "Platform config"),
	}


class GenericRestAdapter(BaseSocialAdapter):
	def render(self, template, ctx):
		return frappe.render_template(template or "", ctx)

	def publish(self, account, content, media=None, idempotency_key=None):
		p = self.platform_doc
		if not p.publish_url:
			raise PublishValidationError("Platform has no publish URL", code="bad_config")
		token = account.get_password("access_token", raise_exception=False)
		if not token:
			raise AuthError("No access token stored for this account", code="no_token")

		ctx = build_context(p, account, content, media, idempotency_key)
		url = self.render(p.publish_url, ctx).strip()
		headers = {str(k): str(v) for k, v in _loads(self.render(p.headers_template, ctx), "Headers").items()}
		body = _loads(self.render(p.body_template, ctx), "Body")
		params = None

		if p.auth_scheme == "Bearer":
			headers["Authorization"] = f"Bearer {token}"
		elif p.auth_scheme == "Custom Header":
			headers[p.auth_param_name or "Authorization"] = token
		else:
			params = {p.auth_param_name or "access_token": token}

		assert_public_url(url)
		kwargs = {"headers": headers, "params": params, "allow_redirects": False}
		if p.body_format == "Form":
			kwargs["data"] = body
		else:
			kwargs["json"] = body
		resp = request(p.http_method or "POST", url, **kwargs)

		path = (p.post_id_path or "id").strip()
		if path.startswith("header:"):
			post_id = resp.headers.get(path[7:])
		else:
			try:
				post_id = dig(resp.json(), path)
			except ValueError:
				post_id = None
		return PublishResult(
			platform_post_id=str(post_id) if post_id is not None else None,
			published_at=now_datetime(),
			response_status=str(resp.status_code),
		)
