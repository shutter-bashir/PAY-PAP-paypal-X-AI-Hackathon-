"""Run from backend/: `python check_env.py` — lists any keys still empty in .env (values are never printed)."""
from app.config import settings

missing = settings.missing_required()
if missing:
    print("Missing keys in backend/.env:")
    for key in missing:
        print(f"  - {key}")
    raise SystemExit(1)
print("All required keys are set.")
