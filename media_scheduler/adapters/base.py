from dataclasses import dataclass
from datetime import datetime


@dataclass
class PublishResult:
	"""Returned on success. Failures are raised as AdapterError subclasses."""

	platform_post_id: str | None = None
	published_at: datetime | None = None
	response_status: str | None = None


class BaseSocialAdapter:
	def __init__(self, platform_doc=None):
		self.platform_doc = platform_doc  # Social Platform record

	def publish(self, account, content: str, media: list | None = None, idempotency_key: str | None = None) -> PublishResult:
		"""Publish to ONE account.

		media: list of {"file_url": str, "media_type": "Image"|"Video"}.
		Must raise RetryableError / AuthError / PublishValidationError on failure.
		"""
		raise NotImplementedError
