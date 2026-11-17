"""Scheduler entry points (registered in hooks.py)."""

import frappe

from media_scheduler.services import publisher

SCAN_BATCH = 50


def check_and_enqueue():
	now = publisher.utcnow()
	due = frappe.get_all(
		"Social Post",
		filters={"status": "Scheduled", "next_run_at": ["<=", now]},
		pluck="name",
		order_by="next_run_at asc",
		limit=SCAN_BATCH,
	)
	for name in due:
		job = publisher.claim(name)
		if job:
			publisher.enqueue(name, job)


def recover_stuck():
	publisher.recover_stuck()


def refresh_expiring_tokens():
	from media_scheduler.services import oauth_service

	oauth_service.refresh_expiring_tokens()
