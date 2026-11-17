import json
import socket
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from media_scheduler.adapters import get_adapter
from media_scheduler.errors import PublishValidationError, RetryableError
from media_scheduler.tests.test_social_scheduler import make_account, make_workspace, purge_test_data

REQ = "media_scheduler.adapters.http.requests.request"
DNS = "media_scheduler.adapters.http.socket.getaddrinfo"
PUBLIC = [(2, 1, 6, "", ("93.184.216.34", 443))]
_real_getaddrinfo = socket.getaddrinfo


def fake_dns(host, *args, **kwargs):
	"""Only the fake example.com hosts resolve to a public IP. Redis/MariaDB lookups stay real."""
	return PUBLIC if str(host).endswith("example.com") else _real_getaddrinfo(host, *args, **kwargs)


def make_platform(**kw):
	vals = {"doctype": "Social Platform", "platform_name": "TestRest", "adapter_type": "Generic REST",
		"publish_url": "https://{{ account_config.host }}/api/post", "auth_scheme": "Bearer",
		"headers_template": '{"Idempotency-Key": {{ idempotency_key | tojson }}}',
		"body_template": '{"text": {{ content | tojson }}}', "post_id_path": "data.id", "char_limit": 100}
	vals.update(kw)
	return frappe.get_doc(vals).insert()


def fake_response(status=200, body=None, headers=None):
	r = MagicMock(status_code=status, headers=headers or {}, text=json.dumps(body or {}))
	r.json.return_value = body or {}
	return r


class TestGenericPlatform(FrappeTestCase):
	def setUp(self):
		purge_test_data()
		self.ws = make_workspace("TEST-WS-G")
		self.platform = make_platform()
		self.account = frappe.get_doc("Social Media Account", make_account(self.ws, "TestRest"))
		self.account.extra_config = '{"host": "social.example.com"}'
		self.account.access_token = "TOP-SECRET-TOKEN"
		self.account.save()

	def tearDown(self):
		purge_test_data()

	def publish(self, content="hi \"quoted\"\nline2"):
		with patch(DNS, side_effect=fake_dns):
			return get_adapter("TestRest").publish(self.account, content, None, "key-1")

	def test_publish_renders_templates_and_extracts_id(self):
		with patch(REQ, return_value=fake_response(200, {"data": {"id": 42}})) as req:
			result = self.publish()
		self.assertEqual(result.platform_post_id, "42")
		args, kw = req.call_args
		self.assertEqual(args[1], "https://social.example.com/api/post")
		self.assertEqual(kw["json"], {"text": "hi \"quoted\"\nline2"})  # tojson keeps JSON valid
		self.assertEqual(kw["headers"]["Authorization"], "Bearer TOP-SECRET-TOKEN")
		self.assertEqual(kw["headers"]["Idempotency-Key"], "key-1")
		self.assertFalse(kw["allow_redirects"])

	def test_token_never_available_to_templates(self):
		self.platform.body_template = '{"t": {{ account.access_token | default("none") | tojson }}}'
		self.platform.save()
		frappe.clear_cache()
		with patch(REQ, return_value=fake_response(200, {"data": {"id": 1}})) as req:
			self.publish()
		self.assertEqual(req.call_args[1]["json"], {"t": "none"})

	def test_private_address_blocked(self):
		with patch(DNS, return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]), patch(REQ) as req:
			with self.assertRaises(PublishValidationError):
				get_adapter("TestRest").publish(self.account, "x", None, "k")
		req.assert_not_called()

	def test_5xx_is_retryable(self):
		with patch(REQ, return_value=fake_response(503, {"error": "down"})):
			with self.assertRaises(RetryableError):
				self.publish()

	def test_header_post_id_path(self):
		self.platform.post_id_path = "header:x-id"
		self.platform.save()
		frappe.clear_cache()
		with patch(REQ, return_value=fake_response(201, {}, {"x-id": "abc"})):
			self.assertEqual(self.publish().platform_post_id, "abc")

	def test_bad_template_rejected_on_save(self):
		self.platform.body_template = '{"text": {{ content }}}'  # missing | tojson -> invalid JSON
		with self.assertRaises(frappe.ValidationError):
			self.platform.save()

	def test_http_url_rejected_on_save(self):
		self.platform.publish_url = "http://plain.example.com/post"
		with self.assertRaises(frappe.ValidationError):
			self.platform.save()

	def test_unregistered_python_adapter_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			make_platform(platform_name="Evil", adapter_type="Python Adapter", adapter_key="os.system")

	def test_disabled_platform_cannot_publish(self):
		self.platform.enabled = 0
		self.platform.save()
		frappe.clear_cache()
		with self.assertRaises(PublishValidationError):
			get_adapter("TestRest")

	def test_char_limit_enforced_from_platform_record(self):
		from media_scheduler.tests.test_social_scheduler import make_post

		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc({"doctype": "Social Post", "title": "t", "workspace": self.ws,
				"master_content": "x" * 101,
				"target_accounts": [{"social_account": self.account.name}]}).insert()
