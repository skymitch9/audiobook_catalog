import { Link, useLocation } from 'react-router-dom';
import ThemeToggle from './ThemeToggle';
import './Header.css';

/**
 * Header component - Navigation bar displayed on all pages
 * 
 * Provides consistent navigation across the application with:
 * - Application title/branding with emoji
 * - Links to Catalog, Statistics, and Game pages
 * - Theme toggle button
 * 
 * Requirements: 1.7, 2.8, 3.1, 6.1, 4.5
 */
function Header() {
  const location = useLocation();

  return (
    <header className="header">
      <div className="container">
        <div className="nav-wrapper">
          <div className="logo">
            <h1>📚 Audiobook Catalog</h1>
          </div>
          <nav className="nav">
            <Link 
              to="/" 
              className={`nav-link ${location.pathname === '/' ? 'active' : ''}`}
            >
              Catalog
            </Link>
            <Link 
              to="/statistics" 
              className={`nav-link ${location.pathname === '/statistics' ? 'active' : ''}`}
            >
              Statistics
            </Link>
            <Link 
              to="/game" 
              className={`nav-link ${location.pathname === '/game' ? 'active' : ''}`}
            >
              Guess Game
            </Link>
            <ThemeToggle />
          </nav>
        </div>
      </div>
    </header>
  );
}

export default Header;
