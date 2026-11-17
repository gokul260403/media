from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime

from media_scheduler.services import oauth_service
from media_scheduler.tests.test_generic_platform import DNS, REQ, fake_dns, fake_response, make_platform
from media_scheduler.tests.test_oauth_and_multi import OAUTH
from media_scheduler.tests.test_social_scheduler import make_account, make_workspace, purge_test_data

NO_EMAIL = "frappe.desk.doctype.notification_log.notification_log.NotificationLog.after_insert"


class TestConnectionAndAlerts(FrappeTestCase):
	def setUp(self):
		purge_test_data()
		self.ws = make_workspace("TEST-WS-C")
		make_platform(**OAUTH)
		self.acc = frappe.get_doc("Social Media Account", make_account(self.ws, "TestRest", pid="u-1"))

	def tearDown(self):
		frappe.db.delete("Notification Log", {"document_type": "Social Media Account", "document_name": self.acc.name})
		purge_test_data()

	def check(self, response):
		with patch(REQ, return_value=response), patch(DNS, side_effect=fake_dns):
			return oauth_service.test_connection(self.acc)

	def token(self, **kw):
		self.acc.access_token = "AT"
		self.acc.update(kw)
		self.acc.save()

	def alerts(self):
		return frappe.db.count("Notification Log", {"document_type": "Social Media Account", "document_name": self.acc.name})

	def test_no_token(self):
		res = oauth_service.test_connection(self.acc)
		self.assertFalse(res["ok"])
		self.assertIn("Authorize", res["message"])

	def test_valid_token(self):
		self.token()
		self.assertEqual(self.check(fake_response(200, {"id": "u-1", "name": "Jane"})), {"ok": True, "message": "Connected as Jane"})

	def test_rejected_token(self):
		self.token()
		res = self.check(fake_response(401, {"error": "expired"}))
		self.assertFalse(res["ok"])
		self.assertIn("http_401", res["message"])

	def test_token_of_another_account(self):
		self.token()
		self.assertFalse(self.check(fake_response(200, {"id": "someone-else", "name": "Mallory"}))["ok"])

	def test_expiry_alert_sent_once_without_refresh_token(self):
		self.token(token_expires_at=add_to_date(now_datetime(), days=3))
		with patch(NO_EMAIL):
			oauth_service.refresh_expiring_tokens()
			oauth_service.refresh_expiring_tokens()  # daily job runs again: must not spam
		self.assertEqual(self.alerts(), 1)
		self.assertEqual(frappe.db.get_value("Social Media Account", self.acc.name, "status"), "Connected")

	def test_expired_token_is_flagged_and_realert_after_new_token(self):
		self.token(token_expires_at=add_to_date(now_datetime(), hours=-1))
		with patch(NO_EMAIL):
			oauth_service.refresh_expiring_tokens()
		self.assertEqual(frappe.db.get_value("Social Media Account", self.acc.name, "status"), "Expired")
		self.acc.reload()
		oauth_service._store_tokens(self.acc, {"access_token": "AT2", "expires_in": 100})
		self.assertIsNone(self.acc.expiry_notified_at)

	def test_healthy_token_not_alerted(self):
		self.token(token_expires_at=add_to_date(now_datetime(), days=30))
		with patch(NO_EMAIL):
			oauth_service.refresh_expiring_tokens()
		self.assertEqual(self.alerts(), 0)
