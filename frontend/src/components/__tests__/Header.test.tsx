import { render, screen } from '@testing-library/react';
import { BrowserRouter } from 'react-router-dom';
import { describe, it, expect } from 'vitest';
import Header from '../Header';

/**
 * Unit tests for Header component
 * 
 * Tests verify:
 * - Component renders without crashing
 * - Application title is displayed
 * - Navigation link to home is present
 * 
 * Requirements: 6.1, 4.5
 */

// Helper function to render Header with Router context
function renderHeader() {
  return render(
    <BrowserRouter>
      <Header />
    </BrowserRouter>
  );
}

describe('Header Component', () => {
  it('renders without crashing', () => {
    renderHeader();
    expect(screen.getByRole('banner')).toBeInTheDocument();
  });

  it('displays the application title', () => {
    renderHeader();
    expect(screen.getByRole('heading', { name: /audiobook catalog/i })).toBeInTheDocument();
  });

  it('displays a link to the home page', () => {
    renderHeader();
    const homeLinks = screen.getAllByRole('link', { name: /home/i });
    expect(homeLinks.length).toBeGreaterThan(0);
    expect(homeLinks[0]).toHaveAttribute('href', '/');
  });

  it('title is a clickable link to home page', () => {
    renderHeader();
    const titleLink = screen.getByRole('heading', { name: /audiobook catalog/i }).closest('a');
    expect(titleLink).toHaveAttribute('href', '/');
  });
});
