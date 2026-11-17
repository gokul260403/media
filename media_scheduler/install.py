import frappe

ROLES = ("Social Manager", "Social Editor")


def ensure_roles():
	for role in ROLES:
		if not frappe.db.exists("Role", role):
			frappe.get_doc({"doctype": "Role", "role_name": role, "desk_access": 1}).insert(
				ignore_permissions=True
			)


META_SCOPES = "instagram_basic,instagram_content_publish,pages_show_list,pages_read_engagement"

SEED_PLATFORMS = [
	{"platform_name": "LinkedIn", "adapter_type": "Python Adapter", "adapter_key": "linkedin",
		"api_version": "202511", "char_limit": 3000, "media_types": "",
		"authorize_url": "https://www.linkedin.com/oauth/v2/authorization",
		"token_url": "https://www.linkedin.com/oauth/v2/accessToken", "scopes": "openid,profile,w_member_social",
		"scope_separator": "Space", "userinfo_url": "https://api.linkedin.com/v2/userinfo",
		"account_id_path": "sub", "account_name_path": "name"},
	{"platform_name": "Instagram", "adapter_type": "Python Adapter", "adapter_key": "meta",
		"api_version": "v21.0", "char_limit": 2200, "requires_media": 1, "media_types": "Image",
		"authorize_url": "https://www.facebook.com/v21.0/dialog/oauth",
		"token_url": "https://graph.facebook.com/v21.0/oauth/access_token", "scopes": META_SCOPES,
		"scope_separator": "Comma"},
	{"platform_name": "Facebook", "adapter_type": "Python Adapter", "adapter_key": "meta",
		"api_version": "v21.0", "char_limit": 63206, "media_types": "Image,Video",
		"authorize_url": "https://www.facebook.com/v21.0/dialog/oauth",
		"token_url": "https://graph.facebook.com/v21.0/oauth/access_token", "scopes": META_SCOPES},
	# Example of a no-code platform. Disabled until you review it and add an account.
	{"platform_name": "Mastodon", "enabled": 0, "adapter_type": "Generic REST", "char_limit": 500,
		"publish_url": "{{ account_config.instance_url }}/api/v1/statuses", "http_method": "POST",
		"auth_scheme": "Bearer", "body_format": "JSON", "post_id_path": "id",
		"headers_template": '{"Idempotency-Key": {{ idempotency_key | tojson }}}',
		"body_template": '{"status": {{ content | tojson }}, "visibility": "public"}'},
]


def seed_platforms():
	"""Insert missing defaults only. Never overwrites a platform you have edited."""
	for row in SEED_PLATFORMS:
		if not frappe.db.exists("Social Platform", row["platform_name"]):
			frappe.get_doc({"doctype": "Social Platform", **row}).insert(ignore_permissions=True)
