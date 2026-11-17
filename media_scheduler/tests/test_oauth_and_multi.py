import json
from datetime import timedelta
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime

from media_scheduler.errors import OAuthFlowError
from media_scheduler.services import oauth_service, publisher
from media_scheduler.tests.test_generic_platform import DNS, REQ, fake_dns, fake_response, make_platform
from media_scheduler.tests.test_social_scheduler import make_account, make_post, make_workspace, purge_test_data

TOKEN_URL = "https://auth.example.com/token"
INFO_URL = "https://api.example.com/me"
OAUTH = {"authorize_url": "https://auth.example.com/authorize", "token_url": TOKEN_URL, "client_id": "cid",
	"client_secret": "csecret", "scopes": "read,write", "userinfo_url": INFO_URL, "account_id_path": "id",
	"account_name_path": "name", "use_pkce": 1}


def provider(token_status=200):
	def handler(method, url, **kw):
		if url == TOKEN_URL:
			if token_status != 200:
				return fake_response(token_status, {"error": "invalid_grant"})
			return fake_response(200, {"access_token": "AT-new", "refresh_token": "RT-new", "expires_in": 3600})
		if url == INFO_URL:
			return fake_response(200, {"id": "u-1", "name": "Jane"})
		return fake_response(200, {"id": "X"})
	return handler


class TestOAuth(FrappeTestCase):
	def setUp(self):
		purge_test_data()
		self.ws = make_workspace("TEST-WS-O")
		make_platform(**OAUTH)

	def tearDown(self):
		frappe.set_user("Administrator")
		purge_test_data()

	def url_state(self, **kw):
		url = oauth_service.start_connect("TestRest", self.ws, **kw)
		q = parse_qs(urlparse(url).query)
		return url, q, q["state"][0]

	def test_authorize_url(self):
		url, q, state = self.url_state()
		self.assertTrue(url.startswith("https://auth.example.com/authorize?"))
		self.assertEqual(q["scope"], ["read write"])  # Space separator
		self.assertEqual(q["response_type"], ["code"])
		self.assertEqual(q["code_challenge_method"], ["S256"])
		self.assertIn("/api/method/media_scheduler.api.oauth_callback", q["redirect_uri"][0])
		self.assertNotIn("csecret", url)  # the secret never goes to the browser

	def test_successful_connect_creates_account_with_tokens(self):
		_, _, state = self.url_state(account_name="Jane's page")
		with patch(REQ, side_effect=provider()), patch(DNS, side_effect=fake_dns):
			name = oauth_service.handle_callback("the-code", state)
		acc = frappe.get_doc("Social Media Account", name)
		self.assertEqual((acc.workspace, acc.platform, acc.platform_account_id, acc.status),
			(self.ws, "TestRest", "u-1", "Connected"))
		self.assertEqual(acc.account_name, "Jane's page")
		self.assertEqual(acc.get_password("access_token"), "AT-new")
		self.assertEqual(acc.get_password("refresh_token"), "RT-new")
		self.assertGreater(acc.token_expires_at, now_datetime())

	def test_pkce_verifier_and_secret_sent_to_token_endpoint(self):
		_, q, state = self.url_state()
		with patch(REQ, side_effect=provider()) as req, patch(DNS, side_effect=fake_dns):
			oauth_service.handle_callback("c", state)
		body = next(c.kwargs["data"] for c in req.call_args_list if c.args[1] == TOKEN_URL)
		self.assertEqual((body["grant_type"], body["code"], body["client_secret"]), ("authorization_code", "c", "csecret"))
		self.assertTrue(body["code_verifier"])

	def test_reconnect_same_identity_updates_not_duplicates(self):
		for _ in range(2):
			_, _, state = self.url_state()
			with patch(REQ, side_effect=provider()), patch(DNS, side_effect=fake_dns):
				oauth_service.handle_callback("c", state)
		self.assertEqual(frappe.db.count("Social Media Account", {"workspace": self.ws, "platform": "TestRest"}), 1)

	def test_bad_or_replayed_state_rejected(self):
		with self.assertRaises(OAuthFlowError) as c:
			oauth_service.handle_callback("c", "forged-state")
		self.assertEqual(c.exception.code, "state_invalid")
		_, _, state = self.url_state()
		with patch(REQ, side_effect=provider()), patch(DNS, side_effect=fake_dns):
			oauth_service.handle_callback("c", state)
			with self.assertRaises(OAuthFlowError):  # second use of the same state
				oauth_service.handle_callback("c", state)

	def test_state_bound_to_user(self):
		_, _, state = self.url_state()
		frappe.set_user("Guest")
		with self.assertRaises(OAuthFlowError) as c:
			oauth_service.handle_callback("c", state)
		self.assertEqual(c.exception.code, "state_user_mismatch")

	def test_provider_denied_and_token_failure(self):
		_, _, state = self.url_state()
		with self.assertRaises(OAuthFlowError) as c:
			oauth_service.handle_callback(None, state, error="access_denied")
		self.assertEqual(c.exception.code, "provider_denied")
		_, _, state = self.url_state()
		with patch(REQ, side_effect=provider(400)), patch(DNS, side_effect=fake_dns):
			with self.assertRaises(OAuthFlowError) as c:
				oauth_service.handle_callback("c", state)
		self.assertEqual(c.exception.code, "token_exchange_failed")

	def test_reconnect_cannot_swap_identity(self):
		acc = make_account(self.ws, "TestRest", pid="someone-else")
		_, _, state = self.url_state(account=acc)
		with patch(REQ, side_effect=provider()), patch(DNS, side_effect=fake_dns):
			with self.assertRaises(OAuthFlowError) as c:
				oauth_service.handle_callback("c", state)
		self.assertEqual(c.exception.code, "identity_mismatch")

	def test_no_access_to_workspace_cannot_start(self):
		email = "ws.a.user@example.com"
		frappe.get_doc({"doctype": "User", "email": email, "first_name": "X", "send_welcome_email": 0,
			"roles": [{"role": "Social Editor"}]}).insert()
		frappe.set_user(email)
		with self.assertRaises(frappe.PermissionError):
			oauth_service.start_connect("TestRest", self.ws)

	def test_refresh_replaces_token_and_keeps_old_refresh_token_if_not_returned(self):
		acc = frappe.get_doc("Social Media Account", make_account(self.ws, "TestRest"))
		acc.access_token, acc.refresh_token = "AT-old", "RT-old"
		acc.token_expires_at = add_to_date(now_datetime(), seconds=30)
		acc.save()
		with patch(REQ, return_value=fake_response(200, {"access_token": "AT-2", "expires_in": 100})), patch(DNS, side_effect=fake_dns):
			oauth_service.ensure_fresh_token(acc)
		acc.reload()
		self.assertEqual((acc.get_password("access_token"), acc.get_password("refresh_token")), ("AT-2", "RT-old"))

	def test_failed_refresh_marks_account_for_reauthorization(self):
		from media_scheduler.errors import AuthError

		acc = frappe.get_doc("Social Media Account", make_account(self.ws, "TestRest"))
		acc.access_token, acc.refresh_token = "AT-old", "RT-old"
		acc.token_expires_at = add_to_date(now_datetime(), seconds=-10)
		acc.save()
		with patch(REQ, side_effect=provider(400)), patch(DNS, side_effect=fake_dns):
			with self.assertRaises(AuthError):
				oauth_service.ensure_fresh_token(acc)
		self.assertEqual(frappe.db.get_value("Social Media Account", acc.name, "status"), "Needs Reauthorization")


class TestOnePostManyNetworks(FrappeTestCase):
	"""The headline feature: ONE post, several different networks, independent outcomes."""

	def setUp(self):
		purge_test_data()
		self.ws = make_workspace("TEST-WS-M")
		make_platform()  # TestRest   -> one.example.com style (host from account_config)
		make_platform(platform_name="TestRest2", publish_url="https://two.example.com/p", post_id_path="id",
			body_template='{"message": {{ content | tojson }}}', auth_scheme="Custom Header", auth_param_name="X-Key")
		self.a1, self.a2 = self.account("TestRest", {"host": "one.example.com"}), self.account("TestRest2")

	def tearDown(self):
		purge_test_data()

	def account(self, platform, cfg=None):
		acc = frappe.get_doc("Social Media Account", make_account(self.ws, platform))
		acc.access_token = f"tok-{platform}"
		if cfg:
			acc.extra_config = json.dumps(cfg)
		acc.save()
		return acc.name

	def run_post(self, handler):
		post = make_post(self.ws, [self.a1, self.a2])
		frappe.db.set_value("Social Post", post.name, "status", "Queued")
		with patch(REQ, side_effect=handler) as req, patch(DNS, side_effect=fake_dns):
			publisher.run_post(post.name)
		post.reload()
		return post, req

	def test_all_networks_succeed_each_in_own_format(self):
		def handler(method, url, **kw):
			return fake_response(200, {"data": {"id": "ONE"}} if "one." in url else {"id": "TWO"})

		post, req = self.run_post(handler)
		self.assertEqual(post.status, "Published")
		self.assertEqual([t.published_post_id for t in post.target_accounts], ["ONE", "TWO"])
		calls = {c.args[1]: c.kwargs for c in req.call_args_list}
		self.assertEqual(calls["https://one.example.com/api/post"]["json"], {"text": "hello"})
		self.assertEqual(calls["https://two.example.com/p"]["json"], {"message": "hello"})  # different body shape
		self.assertEqual(calls["https://one.example.com/api/post"]["headers"]["Authorization"], "Bearer tok-TestRest")
		self.assertEqual(calls["https://two.example.com/p"]["headers"]["X-Key"], "tok-TestRest2")  # different auth
		self.assertEqual(frappe.db.count("Publish Log", {"post": post.name, "success": 1}), 2)

	def test_one_network_down_does_not_block_the_other(self):
		def handler(method, url, **kw):
			if "two." in url:
				return fake_response(401, {"error": "bad token"})
			return fake_response(200, {"data": {"id": "ONE"}})

		post, _ = self.run_post(handler)
		self.assertEqual(post.status, "Partially Published")
		self.assertEqual([t.target_status for t in post.target_accounts], ["Published", "Failed"])
