# GitHub Repository Setup Guide

## Branch Protection Rules

To enforce linting and tests before merging, set up branch protection:

### Steps:

1. Go to your repository on GitHub
2. Click **Settings** → **Branches**
3. Click **Add branch protection rule**
4. Configure:

```
Branch name pattern: main
```

**Required settings:**

✅ **Require a pull request before merging**
- Require approvals: 0 (or 1 if you want review)
- Dismiss stale pull request approvals when new commits are pushed

✅ **Require status checks to pass before merging**
- Require branches to be up to date before merging
- Status checks that are required:
  - `lint` (from lint.yml workflow)
  - `test` (from tests.yml workflow)

✅ **Require conversation resolution before merging** (optional)

✅ **Do not allow bypassing the above settings**

### Result:
- All PRs must pass linting and tests before merge
- Direct pushes to main are blocked (must use PRs)
- Ensures code quality is maintained

---

## Pre-commit Hooks Setup

For local enforcement before commits:

### Installation:

```bash
# Install pre-commit
pip install pre-commit

# Install the git hooks
cd audiobook_catalog
pre-commit install

# Test it works
pre-commit run --all-files
```

### What it does:

- **Black**: Auto-formats Python code
- **Flake8**: Checks for code quality issues
- **isort**: Sorts imports
- **Bandit**: Security checks
- **General checks**: Trailing whitespace, file endings, YAML/JSON validation

### Usage:

```bash
# Runs automatically on git commit
git commit -m "Your message"

# Run manually on all files
pre-commit run --all-files

# Run on specific files
pre-commit run --files app/main.py

# Skip hooks if needed (not recommended)
git commit --no-verify -m "Emergency fix"

# Update hooks to latest versions
pre-commit autoupdate
```

---

## Workflow Overview

### Local Development:
1. Make changes
2. Pre-commit hooks run automatically on `git commit`
3. Fix any issues flagged by hooks
4. Commit succeeds

### Pull Request:
1. Push branch to GitHub
2. Create Pull Request
3. GitHub Actions run:
   - Lint workflow (must pass)
   - Test workflow (must pass)
   - Build workflow (must pass)
4. If all pass → can merge
5. If any fail → must fix before merge

### Deployment:
1. Merge to main
2. Deploy workflow runs:
   - Runs tests (must pass)
   - Runs lint (must pass)
   - Builds site
   - Deploys to GitHub Pages
   - Sends Discord notification

---

## Bypassing Checks (Emergency Only)

### Local pre-commit:
```bash
git commit --no-verify -m "Emergency fix"
```

### GitHub Actions:
- Cannot bypass (by design)
- Admin can temporarily disable branch protection if absolutely necessary

---

## Recommended Workflow

### For new features:
```bash
# Create feature branch
git checkout -b feature/new-feature

# Make changes
# ... edit files ...

# Commit (pre-commit runs automatically)
git add .
git commit -m "Add new feature"

# Push and create PR
git push origin feature/new-feature
# Create PR on GitHub

# Wait for checks to pass
# Merge when green ✅
```

### For quick fixes:
```bash
# Create fix branch
git checkout -b fix/quick-fix

# Make changes
# ... edit files ...

# Run checks manually first
pre-commit run --all-files
python run_tests.py

# Commit and push
git add .
git commit -m "Fix issue"
git push origin fix/quick-fix

# Create PR and merge when green
```

---

## Troubleshooting

### Pre-commit hook fails:
```bash
# See what failed
pre-commit run --all-files

# Auto-fix formatting issues
black app tests
isort app tests

# Check remaining issues
flake8 app tests --max-line-length=127

# Commit again
git add .
git commit -m "Fix linting issues"
```

### GitHub Actions fail:
1. Check the Actions tab for error details
2. Fix issues locally
3. Push fixes
4. Checks will re-run automatically

### Can't merge PR:
- Ensure all status checks are green
- Resolve any merge conflicts
- Get required approvals (if configured)

---

## Configuration Files

- `.pre-commit-config.yaml` - Pre-commit hook configuration
- `pyproject.toml` - Tool configurations (black, isort, bandit)
- `.github/workflows/lint.yml` - Linting workflow
- `.github/workflows/tests.yml` - Testing workflow
- `.github/workflows/deploy.yml` - Deployment workflow

---

## Benefits

✅ **Code Quality**: Consistent formatting and style
✅ **Early Detection**: Catch issues before they reach main
✅ **Team Consistency**: Everyone follows same standards
✅ **Automated**: No manual checking needed
✅ **Safe Deployments**: Only tested code reaches production
