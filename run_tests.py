#!/usr/bin/env python3
"""
Test runner for audiobook catalog.
Runs all unit tests and generates a report.
"""
import unittest
import sys
import time
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))


def print_header(text, char='='):
    """Print a formatted header."""
    width = 70
    print()
    print(char * width)
    print(f" {text}")
    print(char * width)


def print_section(text):
    """Print a section header."""
    print(f"\n{'─' * 70}")
    print(f"▶ {text}")
    print('─' * 70)


def run_all_tests(verbosity=2):
    """Run all tests in the tests directory."""
    print_header("Audiobook Catalog Test Suite")
    print(f"Python: {sys.version.split()[0]}")
    print(f"Working directory: {project_root}")
    
    loader = unittest.TestLoader()
    start_dir = project_root / 'tests'
    
    print_section("Discovering tests...")
    suite = loader.discover(start_dir, pattern='test_*.py')
    test_count = suite.countTestCases()
    print(f"Found {test_count} tests")
    
    print_section("Running tests...")
    start_time = time.time()
    
    runner = unittest.TextTestRunner(verbosity=verbosity)
    result = runner.run(suite)
    
    elapsed = time.time() - start_time
    
    # Print summary
    print_header("Test Summary", '=')
    print(f"Tests run: {result.testsRun}")
    print(f"Successes: {result.testsRun - len(result.failures) - len(result.errors) - len(result.skipped)}")
    print(f"Failures: {len(result.failures)}")
    print(f"Errors: {len(result.errors)}")
    print(f"Skipped: {len(result.skipped)}")
    print(f"Time: {elapsed:.2f}s")
    
    if result.wasSuccessful():
        print("\n✓ All tests passed!")
    else:
        print("\n✗ Some tests failed")
    
    print('=' * 70)
    print()
    
    return result.wasSuccessful()


def run_specific_test(test_module, verbosity=2):
    """Run a specific test module."""
    print_header(f"Running: {test_module}")
    
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromName(f'tests.{test_module}')
    
    test_count = suite.countTestCases()
    print(f"Found {test_count} tests in {test_module}")
    
    print_section("Running tests...")
    start_time = time.time()
    
    runner = unittest.TextTestRunner(verbosity=verbosity)
    result = runner.run(suite)
    
    elapsed = time.time() - start_time
    
    # Print summary
    print_header("Test Summary", '=')
    print(f"Tests run: {result.testsRun}")
    print(f"Successes: {result.testsRun - len(result.failures) - len(result.errors) - len(result.skipped)}")
    print(f"Failures: {len(result.failures)}")
    print(f"Errors: {len(result.errors)}")
    print(f"Skipped: {len(result.skipped)}")
    print(f"Time: {elapsed:.2f}s")
    
    if result.wasSuccessful():
        print("\n✓ All tests passed!")
    else:
        print("\n✗ Some tests failed")
    
    print('=' * 70)
    print()
    
    return result.wasSuccessful()


if __name__ == '__main__':
    import argparse
    
    parser = argparse.ArgumentParser(description='Run audiobook catalog tests')
    parser.add_argument('test', nargs='?', help='Specific test module to run (e.g., test_title_parsing)')
    parser.add_argument('-v', '--verbose', action='store_true', help='Verbose output')
    parser.add_argument('-q', '--quiet', action='store_true', help='Quiet output (minimal formatting)')
    
    args = parser.parse_args()
    
    # Determine verbosity
    verbosity = 2
    if args.verbose:
        verbosity = 3
    elif args.quiet:
        verbosity = 1
    
    # Run tests
    try:
        if args.test:
            success = run_specific_test(args.test, verbosity)
        else:
            success = run_all_tests(verbosity)
    except Exception as e:
        print(f"\n✗ Error running tests: {e}", file=sys.stderr)
        sys.exit(1)
    
    # Exit with appropriate code
    sys.exit(0 if success else 1)
