# mainApp/inventory_urls.py
"""
Include from mainApp/urls.py (replace section 23 "INVENTORY & LIBRARY"):

    path("inventory/", include("mainApp.inventory_urls")),

No namespace, so every existing {% url 'inv_...' %} keeps working.
"""
from django.urls import path

from mainApp.views import (
    inventory_ajax_views as ajax,
    inventory_procurement_views as proc,
    inventory_report_views as reports,
    inventory_stock_views as stock,
    inventory_stocktake_views as st,
    inventory_views as v,
)
from mainApp.views.inventory_labels_views import inv_labels_print, inv_labels_select, inv_qr_png

urlpatterns = [
    path("", v.inventory_dashboard, name="inv_dashboard"),
    path("settings/", v.inventory_settings, name="inv_settings"),
    path("settings/class-rules/", v.class_policy_list, name="inv_class_policies"),
    path("settings/class-rules/new/", v.class_policy_edit, name="inv_class_policy_create"),
    path("settings/class-rules/<int:pk>/", v.class_policy_edit, name="inv_class_policy_edit"),
    path("settings/class-rules/<int:pk>/delete/", v.class_policy_delete, name="inv_class_policy_delete"),

    # Setup
    path("categories/", v.categories_list, name="inv_categories"),
    path("categories/new/", v.category_edit, name="inv_category_create"),
    path("categories/<int:pk>/edit/", v.category_edit, name="inv_category_edit"),
    path("locations/", v.locations_list, name="inv_locations"),
    path("locations/new/", v.location_edit, name="inv_location_create"),
    path("locations/<int:pk>/edit/", v.location_edit, name="inv_location_edit"),

    # Items
    path("items/", v.items_list, name="inv_items"),
    path("items/new/", v.item_edit, name="inv_item_create"),
    path("items/<int:pk>/", v.item_detail, name="inv_item_detail"),
    path("items/<int:pk>/edit/", v.item_edit, name="inv_item_edit"),

    # Tagged units
    path("units/", v.units_list, name="inv_units"),
    path("units/new/", v.unit_create, name="inv_unit_create"),
    path("units/bulk-add/", v.unit_bulk_add, name="inv_unit_bulk_add"),
    path("units/<int:pk>/", v.unit_detail, name="inv_unit_detail"),
    path("units/<int:pk>/edit/", v.unit_edit, name="inv_unit_edit"),
    path("units/<int:pk>/<slug:action>/", v.unit_action, name="inv_unit_action"),

    # Desk, audit, labels
    path("scan/", v.issue_return_scan, name="inv_scan"),
    path("transactions/", v.transactions_list, name="inv_transactions"),
    path("qr/<str:tag_code>.png", inv_qr_png, name="inv_qr_png"),
    path("labels/select/", inv_labels_select, name="inv_labels_select"),
    path("labels/print/", inv_labels_print, name="inv_labels_print"),
    path("labels/item/<int:item_id>/", v.inv_labels_item, name="inv_labels_item"),

    # Term textbooks & clearance
    path("textbooks/", v.batch_list, name="inv_batches"),
    path("textbooks/issue/", v.class_issue_create, name="inv_class_issue"),
    path("textbooks/<int:pk>/", v.batch_detail, name="inv_batch_detail"),
    path("clearance/", v.clearance, name="inv_clearance"),

    # Counted stock
    path("stock/", stock.stock_list, name="inv_stock"),
    path("stock/movements/", stock.stock_movements, name="inv_stock_movements"),
    path("stock/<slug:op>/", stock.stock_operation, name="inv_stock_op"),

    # Procurement
    path("suppliers/", proc.supplier_list, name="inv_suppliers"),
    path("suppliers/new/", proc.supplier_edit, name="inv_supplier_create"),
    path("suppliers/<int:pk>/edit/", proc.supplier_edit, name="inv_supplier_edit"),
    path("purchase-orders/", proc.po_list, name="inv_pos"),
    path("purchase-orders/new/", proc.po_edit, name="inv_po_create"),
    path("purchase-orders/<int:pk>/", proc.po_detail, name="inv_po_detail"),
    path("purchase-orders/<int:pk>/edit/", proc.po_edit, name="inv_po_edit"),
    path("purchase-orders/<int:pk>/approve/", proc.po_approve, name="inv_po_approve"),
    path("purchase-orders/<int:pk>/cancel/", proc.po_cancel, name="inv_po_cancel"),
    path("purchase-orders/<int:pk>/close/", proc.po_close, name="inv_po_close"),
    path("goods-received/", proc.grn_list, name="inv_grns"),
    path("goods-received/new/", proc.grn_edit, name="inv_grn_create"),
    path("goods-received/<int:pk>/", proc.grn_detail, name="inv_grn_detail"),
    path("goods-received/<int:pk>/edit/", proc.grn_edit, name="inv_grn_edit"),
    path("goods-received/<int:pk>/post/", proc.grn_post, name="inv_grn_post"),

    # Stock take
    path("stock-takes/", st.stocktake_list, name="inv_stocktakes"),
    path("stock-takes/new/", st.stocktake_start, name="inv_stocktake_start"),
    path("stock-takes/<int:pk>/", st.stocktake_detail, name="inv_stocktake_detail"),
    path("stock-takes/<int:pk>/scan/", st.stocktake_scan, name="inv_stocktake_scan"),
    path("stock-takes/<int:pk>/count/", st.stocktake_count, name="inv_stocktake_count"),
    path("stock-takes/<int:pk>/complete/", st.stocktake_complete, name="inv_stocktake_complete"),
    path("stock-takes/<int:pk>/cancel/", st.stocktake_cancel, name="inv_stocktake_cancel"),

    # Reports
    path("reports/", reports.reports_index, name="inv_reports"),
    path("reports/<slug:key>/", reports.report_view, name="inv_report"),

    # AJAX (scan desk)
    path("ajax/unit-lookup/", ajax.inv_ajax_unit_lookup, name="inv_ajax_unit_lookup"),
    path("ajax/student-lookup/", ajax.inv_ajax_student_lookup, name="inv_ajax_student_lookup"),
    path("ajax/issue/", ajax.inv_ajax_issue, name="inv_ajax_issue"),
    path("ajax/return/", ajax.inv_ajax_return, name="inv_ajax_return"),
    path("ajax/book-search/", ajax.inv_ajax_book_search, name="inv_ajax_book_search"),
    path("ajax/borrower-search/", ajax.inv_ajax_borrower_search, name="inv_ajax_borrower_search"),
    path("ajax/return-search/", ajax.inv_ajax_return_search, name="inv_ajax_return_search"),
]
