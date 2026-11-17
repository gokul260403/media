"""Public REST API. Thin wrappers: all rules live in services/post_service.py.

POST /api/method/media_scheduler.api.schedule_post
Auth: `Authorization: token <api_key>:<api_secret>` of a user holding the Social Editor role and a
User Permission on the target Social Workspace.
"""

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit

from media_scheduler.permissions import assert_workspace_access
from media_scheduler.services import post_service


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=60, seconds=60)
def schedule_post(workspace, title, content, scheduled_time, target_accounts, timezone=None):
	accounts = frappe.parse_json(target_accounts)
	if not isinstance(accounts, list) or not accounts:
		frappe.throw(_("target_accounts must be a non-empty list"))
	doc = post_service.create_post(workspace, title, content, scheduled_time, accounts, timezone)
	return {"status": doc.status, "post_name": doc.name, "next_run_at": str(doc.next_run_at)}


@frappe.whitelist(methods=["POST"])
def cancel_post(post_name):
	doc = frappe.get_doc("Social Post", post_name)
	doc.check_permission("write")
	post_service.cancel(doc)
	return {"status": "Cancelled", "post_name": post_name}


@frappe.whitelist(methods=["GET"])
def get_post(post_name):
	doc = frappe.get_doc("Social Post", post_name)
	doc.check_permission("read")
	return {
		"post_name": doc.name, "status": doc.status, "error_summary": doc.error_summary,
		"targets": [{"account": t.social_account, "platform": t.platform, "status": t.target_status,
			"platform_post_id": t.published_post_id, "error": t.error_message} for t in doc.target_accounts],
	}


@frappe.whitelist(methods=["POST"])
def connect_start(platform, workspace=None, account=None, account_name=None):
	"""Returns the provider URL the browser should be sent to."""
	from media_scheduler.services import oauth_service

	return oauth_service.start_connect(platform, workspace, account, account_name)


@frappe.whitelist(methods=["GET"])
def oauth_callback(code=None, state=None, error=None, **kwargs):
	"""Provider redirects the browser here. Messages shown to the user are fixed codes, never provider text."""
	from media_scheduler.errors import OAuthFlowError
	from media_scheduler.services import oauth_service

	try:
		name = oauth_service.handle_callback(code, state, error)
		location = f"/app/social-media-account/{name}?oauth_status=connected"
	except OAuthFlowError as e:
		location = f"/app/social-media-account?oauth_status=error&oauth_code={e.code}"
	frappe.local.response["type"] = "redirect"
	frappe.local.response["location"] = location
