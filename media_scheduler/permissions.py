"""Workspace (tenant) isolation.

Isolation is fail-closed: anyone who is not a System Manager only sees rows whose
`workspace` is granted to them through a User Permission on "Social Workspace".
A user with no grants sees nothing.
"""

import frappe
from frappe import _

ADMIN_ROLE = "System Manager"


def allowed_workspaces(user=None):
	"""Return None for unrestricted users, otherwise the list of permitted workspace names."""
	user = user or frappe.session.user
	if user == "Administrator" or ADMIN_ROLE in frappe.get_roles(user):
		return None
	grants = frappe.permissions.get_user_permissions(user).get("Social Workspace", [])
	return [g["doc"] for g in grants]


def assert_workspace_access(workspace, user=None):
	allowed = allowed_workspaces(user)
	if allowed is not None and workspace not in allowed:
		frappe.throw(_("You do not have access to workspace {0}").format(workspace), frappe.PermissionError)


def _conditions(doctype, user=None):
	allowed = allowed_workspaces(user)
	if allowed is None:
		return ""
	if not allowed:
		return "1=0"
	values = ", ".join(frappe.db.escape(w) for w in allowed)
	return f"`tab{doctype}`.`workspace` in ({values})"


def social_post_query(user=None, doctype=None):
	return _conditions("Social Post", user)


def social_account_query(user=None, doctype=None):
	return _conditions("Social Media Account", user)


def publish_log_query(user=None, doctype=None):
	return _conditions("Publish Log", user)


def has_workspace_permission(doc, ptype=None, user=None, **kwargs):
	"""Controllers may only deny. Return False for foreign workspaces, otherwise defer."""
	workspace = doc.get("workspace")
	if not workspace:
		return None
	allowed = allowed_workspaces(user)
	if allowed is not None and workspace not in allowed:
		return False
	return None
