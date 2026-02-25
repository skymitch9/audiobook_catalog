import { createContext, useContext } from 'react';
import type { ReactNode } from 'react';
import { useLocalStorage } from '../hooks/useLocalStorage';
import type { ViewMode } from '../types/View';

interface ViewContextType {
  viewMode: ViewMode;
  setViewMode: (mode: ViewMode) => void;
}

const ViewContext = createContext<ViewContextType | undefined>(undefined);

interface ViewProviderProps {
  children: ReactNode;
}

export function ViewProvider({ children }: ViewProviderProps) {
  const [viewMode, setViewMode] = useLocalStorage<ViewMode>('viewMode', 'grid');

  const value: ViewContextType = {
    viewMode,
    setViewMode,
  };

  return (
    <ViewContext.Provider value={value}>
      {children}
    </ViewContext.Provider>
  );
}

export function useView(): ViewContextType {
  const context = useContext(ViewContext);
  if (context === undefined) {
    throw new Error('useView must be used within a ViewProvider');
  }
  return context;
}
