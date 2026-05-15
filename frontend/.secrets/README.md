# Local credentials (not committed)

Place your Speech-to-Text (or other) **service account JSON** here as `speech-sa.json`.

- Download from [Google Cloud Console](https://console.cloud.google.com/iam-admin/serviceaccounts) (JSON key).
- **Never** commit `*.json` in this folder; `.gitignore` blocks them.
- `frontend/.env` should use `GOOGLE_APPLICATION_CREDENTIALS=.secrets/speech-sa.json` (path relative to `frontend/`).
