frappe.ui.form.on("Social Post", {
	refresh(frm) {
		const editable = frm.is_new() || ["Draft", "Scheduled", "Cancelled"].includes(frm.doc.status);
		if (editable && frm.doc.workspace) {
			// Pick many accounts at once: one post, many networks.
			frm.add_custom_button(__("Add Accounts"), () => {
				new frappe.ui.form.MultiSelectDialog({
					doctype: "Social Media Account",
					target: frm,
					setters: { platform: null },
					add_filters_group: 0,
					get_query: () => ({ filters: { workspace: frm.doc.workspace, status: "Connected" } }),
					action(selections) {
						const have = new Set((frm.doc.target_accounts || []).map((r) => r.social_account));
						selections.filter((n) => !have.has(n)).forEach((n) => frm.add_child("target_accounts", { social_account: n }));
						frm.refresh_field("target_accounts");
						cur_dialog.hide();
					},
				});
			});
		}
		if (frm.is_new()) return;
		const call = (method, args) =>
			frm.call({ doc: frm.doc, method, args, freeze: true }).then(() => frm.reload_doc());

		if (["Draft", "Cancelled", "Scheduled"].includes(frm.doc.status)) {
			frm.add_custom_button(__("Schedule"), () => call("schedule"), __("Actions"));
			frm.add_custom_button(__("Publish Now"), () =>
				frappe.confirm(__("Publish to all targets now?"), () => call("publish_now")), __("Actions"));
		}
		if (["Draft", "Scheduled", "Queued"].includes(frm.doc.status)) {
			frm.add_custom_button(__("Cancel Post"), () => call("cancel"), __("Actions"));
		}
		if (["Failed", "Partially Published"].includes(frm.doc.status)) {
			frm.add_custom_button(__("Retry Failed Targets"), () => call("retry_failed"), __("Actions"));
			if ((frm.doc.target_accounts || []).some((r) => r.target_status === "Needs Review")) {
				frm.add_custom_button(__("Retry 'Needs Review' (may duplicate)"), () =>
					frappe.confirm(__("Check the platform first. This can create a duplicate post. Continue?"),
						() => call("retry_failed", { include_needs_review: 1 })), __("Actions"));
			}
		}
	},
});
