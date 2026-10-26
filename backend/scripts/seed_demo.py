"""Generate the demo data. Run from backend/:

    .venv\\Scripts\\python scripts\\seed_demo.py                    # writes seed/demo_payouts.csv
    .venv\\Scripts\\python scripts\\seed_demo.py --load-directory   # also loads 250 recipients into Supabase
    .venv\\Scripts\\python scripts\\seed_demo.py --sandbox-receivers a@personal.example.com,b@personal.example.com

Same as `python -m app.cli seed`.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(["seed", *sys.argv[1:]]))
