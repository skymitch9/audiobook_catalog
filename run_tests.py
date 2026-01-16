#!/usr/bin/env python3
"""
Test runner for audiobook catalog.
Runs all unit tests and generates a report.
"""
import unittest
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))


def run_all_tests(verbosity=2):
    """Run all tests in the tests directory."""
    loader = unittest.TestLoader()
    start_dir = project_root / 'tests'
    suite = loader.discover(start_dir, pattern='test_*.py')
    
    runner = unittest.TextTestRunner(verbosity=verbosity)
    result = runner.run(suite)
    
    return result.wasSuccessful()


def run_specific_test(test_module, verbosity=2):
    """Run a specific test module."""
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromName(f'tests.{test_module}')
    
    runner = unittest.TextTestRunner(verbosity=verbosity)
    result = runner.run(suite)
    
    return result.wasSuccessful()


if __name__ == '__main__':
    import argparse
    
    parser = argparse.ArgumentParser(description='Run audiobook catalog tests')
    parser.add_argument('test', nargs='?', help='Specific test module to run (e.g., test_title_parsing)')
    parser.add_argument('-v', '--verbose', action='store_true', help='Verbose output')
    parser.add_argument('-q', '--quiet', action='store_true', help='Quiet output')
    
    args = parser.parse_args()
    
    # Determine verbosity
    verbosity = 2
    if args.verbose:
        verbosity = 3
    elif args.quiet:
        verbosity = 0
    
    # Run tests
    if args.test:
        success = run_specific_test(args.test, verbosity)
    else:
        success = run_all_tests(verbosity)
    
    # Exit with appropriate code
    sys.exit(0 if success else 1)
