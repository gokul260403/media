"""Business logic shared by the Desk UI, the scheduler and the public REST API."""

from datetime import timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import frappe
from frappe import _
from frappe.utils import get_first_day, get_datetime, now_datetime

from media_scheduler.permissions import assert_workspace_access
from media_scheduler.services import publisher

LOCKED = ("Queued", "Processing", "Published", "Partially Published")
SCHEDULABLE_FROM = ("Draft", "Cancelled", "Scheduled")
COUNTED = ("Scheduled", "Queued", "Processing", "Published", "Partially Published")


def to_utc(value, tz_name):
	try:
		tz = ZoneInfo(tz_name)
	except (ZoneInfoNotFoundError, ValueError, TypeError):
		frappe.throw(_("Unknown timezone {0}").format(tz_name))
	return get_datetime(value).replace(tzinfo=tz).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def validate_post(doc, strict=False):
	"""strict=True is used when scheduling/publishing; plain saves only enforce isolation + limits."""
	if not doc.workspace:
		frappe.throw(_("Workspace is required"))
	assert_workspace_access(doc.workspace)
	ws = frappe.db.get_value("Social Workspace", doc.workspace, ["is_active", "timezone"], as_dict=True)
	if not ws:
		frappe.throw(_("Workspace {0} does not exist").format(doc.workspace))
	if not doc.timezone:
		doc.timezone = ws.timezone or "UTC"
	if strict:
		if not ws.is_active:
			frappe.throw(_("Workspace {0} is inactive").format(doc.workspace))
		if not doc.target_accounts:
			frappe.throw(_("Select at least one target account"))

	seen = set()
	for row in doc.target_accounts:
		if row.social_account in seen:
			frappe.throw(_("Row {0}: duplicate account {1}").format(row.idx, row.social_account))
		seen.add(row.social_account)
		acc = frappe.db.get_value("Social Media Account", row.social_account,
			["workspace", "platform", "status"], as_dict=True)
		if not acc:
			frappe.throw(_("Row {0}: account {1} not found").format(row.idx, row.social_account))
		if acc.workspace != doc.workspace:  # never allowed, even for drafts
			frappe.throw(_("Row {0}: account belongs to another workspace").format(row.idx), frappe.PermissionError)
		platform = frappe.get_cached_doc("Social Platform", acc.platform)
		content = row.custom_content or doc.master_content or ""
		if platform.char_limit and len(content) > platform.char_limit:
			frappe.throw(_("Row {0}: {1} allows at most {2} characters").format(row.idx, acc.platform, platform.char_limit))
		allowed = {t.strip() for t in (platform.media_types or "").split(",") if t.strip()}
		for m in doc.media:
			if m.media_type not in allowed:
				frappe.throw(_("Row {0}: {1} does not accept {2} media").format(row.idx, acc.platform, m.media_type))
		if strict:
			if acc.status != "Connected":
				frappe.throw(_("Row {0}: account is {1}. Reconnect it first").format(row.idx, acc.status))
			if not content.strip():
				frappe.throw(_("Row {0}: post text is empty").format(row.idx))
			if not platform.enabled:
				frappe.throw(_("Row {0}: platform {1} is disabled").format(row.idx, acc.platform))
			if platform.requires_media and not doc.media:
				frappe.throw(_("Row {0}: {1} posts need media").format(row.idx, acc.platform))


def _check_quota(doc):
	limit = frappe.db.get_value("Social Workspace", doc.workspace, "max_posts_per_month")
	if not limit:
		return
	used = frappe.db.count("Social Post", {"workspace": doc.workspace, "status": ["in", COUNTED],
		"modified": [">=", get_first_day(now_datetime())], "name": ["!=", doc.name]})
	if used >= limit:
		frappe.throw(_("Monthly post limit ({0}) reached for this workspace").format(limit))


def schedule(doc):
	if doc.status not in SCHEDULABLE_FROM:
		frappe.throw(_("A post in status {0} cannot be scheduled").format(doc.status))
	if not doc.scheduled_time:
		frappe.throw(_("Scheduled time is required"))
	validate_post(doc, strict=True)
	_check_quota(doc)
	run_at = to_utc(doc.scheduled_time, doc.timezone)
	if run_at < publisher.utcnow() - timedelta(minutes=1):
		frappe.throw(_("Scheduled time is in the past. Use Publish Now instead"))
	doc.status = "Scheduled"
	doc.next_run_at = run_at
	doc.save()
	return doc


def publish_now(doc):
	if doc.status not in SCHEDULABLE_FROM:
		frappe.throw(_("A post in status {0} cannot be published").format(doc.status))
	validate_post(doc, strict=True)
	_check_quota(doc)
	doc.status = "Scheduled"
	doc.next_run_at = publisher.utcnow()
	doc.save()
	token = publisher.claim(doc.name)
	if token:
		publisher.enqueue(doc.name, token)
	return doc


def cancel(doc):
	frappe.db.sql("UPDATE `tabSocial Post` SET status='Cancelled' WHERE name=%s "
		"AND status IN ('Draft','Scheduled','Queued')", (doc.name,))
	if frappe.db.sql("SELECT ROW_COUNT()")[0][0] != 1:
		frappe.throw(_("Too late to cancel: the post is already {0}").format(
			frappe.db.get_value("Social Post", doc.name, "status")))
	frappe.db.commit()
	return doc


def retry_failed(doc, include_needs_review=False):
	if doc.status not in ("Failed", "Partially Published"):
		frappe.throw(_("Only failed posts can be retried"))
	assert_workspace_access(doc.workspace)
	states = ["Failed"] + (["Needs Review"] if include_needs_review else [])
	rows = [r for r in doc.target_accounts if r.target_status in states]
	if not rows:
		frappe.throw(_("No target to retry"))
	for r in rows:
		frappe.db.set_value("Post Target", r.name, {"target_status": "Pending", "attempt_count": 0,
			"next_retry_at": None, "error_code": None, "error_message": None}, update_modified=False)
	frappe.db.set_value("Social Post", doc.name, {"status": "Scheduled", "next_run_at": publisher.utcnow()},
		update_modified=False)
	frappe.db.commit()
	token = publisher.claim(doc.name)
	if token:
		publisher.enqueue(doc.name, token)


def create_post(workspace, title, content, scheduled_time, target_accounts, timezone=None):
	"""Entry point for the REST API. Goes through normal permission checks (no ignore_permissions)."""
	assert_workspace_access(workspace)
	doc = frappe.get_doc({
		"doctype": "Social Post", "workspace": workspace, "title": title, "master_content": content,
		"scheduled_time": scheduled_time, "timezone": timezone,
		"target_accounts": [{"social_account": a} for a in target_accounts],
	})
	doc.insert()
	return schedule(doc)
