# GABI Audiobook Catalog (GitHub Pages)

## Setup
1. `cp .env.example .env` and edit `ROOT_DIR`.
2. `pip install -r requirements.txt`

## Generate & publish
```bash
python -m app.main
git add site
git commit -m "Update catalog"
git push
