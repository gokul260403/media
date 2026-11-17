import json

import frappe
from frappe import _
from frappe.model.document import Document

from media_scheduler.adapters.generic import GenericRestAdapter, _loads

SAMPLE_CTX = {
	"content": "Sample post \"text\"\nsecond line", "media": [], "idempotency_key": "sample-key",
	"account": {"name": "SMA-00001", "account_name": "Sample", "platform_account_id": "123", "account_type": ""},
	"account_config": {}, "platform": {"name": "", "api_version": ""}, "platform_config": {},
}


class SocialPlatform(Document):
	def _validate_oauth(self):
		self.redirect_uri = frappe.utils.get_url("/api/method/media_scheduler.api.oauth_callback")
		# client_id is checked when someone clicks Connect, so default platforms stay editable.
		if not (self.authorize_url or self.token_url):
			return
		for field in ("authorize_url", "token_url"):
			if not self.get(field):
				frappe.throw(_("{0} is required when OAuth is configured").format(self.meta.get_label(field)))
		for field in ("authorize_url", "token_url", "userinfo_url"):
			value = self.get(field)
			if value and not value.startswith("https://"):
				frappe.throw(_("{0} must start with https://").format(self.meta.get_label(field)))
		_loads(self.extra_authorize_params, "Extra authorize params")

	def validate(self):
		self._validate_oauth()
		if self.adapter_type == "Python Adapter":
			if not self.adapter_key:
				frappe.throw(_("Adapter key is required for a Python adapter"))
			if self.adapter_key not in frappe.get_hooks("social_platform_adapters"):
				frappe.throw(_("Adapter key {0} is not registered in any app's hooks.py").format(self.adapter_key))
			return

		if not self.publish_url:
			frappe.throw(_("Publish URL is required for Generic REST"))
		for field in ("platform_config",):
			_loads(self.get(field), self.meta.get_label(field))
		# Dry-run the templates so mistakes show up now, not at publish time.
		adapter = GenericRestAdapter(self)
		ctx = {**SAMPLE_CTX, "platform": {"name": self.platform_name, "api_version": self.api_version or ""}}
		try:
			url = adapter.render(self.publish_url, ctx)
			if "{{" not in self.publish_url and not url.startswith("https://"):
				frappe.throw(_("Publish URL must start with https://"))
			_loads(adapter.render(self.headers_template, ctx), "Headers template")
			_loads(adapter.render(self.body_template, ctx), "Body template")
		except frappe.ValidationError:
			raise
		except Exception as e:
			frappe.throw(_("Template error: {0}").format(frappe.utils.escape_html(str(e))))
		if self.auth_scheme != "Bearer" and not self.auth_param_name:
			frappe.throw(_("Auth parameter name is required for {0}").format(self.auth_scheme))
