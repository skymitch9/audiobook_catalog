import { describe, it, expect, beforeEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import * as fc from 'fast-check';
import { useLocalStorage } from '../../hooks/useLocalStorage';

describe('Property-Based Tests: Persistence', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it('Property 8: Theme persistence round-trip - saving and reading theme returns same value', () => {
    /**
     * Feature: audiobook-react-enhanced, Property 8: Theme persistence round-trip
     * 
     * **Validates: Requirements 3.3, 3.4**
     * 
     * For any theme mode (light or dark), saving the theme to localStorage 
     * and then reading it back should return the same theme mode.
     */
    fc.assert(
      fc.property(fc.constantFrom('light', 'dark'), (theme) => {
        const key = 'test-theme';
        
        // First hook instance: save the theme
        const { result: result1 } = renderHook(() => 
          useLocalStorage<'light' | 'dark'>(key, 'dark')
        );
        
        act(() => {
          result1.current[1](theme);
        });
        
        // Second hook instance: read the theme (simulates new session)
        const { result: result2 } = renderHook(() => 
          useLocalStorage<'light' | 'dark'>(key, 'dark')
        );
        
        // The read value should match the saved value
        expect(result2.current[0]).toBe(theme);
        
        // Cleanup
        localStorage.removeItem(key);
      }),
      { numRuns: 100 }
    );
  });

  it('Property 9: View mode persistence round-trip - saving and reading view mode returns same value', () => {
    /**
     * Feature: audiobook-react-enhanced, Property 9: View mode persistence round-trip
     * 
     * **Validates: Requirements 6.6**
     * 
     * For any view mode (grid or table), saving the view mode to localStorage 
     * and then reading it back should return the same view mode.
     */
    fc.assert(
      fc.property(fc.constantFrom('grid', 'table'), (viewMode) => {
        const key = 'test-viewMode';
        
        // First hook instance: save the view mode
        const { result: result1 } = renderHook(() => 
          useLocalStorage<'grid' | 'table'>(key, 'grid')
        );
        
        act(() => {
          result1.current[1](viewMode);
        });
        
        // Second hook instance: read the view mode (simulates new session)
        const { result: result2 } = renderHook(() => 
          useLocalStorage<'grid' | 'table'>(key, 'grid')
        );
        
        // The read value should match the saved value
        expect(result2.current[0]).toBe(viewMode);
        
        // Cleanup
        localStorage.removeItem(key);
      }),
      { numRuns: 100 }
    );
  });

  it('Property: Generic persistence round-trip - any JSON-serializable value persists correctly', () => {
    /**
     * Generic persistence property: For any JSON-serializable value,
     * saving to localStorage and reading back should return the same value.
     */
    fc.assert(
      fc.property(
        fc.string({ minLength: 1, maxLength: 20 }), // key
        fc.oneof(
          fc.string(),
          fc.integer(),
          fc.boolean(),
          fc.constant(null),
          fc.array(fc.integer(), { maxLength: 10 }),
          fc.record({
            str: fc.string(),
            num: fc.integer(),
            bool: fc.boolean()
          })
        ), // value
        (key, value) => {
          // First hook instance: save the value
          const { result: result1 } = renderHook(() => 
            useLocalStorage(key, value)
          );
          
          act(() => {
            result1.current[1](value);
          });
          
          // Second hook instance: read the value
          const { result: result2 } = renderHook(() => 
            useLocalStorage(key, value)
          );
          
          // The read value should deeply equal the saved value
          expect(result2.current[0]).toEqual(value);
          
          // Cleanup
          localStorage.removeItem(key);
        }
      ),
      { numRuns: 100 }
    );
  });

  it('Property: Parse error fallback - invalid JSON always returns initial value', () => {
    /**
     * Error handling property: For any initial value and invalid JSON string,
     * the hook should gracefully fall back to the initial value.
     */
    fc.assert(
      fc.property(
        fc.string({ minLength: 1, maxLength: 20 }), // key
        fc.string(), // initial value
        fc.constantFrom(
          'invalid-json{',
          '{incomplete',
          'undefined',
          'NaN',
          '{key: value}', // unquoted keys
          "{'single': 'quotes'}" // single quotes
        ), // invalid JSON
        (key, initialValue, invalidJson) => {
          // Set invalid JSON in localStorage
          localStorage.setItem(key, invalidJson);
          
          // Hook should return initial value
          const { result } = renderHook(() => 
            useLocalStorage(key, initialValue)
          );
          
          expect(result.current[0]).toBe(initialValue);
          
          // Cleanup
          localStorage.removeItem(key);
        }
      ),
      { numRuns: 100 }
    );
  });

  it('Property: Multiple keys independence - changes to one key do not affect others', () => {
    /**
     * Independence property: For any two different keys and values,
     * updating one key should not affect the value stored in another key.
     */
    fc.assert(
      fc.property(
        fc.string({ minLength: 1, maxLength: 20 }), // key1
        fc.string({ minLength: 1, maxLength: 20 }), // key2
        fc.string(), // value1
        fc.string(), // value2
        fc.string(), // newValue1
        (key1, key2, value1, value2, newValue1) => {
          // Skip if keys are the same
          fc.pre(key1 !== key2);
          
          // Create two hooks with different keys
          const { result: result1 } = renderHook(() => 
            useLocalStorage(key1, value1)
          );
          const { result: result2 } = renderHook(() => 
            useLocalStorage(key2, value2)
          );
          
          // Update first key
          act(() => {
            result1.current[1](newValue1);
          });
          
          // Second key should remain unchanged
          expect(result2.current[0]).toBe(value2);
          
          // Cleanup
          localStorage.removeItem(key1);
          localStorage.removeItem(key2);
        }
      ),
      { numRuns: 100 }
    );
  });
});
