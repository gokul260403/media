const OAUTH_MESSAGES = {
	state_missing: __("Connection failed: missing state. Please try again."),
	state_invalid: __("Connection expired or was already used. Please try again."),
	state_user_mismatch: __("Connection failed: you were logged in as a different user."),
	provider_denied: __("The platform did not grant access."),
	token_exchange_failed: __("Could not get a token from the platform. Check the client ID/secret and redirect URL."),
	identity_mismatch: __("That login is a different account from the one you are reconnecting."),
};

frappe.listview_settings["Social Media Account"] = {
	onload(listview) {
		listview.page.add_inner_button(__("Connect Account"), () => {
			const d = new frappe.ui.Dialog({
				title: __("Connect a social account"),
				fields: [
					{ fieldname: "workspace", fieldtype: "Link", options: "Social Workspace", label: __("Workspace"), reqd: 1 },
					{
						fieldname: "platform", fieldtype: "Link", options: "Social Platform", label: __("Platform"), reqd: 1,
						get_query: () => ({ filters: { enabled: 1, authorize_url: ["!=", ""] } }),
					},
					{ fieldname: "account_name", fieldtype: "Data", label: __("Label (optional)") },
				],
				primary_action_label: __("Continue to platform"),
				primary_action(values) {
					frappe.call({ method: "media_scheduler.api.connect_start", args: values, freeze: true })
						.then((r) => { window.location.href = r.message; });
				},
			});
			d.show();
		});

		const q = new URLSearchParams(window.location.search);
		if (q.get("oauth_status") === "error") {
			frappe.show_alert({ message: OAUTH_MESSAGES[q.get("oauth_code")] || __("Connection failed."), indicator: "red" }, 10);
		}
	},
};
