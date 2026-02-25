import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useUrlState } from '../../../hooks/useUrlState';

describe('useUrlState', () => {
  // Store original hash to restore after tests
  let originalHash: string;

  beforeEach(() => {
    originalHash = window.location.hash;
    window.location.hash = '';
  });

  afterEach(() => {
    window.location.hash = originalHash;
  });

  it('should initialize with initial value when hash is empty', () => {
    const { result } = renderHook(() => useUrlState('test', 'default'));
    expect(result.current[0]).toBe('default');
  });

  it('should read string value from URL hash on mount', () => {
    window.location.hash = '#test=hello';
    const { result } = renderHook(() => useUrlState('test', ''));
    expect(result.current[0]).toBe('hello');
  });

  it('should read number value from URL hash on mount', () => {
    window.location.hash = '#page=5';
    const { result } = renderHook(() => useUrlState('page', 1));
    expect(result.current[0]).toBe(5);
  });

  it('should update URL hash when value changes', () => {
    const { result } = renderHook(() => useUrlState('search', ''));
    
    act(() => {
      result.current[1]('fantasy');
    });

    expect(window.location.hash).toBe('#search=fantasy');
    expect(result.current[0]).toBe('fantasy');
  });

  it('should handle multiple keys in URL hash', () => {
    window.location.hash = '#search=test&page=2';
    
    const { result: searchResult } = renderHook(() => useUrlState('search', ''));
    const { result: pageResult } = renderHook(() => useUrlState('page', 1));

    expect(searchResult.current[0]).toBe('test');
    expect(pageResult.current[0]).toBe(2);
  });

  it('should preserve other keys when updating one key', () => {
    window.location.hash = '#search=test&page=2';
    
    const { result } = renderHook(() => useUrlState('search', ''));
    
    act(() => {
      result.current[1]('fantasy');
    });

    expect(window.location.hash).toContain('search=fantasy');
    expect(window.location.hash).toContain('page=2');
  });

  it('should handle URL encoding for special characters', () => {
    const { result } = renderHook(() => useUrlState('search', ''));
    
    act(() => {
      result.current[1]('test & value');
    });

    // URLSearchParams encodes spaces as + and special chars appropriately
    expect(window.location.hash).toContain('search=');
    expect(result.current[0]).toBe('test & value');
  });

  it('should remove parameter from hash when set to initial value', () => {
    window.location.hash = '#search=test&page=2';
    
    const { result } = renderHook(() => useUrlState('search', ''));
    
    act(() => {
      result.current[1]('');
    });

    expect(window.location.hash).toBe('#page=2');
  });

  it('should remove parameter from hash when set to empty string', () => {
    window.location.hash = '#search=test';
    
    const { result } = renderHook(() => useUrlState('search', 'default'));
    
    act(() => {
      result.current[1]('');
    });

    expect(window.location.hash).toBe('');
  });

  it('should handle boolean values', () => {
    const { result } = renderHook(() => useUrlState('active', false));
    
    act(() => {
      result.current[1](true);
    });

    expect(window.location.hash).toBe('#active=true');
    expect(result.current[0]).toBe(true);
  });

  it('should respond to hashchange events', () => {
    const { result } = renderHook(() => useUrlState('page', 1));
    
    act(() => {
      window.location.hash = '#page=3';
      window.dispatchEvent(new HashChangeEvent('hashchange'));
    });

    expect(result.current[0]).toBe(3);
  });

  it('should handle invalid number values gracefully', () => {
    window.location.hash = '#page=invalid';
    const { result } = renderHook(() => useUrlState('page', 1));
    expect(result.current[0]).toBe(1); // Should fall back to initial value
  });

  it('should handle missing key in hash', () => {
    window.location.hash = '#other=value';
    const { result } = renderHook(() => useUrlState('search', 'default'));
    expect(result.current[0]).toBe('default');
  });
});
