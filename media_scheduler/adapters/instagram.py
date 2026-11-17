from media_scheduler.adapters.base import BaseSocialAdapter
from media_scheduler.errors import PublishValidationError


class MetaAdapter(BaseSocialAdapter):
	"""Instagram / Facebook via Facebook Login. Implemented in phase 4-5."""

	def publish(self, account, content, media=None, idempotency_key=None):
		name = self.platform_doc.platform_name if self.platform_doc else "Meta"
		raise PublishValidationError(f"{name} publishing is not implemented yet", code="not_implemented")
