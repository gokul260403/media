"""Single HTTP choke point: timeouts, error classification, secret redaction."""

import ipaddress
import re
import socket
from urllib.parse import urlparse

import requests

from media_scheduler.errors import AuthError, PublishValidationError, RetryableError

TIMEOUT = (5, 30)  # connect, read

_PATTERNS = (
	(re.compile(r"(?i)(bearer\s+)[A-Za-z0-9\-._~+/=]+"), r"\1[REDACTED]"),
	(re.compile(r"(?i)((?:access_token|refresh_token|client_secret|fb_exchange_token|code)=)[^&\s\"\']+"), r"\1[REDACTED]"),
	(re.compile(r"(?i)(\"(?:access_token|refresh_token|client_secret)\"\s*:\s*\")[^\"]+"), r"\1[REDACTED]"),
)


def redact(text, limit=500):
	text = str(text or "")
	for pattern, repl in _PATTERNS:
		text = pattern.sub(repl, text)
	return text[:limit]


def request(method, url, *, headers=None, json=None, params=None, data=None, timeout=TIMEOUT,
		allow_redirects=True):
	try:
		resp = requests.request(method, url, headers=headers, json=json, params=params, data=data, timeout=timeout,
			allow_redirects=allow_redirects)
	except (requests.Timeout, requests.ConnectionError) as e:
		raise RetryableError(redact(e), code="network")

	if resp.status_code < 400:
		return resp

	message = redact(resp.text)
	code = f"http_{resp.status_code}"
	if resp.status_code in (401, 403):
		raise AuthError(message, code=code)
	if resp.status_code == 429:
		retry_after = resp.headers.get("Retry-After", "")
		raise RetryableError(message, code="rate_limited", retry_after=int(retry_after) if retry_after.isdigit() else None)
	if resp.status_code >= 500:
		raise RetryableError(message, code=code)
	raise PublishValidationError(message, code=code)


def assert_public_url(url):
	"""SSRF guard: https only, and the host must not resolve to a private/loopback address."""
	parts = urlparse(url)
	if parts.scheme != "https" or not parts.hostname:
		raise PublishValidationError("URL must be https", code="bad_url")
	try:
		infos = socket.getaddrinfo(parts.hostname, parts.port or 443, proto=socket.IPPROTO_TCP)
	except socket.gaierror:
		raise PublishValidationError(f"Cannot resolve host {parts.hostname}", code="bad_url")
	for info in infos:
		if not ipaddress.ip_address(info[4][0]).is_global:
			raise PublishValidationError("URL resolves to a non-public address", code="bad_url")


def dig(data, path):
	"""Read a dotted path like 'data.0.id' from parsed JSON. None if missing."""
	for key in path.split("."):
		if isinstance(data, dict):
			data = data.get(key)
		elif isinstance(data, list) and key.isdigit() and int(key) < len(data):
			data = data[int(key)]
		else:
			return None
	return data
