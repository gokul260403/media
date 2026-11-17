from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date

from media_scheduler.adapters.base import PublishResult
from media_scheduler.adapters.http import redact
from media_scheduler.errors import AuthError, RetryableError
from media_scheduler.permissions import allowed_workspaces, social_post_query
from media_scheduler.services import post_service, publisher

PUB = "media_scheduler.services.publisher.get_adapter"
TEST_USER = "ws.a.user@example.com"


def purge_test_data():
	"""The publisher commits (as it must), so fixtures outlive rollback(): delete them explicitly."""
	frappe.db.rollback()
	spaces = frappe.get_all("Social Workspace", filters={"name": ["like", "TEST-WS-%"]}, pluck="name")
	for ws in spaces:
		posts = frappe.get_all("Social Post", filters={"workspace": ws}, pluck="name")
		for post in posts:
			frappe.db.delete("Post Target", {"parent": post, "parenttype": "Social Post"})
			frappe.db.delete("Post Media", {"parent": post, "parenttype": "Social Post"})
		frappe.db.delete("Publish Log", {"workspace": ws})
		frappe.db.delete("Social Post", {"workspace": ws})
		frappe.db.delete("Social Media Account", {"workspace": ws})
		frappe.db.delete("Social Workspace", {"name": ws})
	for name in ("TestRest", "TestRest2", "Evil"):
		frappe.db.delete("Social Platform", {"name": name})
	frappe.db.delete("User Permission", {"user": TEST_USER})
	if frappe.db.exists("User", TEST_USER):
		frappe.delete_doc("User", TEST_USER, force=True)
	frappe.db.commit()


def make_workspace(name):
	if not frappe.db.exists("Social Workspace", name):
		frappe.get_doc({"doctype": "Social Workspace", "workspace_name": name, "timezone": "Asia/Kolkata"}).insert()
	return name


def make_account(ws, platform="LinkedIn", pid=None):
	return frappe.get_doc({"doctype": "Social Media Account", "account_name": f"{ws}-{platform}", "workspace": ws,
		"platform": platform, "platform_account_id": pid or frappe.generate_hash(length=8)}).insert().name


def make_post(ws, accounts):
	return frappe.get_doc({"doctype": "Social Post", "title": "t", "workspace": ws, "master_content": "hello",
		"target_accounts": [{"social_account": a} for a in accounts]}).insert()


class FakeAdapter:
	def __init__(self, behaviour):
		self.behaviour = behaviour

	def publish(self, account, content, media=None, idempotency_key=None):
		b = self.behaviour[account.platform]
		if isinstance(b, Exception):
			raise b
		return PublishResult(platform_post_id=b)


class TestSocialScheduler(FrappeTestCase):
	def setUp(self):
		purge_test_data()
		self.ws_a = make_workspace("TEST-WS-A")
		self.ws_b = make_workspace("TEST-WS-B")

	def tearDown(self):
		purge_test_data()

	def queue(self, post):
		frappe.db.set_value("Social Post", post.name, {"status": "Queued"})

	def test_parent_status(self):
		cs = publisher.compute_parent_status
		self.assertEqual(cs(["Published", "Published"]), "Published")
		self.assertEqual(cs(["Published", "Failed"]), "Partially Published")
		self.assertEqual(cs(["Published", "Needs Review"]), "Partially Published")
		self.assertEqual(cs(["Failed", "Failed"]), "Failed")
		self.assertEqual(cs(["Published", "Pending"]), "Processing")

	def test_claim_is_exclusive(self):
		post = make_post(self.ws_a, [make_account(self.ws_a)])
		frappe.db.set_value("Social Post", post.name, "status", "Scheduled")
		self.assertTrue(publisher.claim(post.name))
		self.assertIsNone(publisher.claim(post.name))  # second scanner loses

	def test_partial_publish_and_error_isolation(self):
		a1, a2 = make_account(self.ws_a, "LinkedIn"), make_account(self.ws_a, "Facebook")
		post = make_post(self.ws_a, [a1, a2])
		self.queue(post)
		with patch(PUB, return_value=FakeAdapter({"LinkedIn": "urn:1", "Facebook": AuthError("nope", code="http_401")})):
			publisher.run_post(post.name)
		post.reload()
		self.assertEqual(post.status, "Partially Published")
		self.assertEqual([t.target_status for t in post.target_accounts], ["Published", "Failed"])
		self.assertEqual(frappe.db.get_value("Social Media Account", a2, "status"), "Needs Reauthorization")
		self.assertEqual(frappe.db.count("Publish Log", {"post": post.name}), 2)

	def test_retryable_failure_reschedules_without_losing_post(self):
		post = make_post(self.ws_a, [make_account(self.ws_a)])
		self.queue(post)
		with patch(PUB, return_value=FakeAdapter({"LinkedIn": RetryableError("boom", code="network")})):
			publisher.run_post(post.name)
		post.reload()
		self.assertEqual(post.status, "Scheduled")
		self.assertEqual(post.target_accounts[0].target_status, "Pending")
		self.assertGreater(post.next_run_at, publisher.utcnow())

	def test_retry_limit_ends_in_failed(self):
		post = make_post(self.ws_a, [make_account(self.ws_a)])
		frappe.db.set_value("Post Target", post.target_accounts[0].name, "attempt_count", 2)
		self.queue(post)
		with patch(PUB, return_value=FakeAdapter({"LinkedIn": RetryableError("boom")})):
			publisher.run_post(post.name)
		self.assertEqual(frappe.db.get_value("Social Post", post.name, "status"), "Failed")

	def test_cancelled_post_is_not_processed(self):
		post = make_post(self.ws_a, [make_account(self.ws_a)])
		frappe.db.set_value("Social Post", post.name, "status", "Cancelled")
		with patch(PUB) as adapter:
			publisher.run_post(post.name)
		adapter.assert_not_called()

	def test_stuck_processing_target_needs_review_not_retried(self):
		post = make_post(self.ws_a, [make_account(self.ws_a)])
		frappe.db.set_value("Post Target", post.target_accounts[0].name, "target_status", "Processing")
		frappe.db.set_value("Social Post", post.name, {"status": "Processing",
			"claimed_at": add_to_date(None, hours=-2)})
		publisher.recover_stuck()
		post.reload()
		self.assertEqual(post.target_accounts[0].target_status, "Needs Review")
		self.assertEqual(post.status, "Failed")

	def test_cross_workspace_account_rejected(self):
		foreign = make_account(self.ws_b)
		with self.assertRaises(frappe.PermissionError):
			make_post(self.ws_a, [foreign])

	def test_workspace_isolation_for_normal_user(self):
		email = TEST_USER
		if not frappe.db.exists("User", email):
			frappe.get_doc({"doctype": "User", "email": email, "first_name": "WsA", "send_welcome_email": 0,
				"roles": [{"role": "Social Editor"}]}).insert()
		self.assertEqual(allowed_workspaces(email), [])  # fail closed
		self.assertEqual(social_post_query(email), "1=0")
		frappe.get_doc({"doctype": "User Permission", "user": email, "allow": "Social Workspace",
			"for_value": self.ws_a}).insert()
		frappe.clear_cache(user=email)
		self.assertEqual(allowed_workspaces(email), [self.ws_a])
		self.assertIn(self.ws_a, social_post_query(email))
		self.assertNotIn(self.ws_b, social_post_query(email))

	def test_to_utc(self):
		self.assertEqual(str(post_service.to_utc("2026-11-10 10:00:00", "Asia/Kolkata")), "2026-11-10 04:30:00")

	def test_redaction(self):
		out = redact('Authorization: Bearer abc.def-123 access_token=SECRET1&x=1 {"access_token": "SECRET2"}')
		self.assertNotIn("abc.def-123", out)
		self.assertNotIn("SECRET1", out)
		self.assertNotIn("SECRET2", out)
