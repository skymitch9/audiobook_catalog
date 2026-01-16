@echo off
REM Development environment setup script for Windows

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
echo.
echo 🧪 Running test suite...
python run_tests.py

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
echo   - Run 'python run_tests.py' to run tests
echo   - See .github/SETUP.md for more details
