import { createContext, useContext, useEffect } from 'react';
import type { ReactNode } from 'react';
import { useLocalStorage } from '../hooks/useLocalStorage';
import type { ThemeMode } from '../types/Theme';

/**
 * Theme context type definition
 */
interface ThemeContextType {
  theme: ThemeMode;
  toggleTheme: () => void;
}

/**
 * Theme context for managing application-wide theme state
 */
const ThemeContext = createContext<ThemeContextType | undefined>(undefined);

/**
 * Props for ThemeProvider component
 */
interface ThemeProviderProps {
  children: ReactNode;
}

/**
 * ThemeProvider component - Manages theme state and persistence
 * 
 * Features:
 * - Persists theme preference to localStorage
 * - Applies theme class to document root element
 * - Defaults to dark theme
 * - Provides theme state and toggle function to children
 * 
 * Requirements: 3.1, 3.2, 3.3, 3.4, 3.6, 3.7
 */
export function ThemeProvider({ children }: ThemeProviderProps) {
  // Use localStorage hook with default dark theme (Requirement 3.7)
  const [theme, setTheme] = useLocalStorage<ThemeMode>('theme', 'dark');

  // Apply theme class to document root element (Requirement 3.6)
  useEffect(() => {
    document.documentElement.className = theme;
  }, [theme]);

  // Toggle function to switch between light and dark themes (Requirement 3.2)
  const toggleTheme = () => {
    setTheme(theme === 'light' ? 'dark' : 'light');
  };

  const value: ThemeContextType = {
    theme,
    toggleTheme,
  };

  return (
    <ThemeContext.Provider value={value}>
      {children}
    </ThemeContext.Provider>
  );
}

/**
 * Custom hook to use the theme context
 * 
 * @throws Error if used outside of ThemeProvider
 * @returns Theme context value with theme state and toggle function
 */
export function useTheme(): ThemeContextType {
  const context = useContext(ThemeContext);
  if (context === undefined) {
    throw new Error('useTheme must be used within a ThemeProvider');
  }
  return context;
}
