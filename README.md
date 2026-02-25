# GABI Audiobook Catalog

Modern audiobook catalog with integrated React frontend and Flask backend.

## Quick Start with Docker (Recommended)

### Production:
```bash
# Clone repository
git clone <your-repo>
cd audiobook_catalog

# Configure
cp .env.example .env
# Edit .env and set ROOT_DIR to your audiobook library path

# Build and run
docker compose up -d

# Access at http://localhost:5000
```

### Development with Hot Reload:
```bash
# Run development container
docker compose --profile dev up audiobook-catalog-dev

# Flask server: http://localhost:5000
# Vite dev server: http://localhost:3001
```

### Docker Commands:
```bash
# View logs
docker compose logs -f

# Stop containers
docker compose down

# Rebuild and restart
docker compose up -d --build

# Run tests
docker compose --profile test run --rm audiobook-catalog-test

# Run tests with coverage
docker compose --profile test run --rm audiobook-catalog-test python -m pytest tests/ --cov=app --cov-report=html

# Generate catalog
docker compose exec audiobook-catalog python -m app.main

# Access shell in container
docker compose exec audiobook-catalog sh
```

## Manual Setup (Alternative)

If you prefer not to use Docker:

```bash
# Install dependencies
pip install -r requirements.txt
cd frontend && npm install && cd ..

# Configure
cp .env.example .env
# Edit .env and set ROOT_DIR

# Build frontend
cd frontend && npm run build && cd ..

# Generate catalog
python -m app.main

# Run server
python -m app.web.server
```

## Web Application

The integrated Flask web server serves:
- **React Frontend** at `/` - Modern catalog interface with modals, search, and Book of the Day
- **API Endpoints** at `/api/*` - RESTful API for book data
- **Archive** at `/archive` - Original static HTML catalog (always available)

### Routes:
- `http://localhost:5000/` - React catalog app
- `http://localhost:5000/archive` - Static HTML archive
- `http://localhost:5000/api/books` - All books API
- `http://localhost:5000/api/books/{id}` - Single book API
- `http://localhost:5000/api/covers/{path}` - Cover images

## Publishing to GitHub Pages

```bash
# Generate catalog (in Docker or locally)
docker compose exec audiobook-catalog python -m app.main
# or
python -m app.main

# Commit and push
git add site
git commit -m "Update catalog"
git push
```

## Development

### Running Tests:
```bash
# Using Docker test service (recommended)
docker compose --profile test run --rm audiobook-catalog-test

# With coverage report
docker compose --profile test run --rm audiobook-catalog-test python -m pytest tests/ --cov=app --cov-report=html

# In running container
docker compose exec audiobook-catalog python -m pytest tests/

# Or locally (if not using Docker)
python -m pytest tests/
```

### Code Quality:
```bash
# Using Docker (recommended)
docker compose exec audiobook-catalog black app tests
docker compose exec audiobook-catalog flake8 app tests --max-line-length=127

# Or locally
black app tests
flake8 app tests --max-line-length=127
```

### Frontend Development:
```bash
# Use dev container for hot reload
docker compose --profile dev up audiobook-catalog-dev

# Or manually
cd frontend
npm run dev  # Development server
npm run build  # Production build
npm test  # Run tests
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
