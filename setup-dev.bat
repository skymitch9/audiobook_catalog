@echo off
REM Development environment setup script for Windows

REM KI-3: the cp1252 console crash. Every wrapper in this repo that starts a
REM python process sets this, and this one starts two (pre-commit, pytest).
REM It is not only about emoji in our own strings: the LIBRARY'S OWN DATA is a
REM trigger — a dry run died on an author named 猫子 on 2026-09-02.
set PYTHONIOENCODING=utf-8

echo 🚀 Setting up audiobook catalog development environment...

REM Check if Python is available
python --version >nul 2>&1
if errorlevel 1 (
    echo ❌ Python is not installed. Please install Python 3.12 or later.
    exit /b 1
)

echo ✓ Python found
python --version

REM Install dependencies
echo.
echo 📦 Installing dependencies...
pip install -r requirements.txt

REM Install pre-commit hooks
echo.
echo 🔧 Setting up pre-commit hooks...
pre-commit install

REM Run pre-commit on all files to check setup
echo.
echo 🧪 Testing pre-commit setup...
pre-commit run --all-files

REM Run tests
REM WARNING: pytest, and only pytest. This used to call run_tests.py (stdlib
REM unittest discovery), which collected 135 of the 2,242 cases pytest sees and
REM reported green on the rest - a new developer got a false pass. Deleted
REM 2026-09-05. (Comment kept pure ASCII: this file is read by cp1252 consoles.)
echo.
echo 🧪 Running test suite...
python -m pytest tests/ -q

echo.
echo ✅ Development environment setup complete!
echo.
echo 📝 Next steps:
echo   1. Copy .env.example to .env and configure ROOT_DIR
echo   2. Run: python -m app.main
echo   3. Check site/index.html
echo.
echo 💡 Tips:
echo   - Pre-commit hooks will run automatically on git commit
echo   - Run 'pre-commit run --all-files' to check all files manually
echo   - Run 'python -m pytest tests/ -q' to run the Python tests
echo   - Run 'npx vitest run' to run the JS tests
echo   - See .github/SETUP.md for more details
