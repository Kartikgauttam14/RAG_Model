# Testing

Run backend checks from the repository root:

```powershell
& .\.venv\Scripts\ruff.exe check backend/app backend/tests scripts
Push-Location backend; & ..\.venv\Scripts\mypy.exe app; Pop-Location
Push-Location backend; & ..\.venv\Scripts\pytest.exe -q; Pop-Location
```

The current suite covers structure-aware chunk provenance, XLSX duplicate header handling, upload validation, SSRF checks, prompt injection classification, password handling, insufficient-evidence refusal, and fake-citation rejection. Integration and live-provider tests must use separate configured infrastructure and must not be reported as passing until executed there.

Run frontend validation with `npm run build` and `npm test` from `frontend`. Run golden-data contract validation with `python scripts/evaluate.py`.
