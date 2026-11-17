import frappe
from frappe import _
from frappe.model.document import Document

from media_scheduler.permissions import assert_workspace_access


class SocialMediaAccount(Document):
	@frappe.whitelist()
	def test_connection(self):
		from media_scheduler.services import oauth_service

		self.check_permission("read")
		return oauth_service.test_connection(self)

	def validate(self):
		assert_workspace_access(self.workspace)
		if self.platform_account_id:
			dup = frappe.db.exists("Social Media Account", {
				"workspace": self.workspace, "platform": self.platform,
				"platform_account_id": self.platform_account_id, "name": ["!=", self.name]})
			if dup:
				frappe.throw(_("This account is already connected in this workspace ({0})").format(dup))
		if self.is_new():
			limit = frappe.db.get_value("Social Workspace", self.workspace, "max_accounts")
			if limit and frappe.db.count("Social Media Account", {"workspace": self.workspace}) >= limit:
				frappe.throw(_("Account limit ({0}) reached for this workspace").format(limit))
