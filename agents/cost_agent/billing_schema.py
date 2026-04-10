from __future__ import annotations

import os


def schema_mode() -> str:
    mode = os.environ.get("BILLING_BQ_SCHEMA_MODE", "raw_export").strip().lower()
    if mode in {"clean", "clean_view"}:
        return "clean_view"
    return "raw_export"


def is_clean_view_mode() -> bool:
    return schema_mode() == "clean_view"


def service_name_expr() -> str:
    return "service_name" if is_clean_view_mode() else "service.description"


def project_id_expr() -> str:
    return "project_id" if is_clean_view_mode() else "project.id"


def region_expr() -> str:
    return "region" if is_clean_view_mode() else "location.region"


def project_labels_expr() -> str:
    return "project_labels" if is_clean_view_mode() else "project.labels"


def llm_schema_description() -> str:
    if is_clean_view_mode():
        return """
Allowed table (only source): `{table_ref}`

Clean billing view columns:
- usage_start_time TIMESTAMP, usage_end_time TIMESTAMP, invoice_month STRING
- cost FLOAT64, cost_at_list FLOAT64, currency STRING, cost_type STRING
- service_name STRING, sku_description STRING
- project_id STRING, project_name STRING
- region STRING, country STRING
- usage_amount FLOAT64, usage_unit STRING
- usage_amount_in_pricing_units FLOAT64, pricing_unit STRING
- credits ARRAY<STRUCT<...>>
- resource_labels ARRAY<STRUCT<key STRING, value STRING>>
- project_labels ARRAY<STRUCT<key STRING, value STRING>>
"""
    return """
Allowed table (only source): `{table_ref}`

Standard resource-level export (nested fields):
- usage_start_time TIMESTAMP — required in WHERE as DATE(usage_start_time) BETWEEN ...
- cost FLOAT64, currency STRING (this dataset uses **INR**)
- service.id, service.description
- sku.id, sku.description
- project.id, project.name, project.labels (ARRAY)
- location.region, location.zone, location.country
- cost_type STRING
- credits ARRAY<STRUCT<...>> — UNNEST(credits) for credit lines (amount, name, etc.)
"""
