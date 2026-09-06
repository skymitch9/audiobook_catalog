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

## Development

### Pre-commit Hooks
Automatically format and check code before commits:
```bash
pip install pre-commit
pre-commit install
```

### Run Tests
```bash
python -m pytest tests/ -q   # Python suite — the one CI runs
npx vitest run               # JS suite (site/__tests__)
```
⚠️ `pytest` is the **only** Python runner. A `run_tests.py` (stdlib unittest
discovery) used to sit here; it collected 135 of the 2,242 cases `pytest` sees
and reported green on the rest, so it was deleted 2026-09-05.
On Windows set `PYTHONIOENCODING=utf-8` first (KI-3, the cp1252 console crash).

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
