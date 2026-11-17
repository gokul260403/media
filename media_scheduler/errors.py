"""Normalised adapter errors. The worker only reasons about these classes."""


class AdapterError(Exception):
	code = "adapter_error"
	retryable = False

	def __init__(self, message, code=None, retry_after=None):
		super().__init__(message)
		if code:
			self.code = code
		self.retry_after = retry_after


class RetryableError(AdapterError):
	"""Network problems, 5xx, 429. Safe to retry with backoff."""

	code = "retryable"
	retryable = True


class AuthError(AdapterError):
	"""Expired/revoked token or missing permission. Needs user action, never auto-retried."""

	code = "auth_error"


class PublishValidationError(AdapterError):
	"""Platform rejected the content/media. Retrying will not help."""

	code = "validation_error"


class OAuthFlowError(Exception):
	"""Connect flow failure. `code` is a short, fixed identifier safe to show to users."""

	def __init__(self, code):
		super().__init__(code)
		self.code = code
