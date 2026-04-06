# Billing Ingest: BigQuery -> cloud_costs

This loads real GCP billing export data into PostgreSQL `cloud_costs`, which is what the deployed cost agent reads.

## Prerequisites

- BigQuery Billing Export is enabled in GCP Billing.
- You know:
  - billing export project id
  - dataset
  - table name
- `DATABASE_URL` points to your PostgreSQL (local or tunneled).
- ADC auth is configured:

```powershell
gcloud auth application-default login
```

## Install deps

```powershell
.\.venv\Scripts\pip install -r requirements-adk.txt
```

## Run ingest (PowerShell)

```powershell
$env:DATABASE_URL = "postgresql://postgres:postgres@127.0.0.1:5435/postgres"
.\scripts\ingest-gcp-billing.ps1 `
  -BqProject gls-training-486405 `
  -BqDataset <BILLING_DATASET> `
  -BqTable <BILLING_TABLE> `
  -StartDate 2026-03-01 `
  -EndDate 2026-03-26
```

## What it does

- Queries BigQuery billing export by `DATE(usage_start_time)`, `service.description`, and environment label (`environment`/`env` if present).
- Normalizes environment to `prod` or `dev` to satisfy table constraint.
- Deletes existing `cloud_costs` rows in the requested date range.
- Inserts fresh aggregated rows (idempotent for the date range).

## Verify

Ask the cost agent for:
- "Total cost for prod between 2026-03-01 and 2026-03-26"
- "Show BigQuery costs"

