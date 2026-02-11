#!/usr/bin/env python3
"""
Standalone statistics generator for audiobook catalog
Run this to generate just the statistics page
"""

if __name__ == "__main__":
    from app.tools.generate_stats import main
    main()