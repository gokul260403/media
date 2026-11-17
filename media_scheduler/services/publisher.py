"""Claim + run + finalize. The only code that talks to platform adapters."""

import frappe
from datetime import datetime, timedelta, timezone

from frappe.utils import add_to_date, get_datetime, now_datetime

from media_scheduler.adapters import get_adapter
from media_scheduler.adapters.http import redact
from media_scheduler.errors import AdapterError, AuthError, PublishValidationError

JOB_TIMEOUT = 600  # seconds; must stay below Settings.stuck_after_minutes


def utcnow():
	return datetime.now(timezone.utc).replace(tzinfo=None)


def compute_parent_status(statuses):
	s = set(statuses)
	if not s:
		return "Failed"
	if s & {"Pending", "Processing"}:
		return "Processing"
	if s == {"Published"}:
		return "Published"
	if "Published" in s:
		return "Partially Published"
	return "Failed"


# --- claiming -----------------------------------------------------------------


def claim(name):
	"""Atomically move Scheduled -> Queued. Returns a job token if THIS caller won, else None."""
	token = frappe.generate_hash(length=10)
	frappe.db.sql(
		"""UPDATE `tabSocial Post`
		SET status='Queued', claimed_at=%s, claimed_by=%s
		WHERE name=%s AND status='Scheduled'""",
		(now_datetime(), token, name),
	)
	won = frappe.db.sql("SELECT ROW_COUNT()")[0][0] == 1
	frappe.db.commit()
	return token if won else None


def enqueue(name, token):
	frappe.enqueue(
		"media_scheduler.services.publisher.run_post",
		queue="long",
		timeout=JOB_TIMEOUT,
		job_id=f"media_scheduler::{name}::{token}",
		deduplicate=True,
		post=name,
	)


# --- worker -------------------------------------------------------------------


def run_post(post):
	frappe.db.sql(
		"UPDATE `tabSocial Post` SET status='Processing' WHERE name=%s AND status='Queued'", (post,)
	)
	if frappe.db.sql("SELECT ROW_COUNT()")[0][0] != 1:
		return  # cancelled or already picked up
	frappe.db.commit()

	doc = frappe.get_doc("Social Post", post)
	settings = frappe.get_cached_doc("Social Bridge Settings")
	now = utcnow()
	for row in doc.target_accounts:
		if row.target_status != "Pending":
			continue
		if row.next_retry_at and get_datetime(row.next_retry_at) > now:
			continue
		_publish_target(doc, row, settings)
	finalize(post)


def _set_target(row, **values):
	frappe.db.set_value("Post Target", row.name, values, update_modified=False)


def _log(doc, row, attempt, started, success, platform_post_id=None, response_status=None, error=None):
	frappe.get_doc(
		{
			"doctype": "Publish Log",
			"post": doc.name,
			"workspace": doc.workspace,
			"social_account": row.social_account,
			"target_row": row.name,
			"attempt": attempt,
			"success": 1 if success else 0,
			"started_at": started,
			"ended_at": now_datetime(),
			"response_status": response_status,
			"platform_post_id": platform_post_id,
			"error_code": error.code if error else None,
			"error_message": redact(error) if error else None,
		}
	).insert(ignore_permissions=True)


def _check_account(doc, account):
	from media_scheduler.services.oauth_service import ensure_fresh_token

	if account.workspace != doc.workspace:
		raise PublishValidationError("Account belongs to a different workspace", code="workspace_mismatch")
	if account.status != "Connected":
		raise AuthError(f"Account status is {account.status}", code="account_not_connected")
	ensure_fresh_token(account)
	if account.token_expires_at and get_datetime(account.token_expires_at) <= now_datetime():
		frappe.db.set_value("Social Media Account", account.name, "status", "Expired")
		raise AuthError("Access token has expired", code="token_expired")


def _publish_target(doc, row, settings):
	attempt = (row.attempt_count or 0) + 1
	key = row.idempotency_key or f"{doc.name}-{row.name}"
	started = now_datetime()
	# Persist "Processing" BEFORE calling out: if we die mid-call this row is flagged for review
	# instead of being silently re-posted.
	_set_target(row, target_status="Processing", attempt_count=attempt, last_attempt_at=started,
		idempotency_key=key, next_retry_at=None)
	frappe.db.commit()

	try:
		account = frappe.get_doc("Social Media Account", row.social_account)
		_check_account(doc, account)
		content = row.custom_content or doc.master_content or ""
		media = [{"file_url": m.file, "media_type": m.media_type} for m in doc.media]
		result = get_adapter(account.platform).publish(account, content, media, key)
		_set_target(row, target_status="Published", published_post_id=result.platform_post_id,
			published_at=result.published_at or now_datetime(), error_code=None, error_message=None)
		_log(doc, row, attempt, started, True, result.platform_post_id, result.response_status)
	except AdapterError as e:
		_handle_failure(doc, row, e, settings, attempt, started)
	except Exception:
		frappe.db.rollback()
		trace = redact(frappe.get_traceback(), limit=4000)
		frappe.log_error(title="Social publish: unexpected error", message=trace)
		err = AdapterError("Unexpected error. Verify on the platform before retrying.", code="unexpected")
		_set_target(row, target_status="Needs Review", error_code=err.code, error_message=str(err))
		_log(doc, row, attempt, started, False, error=err)
	frappe.db.commit()


def _delays(settings):
	raw = (settings.retry_backoff_seconds or "60,300,1800").split(",")
	return [int(x) for x in raw if x.strip().isdigit()] or [60, 300, 1800]


def _handle_failure(doc, row, err, settings, attempt, started):
	message = redact(err)
	if isinstance(err, AuthError) and err.code in ("http_401", "token_expired", "no_token"):
		frappe.db.set_value("Social Media Account", row.social_account, "status", "Needs Reauthorization")
	if err.retryable and attempt < (settings.max_retries or 3):
		delays = _delays(settings)
		delay = err.retry_after or delays[min(attempt - 1, len(delays) - 1)]
		_set_target(row, target_status="Pending", next_retry_at=utcnow() + timedelta(seconds=delay),
			error_code=err.code, error_message=message)
	else:
		_set_target(row, target_status="Failed", error_code=err.code, error_message=message)
	_log(doc, row, attempt, started, False, error=err)


# --- finalize / recovery ------------------------------------------------------


def finalize(name):
	rows = frappe.get_all(
		"Post Target",
		filters={"parent": name, "parenttype": "Social Post"},
		fields=["target_status", "next_retry_at", "error_message", "social_account"],
	)
	if any(r.target_status == "Pending" for r in rows):
		now = utcnow()
		times = [get_datetime(r.next_retry_at) if r.next_retry_at else now for r in rows if r.target_status == "Pending"]
		frappe.db.set_value("Social Post", name, {"status": "Scheduled", "next_run_at": min(times),
			"claimed_at": None, "claimed_by": None}, update_modified=False)
		frappe.db.commit()
		return "Scheduled"

	status = compute_parent_status([r.target_status for r in rows])
	problems = [f"{r.social_account}: {r.error_message}" for r in rows if r.target_status in ("Failed", "Needs Review")]
	frappe.db.set_value("Social Post", name, {"status": status, "error_summary": "\n".join(problems)[:1000] or None,
		"claimed_at": None, "claimed_by": None}, update_modified=False)
	frappe.db.commit()
	owner = frappe.db.get_value("Social Post", name, "owner")
	frappe.publish_realtime("social_post_update", {"post": name, "status": status}, user=owner)
	return status


def recover_stuck():
	settings = frappe.get_cached_doc("Social Bridge Settings")
	cutoff = add_to_date(now_datetime(), minutes=-(settings.stuck_after_minutes or 15))
	stuck = frappe.get_all("Social Post", filters={"status": ["in", ["Queued", "Processing"]],
		"claimed_at": ["<", cutoff]}, pluck="name")
	for name in stuck:
		# A target left in Processing may or may not have been posted: never auto-retry it.
		for t in frappe.get_all("Post Target", filters={"parent": name, "parenttype": "Social Post",
				"target_status": "Processing"}, pluck="name"):
			frappe.db.set_value("Post Target", t, {"target_status": "Needs Review", "error_code": "stuck",
				"error_message": "Worker stopped mid-publish. Check the platform before retrying."},
				update_modified=False)
		finalize(name)
