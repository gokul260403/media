import frappe
from frappe.utils import now_datetime

from media_scheduler.adapters.base import BaseSocialAdapter, PublishResult
from media_scheduler.adapters.http import request
from media_scheduler.errors import AuthError, PublishValidationError

POSTS_URL = "https://api.linkedin.com/rest/posts"


class LinkedInAdapter(BaseSocialAdapter):
	def publish(self, account, content, media=None, idempotency_key=None):
		if media:
			raise PublishValidationError("LinkedIn media upload is not implemented yet", code="media_unsupported")
		if not content or not content.strip():
			raise PublishValidationError("Post text is empty", code="empty_content")

		token = account.get_password("access_token", raise_exception=False)
		if not token:
			raise AuthError("No access token stored for this account", code="no_token")

		kind = "organization" if account.account_type == "Organization Page" else "person"
		body = {
			"author": f"urn:li:{kind}:{account.platform_account_id}",
			"commentary": content,
			"visibility": "PUBLIC",
			"distribution": {
				"feedDistribution": "MAIN_FEED",
				"targetEntities": [],
				"thirdPartyDistributionChannels": [],
			},
			"lifecycleState": "PUBLISHED",
			"isReshareDisabledByAuthor": False,
		}
		headers = {
			"Authorization": f"Bearer {token}",
			"Content-Type": "application/json",
			"X-Restli-Protocol-Version": "2.0.0",
			"Linkedin-Version": self.platform_doc.api_version,
		}
		resp = request("POST", POSTS_URL, headers=headers, json=body)
		return PublishResult(
			platform_post_id=resp.headers.get("x-restli-id") or resp.headers.get("x-linkedin-id"),
			published_at=now_datetime(),
			response_status=str(resp.status_code),
		)
