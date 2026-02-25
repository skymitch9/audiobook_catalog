import { describe, it, expect, beforeEach, vi } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useLocalStorage } from '../../../hooks/useLocalStorage';

describe('useLocalStorage', () => {
  beforeEach(() => {
    // Clear localStorage before each test
    localStorage.clear();
    // Clear all mocks
    vi.clearAllMocks();
  });

  it('should return initial value when localStorage is empty', () => {
    const { result } = renderHook(() => useLocalStorage('test-key', 'initial'));
    
    expect(result.current[0]).toBe('initial');
  });

  it('should return stored value from localStorage', () => {
    localStorage.setItem('test-key', JSON.stringify('stored-value'));
    
    const { result } = renderHook(() => useLocalStorage('test-key', 'initial'));
    
    expect(result.current[0]).toBe('stored-value');
  });

  it('should update localStorage when value changes', () => {
    const { result } = renderHook(() => useLocalStorage('test-key', 'initial'));
    
    act(() => {
      result.current[1]('new-value');
    });
    
    expect(result.current[0]).toBe('new-value');
    expect(localStorage.getItem('test-key')).toBe(JSON.stringify('new-value'));
  });

  it('should handle complex objects', () => {
    const complexObject = { theme: 'dark', fontSize: 16, enabled: true };
    
    const { result } = renderHook(() => useLocalStorage('test-key', complexObject));
    
    const newObject = { theme: 'light', fontSize: 18, enabled: false };
    act(() => {
      result.current[1](newObject);
    });
    
    expect(result.current[0]).toEqual(newObject);
    expect(JSON.parse(localStorage.getItem('test-key')!)).toEqual(newObject);
  });

  it('should handle arrays', () => {
    const initialArray = [1, 2, 3];
    
    const { result } = renderHook(() => useLocalStorage('test-key', initialArray));
    
    const newArray = [4, 5, 6];
    act(() => {
      result.current[1](newArray);
    });
    
    expect(result.current[0]).toEqual(newArray);
    expect(JSON.parse(localStorage.getItem('test-key')!)).toEqual(newArray);
  });

  it('should fallback to initial value on JSON parse error', () => {
    // Set invalid JSON in localStorage
    localStorage.setItem('test-key', 'invalid-json{');
    
    const consoleWarnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    
    const { result } = renderHook(() => useLocalStorage('test-key', 'fallback'));
    
    expect(result.current[0]).toBe('fallback');
    expect(consoleWarnSpy).toHaveBeenCalledWith(
      expect.stringContaining('Error reading localStorage key "test-key"'),
      expect.any(Error)
    );
    
    consoleWarnSpy.mockRestore();
  });

  it('should handle null initial value', () => {
    const { result } = renderHook(() => useLocalStorage<string | null>('test-key', null));
    
    expect(result.current[0]).toBe(null);
    
    act(() => {
      result.current[1]('value');
    });
    
    expect(result.current[0]).toBe('value');
  });

  it('should handle boolean values', () => {
    const { result } = renderHook(() => useLocalStorage('test-key', false));
    
    act(() => {
      result.current[1](true);
    });
    
    expect(result.current[0]).toBe(true);
    expect(localStorage.getItem('test-key')).toBe('true');
  });

  it('should handle number values', () => {
    const { result } = renderHook(() => useLocalStorage('test-key', 0));
    
    act(() => {
      result.current[1](42);
    });
    
    expect(result.current[0]).toBe(42);
    expect(localStorage.getItem('test-key')).toBe('42');
  });

  it('should persist across hook re-renders', () => {
    const { result, rerender } = renderHook(() => useLocalStorage('test-key', 'initial'));
    
    act(() => {
      result.current[1]('updated');
    });
    
    rerender();
    
    expect(result.current[0]).toBe('updated');
  });

  it('should handle multiple hooks with different keys', () => {
    const { result: result1 } = renderHook(() => useLocalStorage('key1', 'value1'));
    const { result: result2 } = renderHook(() => useLocalStorage('key2', 'value2'));
    
    act(() => {
      result1.current[1]('new-value1');
      result2.current[1]('new-value2');
    });
    
    expect(result1.current[0]).toBe('new-value1');
    expect(result2.current[0]).toBe('new-value2');
    expect(localStorage.getItem('key1')).toBe(JSON.stringify('new-value1'));
    expect(localStorage.getItem('key2')).toBe(JSON.stringify('new-value2'));
  });
});
