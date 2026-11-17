frappe.ui.form.on("Social Media Account", {
	refresh(frm) {
		if (new URLSearchParams(window.location.search).get("oauth_status") === "connected") {
			frappe.show_alert({ message: __("Account connected"), indicator: "green" }, 7);
		}
		if (frm.is_new()) {
			frm.set_intro(__("Pick the workspace and platform, save, then click Authorize."), "blue");
			return;
		}
		const has_token = !!frm.doc.access_token; // shows ***** when a value is stored
		const authorize = () =>
			frappe.call({ method: "media_scheduler.api.connect_start",
				args: { platform: frm.doc.platform, account: frm.doc.name }, freeze: true })
				.then((r) => { window.location.href = r.message; });

		frm.add_custom_button(has_token ? __("Reauthorize") : __("Authorize"), authorize);
		if (!has_token) frm.change_custom_button_type(__("Authorize"), null, "primary");

		frm.add_custom_button(__("Test Connection"), () =>
			frm.call({ doc: frm.doc, method: "test_connection", freeze: true }).then((r) => {
				const res = r.message || {};
				frappe.show_alert({ message: frappe.utils.escape_html(res.message || ""), indicator: res.ok ? "green" : "red" }, 10);
			}));
	},
});
