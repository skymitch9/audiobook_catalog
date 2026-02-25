# Audiobook React Catalog

A React-based audiobook catalog application that provides a web interface for browsing, searching, and viewing audiobook information. Built with React 18, TypeScript, Vite, and integrates with a Flask backend API.

## Features

- Browse audiobook catalog with responsive grid layout
- Search audiobooks by text query with debounced input
- View detailed information for individual audiobooks
- Client-side routing with React Router
- Type-safe data models with TypeScript
- Docker support for consistent deployment

## Prerequisites

- Node.js 20 or higher
- npm or yarn
- Docker and Docker Compose (for containerized deployment)
- Flask backend API running on port 5001 (or use Docker Compose)

## Quick Start

### Local Development

1. Install dependencies:
```bash
npm install
```

2. Start the development server:
```bash
npm run dev
```

3. Open http://localhost:3001 in your browser

### Docker Deployment

#### Production Mode

Build and run with Docker Compose:

```bash
docker-compose up -d
```

Access the application at http://localhost:3001

#### Development Mode (with hot reload)

Run the development container:

```bash
docker-compose --profile dev up -d
```

For detailed Docker instructions, see [docs/DOCKER.md](./docs/DOCKER.md)

## Available Scripts

- `npm run dev` - Start development server with hot reload
- `npm run build` - Build production bundle
- `npm run preview` - Preview production build locally
- `npm run test` - Run all tests
- `npm run test:watch` - Run tests in watch mode
- `npm run test:ui` - Run tests with UI
- `npm run lint` - Lint code with ESLint

## Project Structure

```
audiobook-react/
├── src/
│   ├── components/      # Reusable UI components
│   ├── pages/          # Page components
│   ├── services/       # API service layer
│   ├── types/          # TypeScript type definitions
│   └── __tests__/      # Test files
├── public/             # Static assets
├── Dockerfile          # Production Docker image
├── Dockerfile.dev      # Development Docker image
├── docker-compose.yml  # Multi-container orchestration
├── nginx.conf         # Nginx configuration for production
└── vite.config.ts     # Vite configuration

```

## API Integration

The application communicates with a Flask backend API at `http://localhost:5001/api` with the following endpoints:

- `GET /api/books` - Fetch all audiobooks
- `GET /api/books/search?q={query}` - Search audiobooks
- `GET /api/books/{id}` - Fetch single audiobook by ID

## Technology Stack

- **React 18** - UI framework with functional components and hooks
- **TypeScript** - Type safety and better developer experience
- **Vite** - Fast build tool with HMR
- **React Router v6** - Client-side routing
- **Axios** - HTTP client for API communication
- **Vitest** - Unit testing framework
- **fast-check** - Property-based testing
- **Docker** - Containerization and deployment

## Testing

The project includes both unit tests and property-based tests:

```bash
# Run all tests
npm test

# Run tests in watch mode
npm run test:watch

# Run tests with UI
npm run test:ui
```

## Docker Configuration

See [docs/DOCKER.md](./docs/DOCKER.md) for comprehensive Docker setup and usage instructions.

## Documentation

Additional documentation is available in the [docs](./docs/) folder:

- [Docker Setup](./docs/DOCKER.md) - Docker deployment guide
- [Project Setup](./docs/PROJECT_SETUP.md) - Initial setup documentation
- [Manual Test Report](./docs/MANUAL_TEST_REPORT.md) - Manual testing results
- [Final Checkpoint](./docs/FINAL_CHECKPOINT_REPORT.md) - Complete validation report

---

## Original Vite Template Information

This template provides a minimal setup to get React working in Vite with HMR and some ESLint rules.

Currently, two official plugins are available:

- [@vitejs/plugin-react](https://github.com/vitejs/vite-plugin-react/blob/main/packages/plugin-react) uses [Babel](https://babeljs.io/) (or [oxc](https://oxc.rs) when used in [rolldown-vite](https://vite.dev/guide/rolldown)) for Fast Refresh
- [@vitejs/plugin-react-swc](https://github.com/vitejs/vite-plugin-react/blob/main/packages/plugin-react-swc) uses [SWC](https://swc.rs/) for Fast Refresh

## React Compiler

The React Compiler is not enabled on this template because of its impact on dev & build performances. To add it, see [this documentation](https://react.dev/learn/react-compiler/installation).

## Expanding the ESLint configuration

If you are developing a production application, we recommend updating the configuration to enable type-aware lint rules:

```js
export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      // Other configs...

      // Remove tseslint.configs.recommended and replace with this
      tseslint.configs.recommendedTypeChecked,
      // Alternatively, use this for stricter rules
      tseslint.configs.strictTypeChecked,
      // Optionally, add this for stylistic rules
      tseslint.configs.stylisticTypeChecked,

      // Other configs...
    ],
    languageOptions: {
      parserOptions: {
        project: ['./tsconfig.node.json', './tsconfig.app.json'],
        tsconfigRootDir: import.meta.dirname,
      },
      // other options...
    },
  },
])
```

You can also install [eslint-plugin-react-x](https://github.com/Rel1cx/eslint-react/tree/main/packages/plugins/eslint-plugin-react-x) and [eslint-plugin-react-dom](https://github.com/Rel1cx/eslint-react/tree/main/packages/plugins/eslint-plugin-react-dom) for React-specific lint rules:

```js
// eslint.config.js
import reactX from 'eslint-plugin-react-x'
import reactDom from 'eslint-plugin-react-dom'

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      // Other configs...
      // Enable lint rules for React
      reactX.configs['recommended-typescript'],
      // Enable lint rules for React DOM
      reactDom.configs.recommended,
    ],
    languageOptions: {
      parserOptions: {
        project: ['./tsconfig.node.json', './tsconfig.app.json'],
        tsconfigRootDir: import.meta.dirname,
      },
      // other options...
    },
  },
])
```
