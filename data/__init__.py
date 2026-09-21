"""On-disk layout of the repository.

Every script and module resolves paths through these constants so that the layout is
defined in exactly one place. Tests override them by passing explicit paths to the
functions they exercise rather than by monkeypatching this module.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

DATA_DIR = REPO_ROOT / "data"
# Dated pulls, never hand-edited. Gitignored except the manifest.json files, which the refresh
# workflow commits together with the data change they document.
RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"  # tidy CSVs derived from raw pulls; committed
LAST_REFRESH_DIFF = DATA_DIR / "last_refresh_diff.md"

MODELS_DIR = REPO_ROOT / "models"  # generated .xlsx per company; committed build artifact

SITE_DIR = REPO_ROOT / "site"
SITE_CONTENT_DIR = SITE_DIR / "content"  # writeups as markdown
SITE_DATA_DIR = SITE_DIR / "data"  # JSON the dashboard reads
SITE_BUILD_DIR = SITE_DIR / "build"  # generated site, deployed to GitHub Pages
SITE_TEMPLATES_DIR = SITE_DIR / "templates"
SITE_STATIC_DIR = SITE_DIR / "static"

# One CSV per company; every value has a source and a status column that Philbert owns.
ASSUMPTIONS_DIR = REPO_ROOT / "assumptions"

NOTES_DIR = REPO_ROOT / "notes"
CALLS_MD = REPO_ROOT / "calls.md"
