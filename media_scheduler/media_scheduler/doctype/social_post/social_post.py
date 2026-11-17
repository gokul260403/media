import frappe
from frappe import _
from frappe.model.document import Document

from media_scheduler.services import post_service


class SocialPost(Document):
	def validate(self):
		before = self.get_doc_before_save()
		if before and before.status in post_service.LOCKED:
			frappe.throw(_("A post that is {0} cannot be edited").format(before.status))
		post_service.validate_post(self)

	@frappe.whitelist()
	def schedule(self):
		post_service.schedule(self)

	@frappe.whitelist()
	def publish_now(self):
		post_service.publish_now(self)

	@frappe.whitelist()
	def cancel(self):
		post_service.cancel(self)

	@frappe.whitelist()
	def retry_failed(self, include_needs_review=False):
		post_service.retry_failed(self, frappe.parse_json(include_needs_review))
