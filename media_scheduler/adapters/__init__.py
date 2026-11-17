import frappe

from media_scheduler.adapters.base import BaseSocialAdapter, PublishResult
from media_scheduler.errors import PublishValidationError


def get_adapter(platform_name: str) -> BaseSocialAdapter:
	"""Build the adapter for a Social Platform record.

	Python adapters are resolved ONLY through the `social_platform_adapters` hook (an allowlist
	maintained in code), never from a dotted path stored in the database.
	"""
	platform = frappe.get_cached_doc("Social Platform", platform_name)
	if not platform.enabled:
		raise PublishValidationError(f"Platform {platform_name} is disabled", code="platform_disabled")

	if platform.adapter_type == "Generic REST":
		from media_scheduler.adapters.generic import GenericRestAdapter

		return GenericRestAdapter(platform)

	paths = frappe.get_hooks("social_platform_adapters").get(platform.adapter_key)
	if not paths:
		raise PublishValidationError(
			f"No adapter registered for key '{platform.adapter_key}'", code="adapter_not_registered"
		)
	return frappe.get_attr(paths[-1])(platform)


__all__ = ["BaseSocialAdapter", "PublishResult", "get_adapter"]
