# Backend Hosting Requirements

**Feature:** Deploy audiobook catalog with Flask backend and React frontend

**Goal:** Host the full-stack application (Flask API + React frontend + Archive HTML) with the Flask backend serving all content. The home page (/) serves the archive static HTML catalog, while the React app is available at /catalog.

**Status:** Active - Flask backend required for API and serving both UIs

---

## 1. User Stories

### 1.1 As a developer, I want to deploy the Flask backend to a hosting service
**Acceptance Criteria:**
- Flask backend runs on a hosting service (free or paid tier)
- Backend serves API endpoints: `/api/books`, `/api/books/{id}`, `/api/books/search`
- Backend serves static files: covers, archive HTML
- Backend serves archive HTML catalog at root (/)
- Backend serves React app at /catalog route
- Service has 99%+ uptime
- Cold start time < 10 seconds acceptable for free tier

### 1.2 As a user, I want to see the archive HTML catalog at the home page
**Acceptance Criteria:**
- Visiting / shows the archive static HTML catalog
- Archive catalog loads in < 2 seconds
- All archive assets (CSS, images) load correctly
- Archive catalog is fully functional

### 1.3 As a user, I want to access the React app at /catalog
**Acceptance Criteria:**
- Visiting /catalog shows the React app
- React app loads in < 3 seconds
- React app can call backend API via relative URLs
- React Router works for all /catalog/* routes

### 1.4 As a developer, I want to use GitHub Actions for automated deployment
**Acceptance Criteria:**
- Push to main branch triggers deployment
- Deployment workflow builds React app and deploys to hosting service
- Deployment failures are reported clearly
- Rollback capability exists

### 1.5 As a developer, I want to minimize hosting costs
**Acceptance Criteria:**
- Total monthly cost < $5 (ideally $0)
- No credit card required for initial setup (preferred)
- Free tier sufficient for low-traffic personal project
- Clear upgrade path if traffic increases

### 1.6 As a user, I want the leaderboard to work with Firebase
**Acceptance Criteria:**
- Firebase Firestore continues to work for game leaderboard
- No backend database needed for audiobook data (uses CSV)
- Firebase costs remain in free tier (< 50K reads/day, < 20K writes/day)

---

## 2. Hosting Options Analysis

### 2.1 Backend Hosting Options (Flask API)

#### Option A: Render.com (RECOMMENDED)
**Pros:**
- Free tier: 750 hours/month (enough for 1 service)
- Automatic HTTPS
- GitHub integration
- Docker support
- No credit card required
- Spins down after 15 min inactivity (cold starts ~30s)

**Cons:**
- Cold starts on free tier
- 512 MB RAM limit
- Spins down after inactivity

**Cost:** $0/month (free tier)

#### Option B: Railway.app
**Pros:**
- $5 free credit/month
- Fast deployments
- Excellent DX
- No cold starts

**Cons:**
- Requires credit card
- Free credit may not cover full month with constant uptime
- $5/month after free credit

**Cost:** $0-5/month

#### Option C: Fly.io
**Pros:**
- Free tier: 3 shared VMs
- No cold starts
- Global edge network
- Docker-native

**Cons:**
- Requires credit card
- Complex pricing
- Free tier limits may be tight

**Cost:** $0/month (within free tier limits)

#### Option D: PythonAnywhere
**Pros:**
- Python-specific
- Free tier available
- No cold starts
- Simple setup

**Cons:**
- Limited to 100 seconds/day CPU time on free tier
- No custom domains on free tier
- Outdated interface

**Cost:** $0/month (free tier) or $5/month (paid)

### 2.2 Frontend Hosting Options (React)

#### Option A: Cloudflare Pages (RECOMMENDED)
**Pros:**
- Unlimited bandwidth
- Unlimited requests
- Global CDN
- Automatic HTTPS
- GitHub integration
- Custom domains free
- No cold starts

**Cons:**
- 500 builds/month limit (plenty for personal use)

**Cost:** $0/month

#### Option B: Vercel
**Pros:**
- Excellent DX
- Automatic deployments
- Preview deployments
- Edge functions

**Cons:**
- 100 GB bandwidth/month limit on free tier
- Commercial use restrictions

**Cost:** $0/month (free tier)

#### Option C: Netlify
**Pros:**
- 100 GB bandwidth/month
- Automatic deployments
- Form handling
- Edge functions

**Cons:**
- 300 build minutes/month
- Slower than Cloudflare

**Cost:** $0/month (free tier)

#### Option D: GitHub Pages (CURRENT)
**Pros:**
- Already using it
- Free
- Simple

**Cons:**
- Static only (no backend)
- No custom headers/redirects
- Slower than CDN options

**Cost:** $0/month

---

## 3. Recommended Architecture

### 3.1 Architecture Overview
```
┌─────────────────────────────────────────────────────────────┐
│                     GitHub Repository                        │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐     │
│  │   Frontend   │  │   Backend    │  │  Catalog     │     │
│  │   (React)    │  │   (Flask)    │  │  Generator   │     │
│  └──────────────┘  └──────────────┘  └──────────────┘     │
└─────────────────────────────────────────────────────────────┘
                            │
                    GitHub Actions
                            │
                            ▼
                    ┌──────────────┐
                    │   Render     │
                    │   (Flask)    │───────┐
                    │   Backend    │       │
                    └──────────────┘       │
                            │              │
                            │              ▼
                            │      ┌──────────────┐
                            │      │   Firebase   │
                            │      │  Firestore   │
                            │      │ (Leaderboard)│
                            │      └──────────────┘
                            │
                            ▼
                      ┌──────────┐
                      │   User   │
                      └──────────┘
                            │
                ┌───────────┴───────────┐
                │                       │
                ▼                       ▼
         / (Archive HTML)      /catalog (React App)
```

### 3.2 Component Responsibilities

**Flask Backend (Render):**
- Serve archive HTML catalog at / (home page)
- Serve React SPA at /catalog
- Serve `/api/books` endpoints for React app
- Serve cover images
- Read catalog.csv from mounted volume or included in deployment

**React App (/catalog):**
- Modern interactive catalog interface
- Call backend API at /api/*
- Call Firebase for leaderboard
- Client-side routing under /catalog/*

**Archive HTML (/):**
- Static HTML catalog (legacy/fallback)
- Works without JavaScript
- Served directly by Flask

**Firebase:**
- Store game leaderboard data only
- No audiobook data (too expensive)

---

## 4. Deployment Workflow

### 4.1 On Push to Main Branch

1. **Run Tests & Lint** (existing)
2. **Generate Catalog Data**
   - Run `python -m app.main` to generate catalog.csv and archive HTML
3. **Build React Frontend**
   - Build React app with `npm run build` (outputs to site/)
4. **Build and Deploy Backend**
   - Build Docker image with Flask + React build + Archive HTML
   - Push to Render
   - Render serves everything from one container

### 4.2 Environment Variables

**Backend (.env):**
```
FLASK_ENV=production
CATALOG_CSV_PATH=/app/site/catalog.csv
COVERS_DIR=/app/site/covers
ARCHIVE_DIR=/app/site/archive
REACT_BUILD_DIR=/app/site
```

**Frontend (.env for build):**
```
VITE_API_URL=/api
VITE_FIREBASE_API_KEY=...
VITE_FIREBASE_PROJECT_ID=audiobook-catalog
```

---

## 5. Cost Breakdown

### 5.1 Monthly Costs (Recommended Setup)

| Service | Tier | Cost | Usage |
|---------|------|------|-------|
| Render | Free | $0 | Backend + Frontend + Archive (with cold starts) |
| Firebase Firestore | Free | $0 | < 50K reads/day |
| GitHub Actions | Free | $0 | < 2000 min/month |
| **TOTAL** | | **$0/month** | |

### 5.2 Upgrade Path (If Needed)

If traffic grows beyond free tiers:

| Service | Paid Tier | Cost | Benefits |
|---------|-----------|------|----------|
| Render | Starter | $7/month | No cold starts, 512MB RAM |
| Firebase | Blaze | Pay-as-you-go | Only pay for usage |

**Estimated cost at 10K users/month:** ~$7/month

---

## 6. Data Storage Strategy

### 6.1 Audiobook Data (catalog.csv, covers)

**Option A: Store in Git (RECOMMENDED for small catalogs)**
- Commit catalog.csv and covers to repo
- Simple, no external storage needed
- Works well for < 100 books, < 50MB covers
- Free

**Option B: Cloudflare R2 (for large catalogs)**
- S3-compatible object storage
- 10 GB free storage
- No egress fees
- Backend fetches from R2
- Cost: $0/month (within free tier)

**Option C: Render Persistent Disk**
- Attach disk to Render service
- Upload via deployment
- Cost: $1/GB/month (not free)

### 6.2 Leaderboard Data (Firebase)

- Keep using Firebase Firestore
- Free tier: 50K reads, 20K writes, 1GB storage per day
- Perfect for leaderboard use case
- No changes needed

---

## 7. Migration Steps

### 7.1 Phase 1: Setup Hosting Services
1. Create Cloudflare Pages project
2. Create Render web service
3. Configure environment variables
4. Test manual deployments

### 7.2 Phase 2: Update Code
1. Update frontend API base URL to use environment variable
2. Update backend CORS to allow Cloudflare domain
3. Update vite.config.ts base URL
4. Test locally with Docker

### 7.3 Phase 3: Setup GitHub Actions
1. Add Cloudflare Pages deployment
2. Add Render deployment
3. Add secrets to GitHub
4. Test deployment workflow

### 7.4 Phase 4: DNS & Domain (Optional)
1. Point custom domain to Cloudflare Pages
2. Update CORS origins
3. Update Firebase authorized domains

---

## 8. Alternative: Serverless Architecture

### 8.1 Fully Serverless Option

**Frontend:** Cloudflare Pages ($0)
**API:** Cloudflare Workers ($0 for < 100K requests/day)
**Data:** Cloudflare R2 or GitHub ($0)
**Leaderboard:** Firebase ($0)

**Benefits:**
- No cold starts
- Infinite scale
- $0 cost for low traffic
- Global edge network

**Tradeoffs:**
- Need to rewrite Flask API as Cloudflare Workers (JavaScript/TypeScript)
- More complex development
- 10ms CPU time limit per request

---

## 9. Recommended Solution

**Single-backend deployment with Flask serving everything:**

1. **Backend:** Render Free Tier
   - Deploy Flask API via Docker
   - Serve archive HTML at /
   - Serve React app at /catalog
   - Serve API at /api/*
   - Accept cold starts (30s)
   - Free, simple setup

2. **Data:** Git-based
   - Commit catalog.csv, covers, and archive HTML to repo
   - Include in Docker image
   - Simple, no external storage
   - Works for small/medium catalogs

3. **Leaderboard:** Firebase (keep as-is)
   - Already working
   - Free tier sufficient
   - No changes needed

**Total Cost:** $0/month
**Setup Time:** 1-2 hours
**Maintenance:** Minimal

**Benefits:**
- Single deployment target (simpler)
- No CORS issues (same origin)
- Archive HTML as fallback
- React app for modern experience

---

## 10. Success Metrics

### 10.1 Performance
- Frontend load time < 3s
- API response time < 500ms (warm)
- API response time < 10s (cold start acceptable)

### 10.2 Reliability
- 99%+ uptime
- Successful deployments > 95%
- Zero data loss

### 10.3 Cost
- Monthly cost < $5
- Stay within free tiers
- No surprise charges

---

## 11. Future Enhancements

### 11.1 If Traffic Grows
- Upgrade Render to paid tier ($7/month) for no cold starts
- Add Redis caching layer
- Use Cloudflare R2 for covers

### 11.2 If Features Expand
- Add user authentication (Firebase Auth)
- Add book ratings/reviews (Firestore)
- Add recommendation engine
- Add admin panel

### 11.3 If Performance Matters
- Migrate to Cloudflare Workers (serverless)
- Add CDN caching
- Optimize images
- Add service worker for offline support
