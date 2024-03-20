# Copyright (c) 2021, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt


import frappe
from frappe import _, qb
from frappe.desk.notifications import clear_notifications
from frappe.model.document import Document
from frappe.utils import cint, create_batch


class TransactionDeletionRecord(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from erpnext.setup.doctype.transaction_deletion_record_item.transaction_deletion_record_item import (
			TransactionDeletionRecordItem,
		)

		amended_from: DF.Link | None
		clear_notifications: DF.Check
		company: DF.Link
		delete_bin_data: DF.Check
		delete_leads_and_addresses: DF.Check
		delete_transactions: DF.Check
		doctypes: DF.Table[TransactionDeletionRecordItem]
		doctypes_to_be_ignored: DF.Table[TransactionDeletionRecordItem]
		initialize_doctypes_table: DF.Check
		reset_company_default_values: DF.Check
		status: DF.Literal["Queued", "Running", "Failed", "Completed", "Cancelled"]
	# end: auto-generated types

	def __init__(self, *args, **kwargs):
		super(TransactionDeletionRecord, self).__init__(*args, **kwargs)
		self.batch_size = 5000

	def validate(self):
		frappe.only_for("System Manager")
		self.validate_doctypes_to_be_ignored()

	def validate_doctypes_to_be_ignored(self):
		doctypes_to_be_ignored_list = get_doctypes_to_be_ignored()
		for doctype in self.doctypes_to_be_ignored:
			if doctype.doctype_name not in doctypes_to_be_ignored_list:
				frappe.throw(
					_(
						"DocTypes should not be added manually to the 'Excluded DocTypes' table. You are only allowed to remove entries from it."
					),
					title=_("Not Allowed"),
				)

	def before_submit(self):
		if not self.doctypes_to_be_ignored:
			self.populate_doctypes_to_be_ignored_table()

	def reset_task_flags(self):
		self.clear_notifications = 0
		self.delete_bin_data = 0
		self.delete_leads_and_addresses = 0
		self.delete_transactions = 0
		self.reset_company_default_values = 0

	def before_save(self):
		self.status = ""
		self.reset_task_flags()

	def on_submit(self):
		self.db_set("status", "Queued")

	def on_cancel(self):
		self.db_set("status", "Cancelled")

	def chain_callback(self, method):
		frappe.enqueue(
			"frappe.utils.background_jobs.run_doc_method",
			doctype=self.doctype,
			name=self.name,
			doc_method=method,
			queue="long",
			enqueue_after_commit=True,
		)

	@frappe.whitelist()
	def start_deletion_process(self):
		self.delete_bins()
		self.delete_lead_addresses()
		self.reset_company_values()
		self.delete_notifications()
		self.initialize_doctypes_to_be_deleted_table()
		self.delete_company_transactions()

	def delete_notifications(self):
		if not self.clear_notifications:
			clear_notifications()
			self.db_set("clear_notifications", 1)
		self.chain_callback("initialize_doctypes_to_be_deleted_table")

	def populate_doctypes_to_be_ignored_table(self):
		doctypes_to_be_ignored_list = get_doctypes_to_be_ignored()
		for doctype in doctypes_to_be_ignored_list:
			self.append("doctypes_to_be_ignored", {"doctype_name": doctype})

	@frappe.whitelist()
	def delete_bins(self):
		if not self.delete_bin_data:
			frappe.db.sql(
				"""delete from `tabBin` where warehouse in
					(select name from tabWarehouse where company=%s)""",
				self.company,
			)
			self.db_set("delete_bin_data", 1)
		self.chain_callback(method="delete_lead_addresses")

	def delete_lead_addresses(self):
		"""Delete addresses to which leads are linked"""
		if not self.delete_leads_and_addresses:
			leads = frappe.get_all("Lead", filters={"company": self.company})
			leads = ["'%s'" % row.get("name") for row in leads]
			addresses = []
			if leads:
				addresses = frappe.db.sql_list(
					"""select parent from `tabDynamic Link` where link_name
					in ({leads})""".format(
						leads=",".join(leads)
					)
				)

				if addresses:
					addresses = ["%s" % frappe.db.escape(addr) for addr in addresses]

					frappe.db.sql(
						"""delete from `tabAddress` where name in ({addresses}) and
						name not in (select distinct dl1.parent from `tabDynamic Link` dl1
						inner join `tabDynamic Link` dl2 on dl1.parent=dl2.parent
						and dl1.link_doctype<>dl2.link_doctype)""".format(
							addresses=",".join(addresses)
						)
					)

					frappe.db.sql(
						"""delete from `tabDynamic Link` where link_doctype='Lead'
						and parenttype='Address' and link_name in ({leads})""".format(
							leads=",".join(leads)
						)
					)

				frappe.db.sql(
					"""update `tabCustomer` set lead_name=NULL where lead_name in ({leads})""".format(
						leads=",".join(leads)
					)
				)
			self.db_set("delete_leads_and_addresses", 1)
		self.chain_callback(method="reset_company_values")

	def reset_company_values(self):
		if not self.reset_company_default_values:
			company_obj = frappe.get_doc("Company", self.company)
			company_obj.total_monthly_sales = 0
			company_obj.sales_monthly_history = None
			company_obj.save()
			self.db_set("reset_company_default_values", 1)
		self.chain_callback(method="delete_notifications")

	def initialize_doctypes_to_be_deleted_table(self):
		if not self.initialize_doctypes_table:
			doctypes_to_be_ignored_list = self.get_doctypes_to_be_ignored_list()
			docfields = self.get_doctypes_with_company_field(doctypes_to_be_ignored_list)
			tables = self.get_all_child_doctypes()
			for docfield in docfields:
				if docfield["parent"] != self.doctype:
					no_of_docs = self.get_number_of_docs_linked_with_specified_company(
						docfield["parent"], docfield["fieldname"]
					)
					if no_of_docs > 0:
						# Initialize
						self.populate_doctypes_table(tables, docfield["parent"], docfield["fieldname"], 0)
			self.db_set("initialize_doctypes_table", 1)
		self.chain_callback(method="delete_company_transactions")

	def delete_company_transactions(self):
		if not self.delete_transactions:
			doctypes_to_be_ignored_list = self.get_doctypes_to_be_ignored_list()
			docfields = self.get_doctypes_with_company_field(doctypes_to_be_ignored_list)

			tables = self.get_all_child_doctypes()
			for docfield in self.doctypes:
				if docfield.doctype_name != self.doctype and not docfield.done:
					no_of_docs = self.get_number_of_docs_linked_with_specified_company(
						docfield.doctype_name, docfield.docfield_name
					)
					if no_of_docs > 0:
						reference_docs = frappe.get_all(
							docfield.doctype_name, filters={docfield.docfield_name: self.company}, limit=self.batch_size
						)
						reference_doc_names = [r.name for r in reference_docs]

						self.delete_version_log(docfield.doctype_name, reference_doc_names)
						self.delete_communications(docfield.doctype_name, reference_doc_names)
						self.delete_comments(docfield.doctype_name, reference_doc_names)
						self.unlink_attachments(docfield.doctype_name, reference_doc_names)

						self.delete_child_tables(docfield.doctype_name, reference_doc_names)
						self.delete_docs_linked_with_specified_company(docfield.doctype_name, docfield.docfield_name)

						naming_series = frappe.db.get_value("DocType", docfield.doctype_name, "autoname")
						# TODO: do this at the end of each doctype
						if naming_series:
							if "#" in naming_series:
								self.update_naming_series(naming_series, docfield.doctype_name)

						self.chain_callback(method="delete_company_transactions")
					else:
						frappe.db.set_value(docfield.doctype, docfield.name, "done", 1)
			self.db_set("delete_transactions", 1)

	def get_doctypes_to_be_ignored_list(self):
		singles = frappe.get_all("DocType", filters={"issingle": 1}, pluck="name")
		doctypes_to_be_ignored_list = singles
		for doctype in self.doctypes_to_be_ignored:
			doctypes_to_be_ignored_list.append(doctype.doctype_name)

		return doctypes_to_be_ignored_list

	def get_doctypes_with_company_field(self, doctypes_to_be_ignored_list):
		docfields = frappe.get_all(
			"DocField",
			filters={
				"fieldtype": "Link",
				"options": "Company",
				"parent": ["not in", doctypes_to_be_ignored_list],
			},
			fields=["parent", "fieldname"],
		)

		return docfields

	def get_all_child_doctypes(self):
		return frappe.get_all("DocType", filters={"istable": 1}, pluck="name")

	def get_number_of_docs_linked_with_specified_company(self, doctype, company_fieldname):
		return frappe.db.count(doctype, {company_fieldname: self.company})

	def populate_doctypes_table(self, tables, doctype, fieldname, no_of_docs):
		self.flags.ignore_validate_update_after_submit = True
		if doctype not in tables:
			self.append(
				"doctypes", {"doctype_name": doctype, "docfield_name": fieldname, "no_of_docs": no_of_docs}
			)
		self.save(ignore_permissions=True)

	def delete_child_tables(self, doctype, reference_doc_names):
		child_tables = frappe.get_all(
			"DocField", filters={"fieldtype": "Table", "parent": doctype}, pluck="options"
		)

		for table in child_tables:
			frappe.db.delete(table, {"parent": ["in", reference_doc_names]})

	def delete_docs_linked_with_specified_company(self, doctype, company_fieldname):
		frappe.db.delete(doctype, {company_fieldname: self.company})

	def update_naming_series(self, naming_series, doctype_name):
		if "." in naming_series:
			prefix, hashes = naming_series.rsplit(".", 1)
		else:
			prefix, hashes = naming_series.rsplit("{", 1)
		last = frappe.db.sql(
			"""select max(name) from `tab{0}`
						where name like %s""".format(
				doctype_name
			),
			prefix + "%",
		)
		if last and last[0][0]:
			last = cint(last[0][0].replace(prefix, ""))
		else:
			last = 0

		frappe.db.sql("""update `tabSeries` set current = %s where name=%s""", (last, prefix))

	def delete_version_log(self, doctype, docnames):
		versions = qb.DocType("Version")
		qb.from_(versions).delete().where(
			(versions.ref_doctype == doctype) & (versions.docname.isin(docnames))
		).run()

	def delete_communications(self, doctype, reference_doc_names):
		communications = frappe.get_all(
			"Communication",
			filters={"reference_doctype": doctype, "reference_name": ["in", reference_doc_names]},
		)
		communication_names = [c.name for c in communications]

		if not communication_names:
			return

		for batch in create_batch(communication_names, self.batch_size):
			frappe.delete_doc("Communication", batch, ignore_permissions=True)

	def delete_comments(self, doctype, reference_doc_names):
		comments = frappe.get_all(
			"Comment",
			filters={"reference_doctype": doctype, "reference_name": ["in", reference_doc_names]},
		)
		comment_names = [c.name for c in comments]

		if not comment_names:
			return

		for batch in create_batch(comment_names, self.batch_size):
			frappe.delete_doc("Comment", batch, ignore_permissions=True)

	def unlink_attachments(self, doctype, reference_doc_names):
		files = frappe.get_all(
			"File",
			filters={"attached_to_doctype": doctype, "attached_to_name": ["in", reference_doc_names]},
		)
		file_names = [c.name for c in files]

		if not file_names:
			return

		file = qb.DocType("File")

		for batch in create_batch(file_names, self.batch_size):
			qb.update(file).set(file.attached_to_doctype, None).set(file.attached_to_name, None).where(
				file.name.isin(batch)
			).run()


@frappe.whitelist()
def get_doctypes_to_be_ignored():
	doctypes_to_be_ignored = [
		"Account",
		"Cost Center",
		"Warehouse",
		"Budget",
		"Party Account",
		"Employee",
		"Sales Taxes and Charges Template",
		"Purchase Taxes and Charges Template",
		"POS Profile",
		"BOM",
		"Company",
		"Bank Account",
		"Item Tax Template",
		"Mode of Payment",
		"Mode of Payment Account",
		"Item Default",
		"Customer",
		"Supplier",
	]

	doctypes_to_be_ignored.extend(frappe.get_hooks("company_data_to_be_ignored") or [])

	return doctypes_to_be_ignored
