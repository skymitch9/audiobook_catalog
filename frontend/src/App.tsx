import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import { ThemeProvider } from './contexts/ThemeContext';
import { ViewProvider } from './contexts/ViewContext';
import Header from './components/Header';
import CatalogPage from './pages/CatalogPage';
import StatisticsPage from './pages/StatisticsPage';
import GamePage from './pages/GamePage';
import './App.css';

/**
 * App Component
 * 
 * Root component that sets up routing and global layout.
 * 
 * Features:
 * - React Router configuration with BrowserRouter
 * - Theme management with ThemeProvider
 * - View mode management with ViewProvider
 * - Persistent Header component on all pages
 * - Route definitions for Catalog, Detail, Statistics, and Game pages
 * - Catch-all route redirecting to catalog
 * 
 * Requirements: 1.7, 2.8, 3.1, 3.6, 4.1, 4.2, 4.3, 4.5, 6.1, 6.6
 * 
 * @returns {JSX.Element} Rendered App component
 */
function App() {
  return (
    <ThemeProvider>
      <ViewProvider>
        <BrowserRouter>
          <div className="app">
            {/* Header component displayed on all pages */}
            <Header />
            
            {/* Main content area with routes */}
            <main className="app-main">
              <Routes>
                {/* Catalog page at root path */}
                <Route path="/" element={<CatalogPage />} />
                
                {/* Statistics page route */}
                <Route path="/statistics" element={<StatisticsPage />} />
                
                {/* Game page route */}
                <Route path="/game" element={<GamePage />} />
                
                {/* Catch-all route redirects to catalog */}
                <Route path="*" element={<Navigate to="/" replace />} />
              </Routes>
            </main>
          </div>
        </BrowserRouter>
      </ViewProvider>
    </ThemeProvider>
  );
}

export default App;
