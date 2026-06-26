# GABI Audiobook Catalog (GitHub Pages)

<!-- Testing Discord webhook integration -->

## Quick Setup

### For Development:
```bash
# Clone and setup
git clone <your-repo>
cd audiobook_catalog

# Run setup script
bash setup-dev.sh  # Linux/Mac
# or
setup-dev.bat      # Windows

# Configure
cp .env.example .env
# Edit .env and set ROOT_DIR to your audiobook library path

# Generate catalog
python -m app.main
```

### For Production:
```bash
# Just configure and run
cp .env.example .env
pip install -r requirements.txt
python -m app.main
```

## Setup
1. `cp .env.example .env` and edit `ROOT_DIR`.
2. `pip install -r requirements.txt`

## Generate & publish
```bash
python -m app.main
git add site
git commit -m "Update catalog"
git push
```

## Optional Hardcover enrichment

The catalog can enrich local audiobook tags with Hardcover.app metadata during generation. This is build-time only, so your API token is never published to GitHub Pages.

1. Copy `.env.example` to `.env`.
2. Set `HARDCOVER_ENABLED=true`.
3. Add `HARDCOVER_TOKEN=<your token>`.
4. Run `python -m app.main`.

The enrichment layer uses your local file tags as the source of truth, then adds optional `hardcover_*` fields such as Hardcover book/edition IDs, URL, rating, rating count, audiobook duration, and match confidence. It caches lookups in `.cache/hardcover.json` so normal rebuilds do not repeatedly call the API.

Useful settings:

```env
HARDCOVER_ENABLED=true
HARDCOVER_TOKEN=
HARDCOVER_CACHE=.cache/hardcover.json
HARDCOVER_MIN_CONFIDENCE=0.86
HARDCOVER_MAX_RESULTS=8
```

## Development

### Pre-commit Hooks
Automatically format and check code before commits:
```bash
pip install pre-commit
pre-commit install
```

### Run Tests
```bash
python run_tests.py
```

### Code Quality
```bash
# Format code
black app tests

# Check linting
flake8 app tests --max-line-length=127

# Run all pre-commit checks
pre-commit run --all-files
```

## Features

- 📚 Automatic metadata extraction from audiobook files
- 🎨 Beautiful, responsive web interface
- 🔍 Search and filter capabilities
- 📊 Series completion tracker
- 🔔 Discord notifications on updates
- 🌙 Dark mode support
- 📱 Mobile-friendly design

See [FEATURES.md](FEATURES.md) for detailed feature documentation.

## Documentation

- [Setup Guide](.github/SETUP.md) - Branch protection, pre-commit hooks, workflows
- [Features](FEATURES.md) - New features documentation
- [Scripts](scripts/README.md) - Utility scripts guide
