"""
Route handlers for Flask web server.
"""

import csv
from pathlib import Path
from flask import send_from_directory, jsonify, request, send_file


def register_routes(app):  # noqa: C901
    """Register all routes for the Flask application."""
    
    # ========== Home Page (Archive Catalog) ==========
    
    @app.route('/')
    def serve_home():
        """Serve the archived static HTML catalog as the home page."""
        archive_dir = app.config['ARCHIVE_DIR']
        if not archive_dir.exists():
            return jsonify({
                'error': 'Archive not found',
                'message': 'Please run "python -m app.main" to generate the catalog'
            }), 404
        return send_from_directory(archive_dir, 'index.html')
    
    # ========== React App Routes ==========
    
    @app.route('/catalog')
    @app.route('/catalog/')
    def serve_react_app():
        """Serve the React app at /catalog route."""
        react_build_dir = app.config['REACT_BUILD_DIR']
        if not react_build_dir.exists():
            return jsonify({
                'error': 'React build not found',
                'message': 'Please run "cd frontend && npm run build" to build the React app'
            }), 503
        return send_from_directory(react_build_dir, 'index.html')
    
    @app.route('/static/<path:path>')
    def serve_react_static(path):
        """Serve React static assets (JS, CSS, etc.)."""
        react_build_dir = app.config['REACT_BUILD_DIR']
        static_dir = react_build_dir / 'static'
        
        if not static_dir.exists():
            return jsonify({'error': 'Static directory not found'}), 404
        
        file_path = static_dir / path
        if not file_path.exists():
            return jsonify({'error': f'File not found: {path}'}), 404
            
        return send_from_directory(static_dir, path)
    
    # ========== API Routes ==========
    
    @app.route('/api/books', methods=['GET'])
    def get_books():
        """Get all books from catalog.csv."""
        catalog_csv = app.config['CATALOG_CSV']
        
        if not catalog_csv.exists():
            return jsonify({
                'error': 'Catalog not found',
                'message': 'Please run "python -m app.main" to generate the catalog'
            }), 500
        
        try:
            books = []
            with open(catalog_csv, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for idx, row in enumerate(reader, start=1):
                    # Add id field (row number)
                    row['id'] = str(idx)
                    # Transform cover_href to cover_url with full API path
                    if 'cover_href' in row and row['cover_href']:
                        row['cover_url'] = f"/api/{row['cover_href']}"
                    books.append(row)
            
            return jsonify(books)
        except Exception as e:
            return jsonify({'error': 'Failed to read catalog', 'message': str(e)}), 500
    
    @app.route('/api/books/search', methods=['GET'])
    def search_books():
        """Search books by query parameter."""
        query = request.args.get('q', '').lower()
        catalog_csv = app.config['CATALOG_CSV']
        
        if not catalog_csv.exists():
            return jsonify({
                'error': 'Catalog not found',
                'message': 'Please run "python -m app.main" to generate the catalog'
            }), 500
        
        try:
            books = []
            with open(catalog_csv, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for idx, row in enumerate(reader, start=1):
                    # Search across multiple fields
                    searchable_text = ' '.join([
                        row.get('title', ''),
                        row.get('author', ''),
                        row.get('narrator', ''),
                        row.get('series', ''),
                        row.get('genre', '')
                    ]).lower()
                    
                    if query in searchable_text:
                        # Add id field (row number)
                        row['id'] = str(idx)
                        # Transform cover_href to cover_url with full API path
                        if 'cover_href' in row and row['cover_href']:
                            row['cover_url'] = f"/api/{row['cover_href']}"
                        books.append(row)
            
            return jsonify(books)
        except Exception as e:
            return jsonify({'error': 'Failed to search catalog', 'message': str(e)}), 500
    
    @app.route('/api/books/<book_id>', methods=['GET'])
    def get_book(book_id):
        """Get a single book by ID (row number)."""
        catalog_csv = app.config['CATALOG_CSV']
        
        if not catalog_csv.exists():
            return jsonify({
                'error': 'Catalog not found',
                'message': 'Please run "python -m app.main" to generate the catalog'
            }), 500
        
        try:
            # Convert book_id to integer for row number lookup
            try:
                target_id = int(book_id)
            except ValueError:
                return jsonify({'error': 'Invalid book ID'}), 400
            
            with open(catalog_csv, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for idx, row in enumerate(reader, start=1):
                    if idx == target_id:
                        # Add id field
                        row['id'] = str(idx)
                        # Transform cover_href to cover_url with full API path
                        if 'cover_href' in row and row['cover_href']:
                            row['cover_url'] = f"/api/{row['cover_href']}"
                        return jsonify(row)
            
            return jsonify({'error': 'Book not found'}), 404
        except Exception as e:
            return jsonify({'error': 'Failed to read catalog', 'message': str(e)}), 500
    
    @app.route('/api/covers/<path:filename>', methods=['GET'])
    def get_cover(filename):
        """Serve cover images."""
        covers_dir = app.config['COVERS_DIR']
        cover_path = covers_dir / filename
        
        if not cover_path.exists():
            return jsonify({'error': 'Cover not found'}), 404
        
        return send_file(cover_path)
    
    # ========== Archive Routes ==========
    
    @app.route('/archive')
    @app.route('/archive/')
    def serve_archive_index():
        """Serve the archived static HTML catalog index."""
        archive_dir = app.config['ARCHIVE_DIR']
        if not archive_dir.exists():
            return jsonify({'error': 'Archive not found'}), 404
        return send_from_directory(archive_dir, 'index.html')
    
    @app.route('/archive/<path:path>')
    def serve_archive_files(path):
        """Serve archived static HTML files and assets."""
        archive_dir = app.config['ARCHIVE_DIR']
        return send_from_directory(archive_dir, path)
    
    # ========== Catch-all for React Router ==========
    
    @app.route('/catalog/<path:path>')
    def catch_all_catalog(path):
        """Catch-all route to support React Router client-side routing under /catalog."""
        # Serve React app for all /catalog/* routes
        react_build_dir = app.config['REACT_BUILD_DIR']
        if not react_build_dir.exists():
            return jsonify({
                'error': 'React build not found',
                'message': 'Please run "cd frontend && npm run build" to build the React app'
            }), 503
        return send_from_directory(react_build_dir, 'index.html')
    
    @app.route('/<path:path>')
    def catch_all(path):
        """Catch-all route for other paths."""
        # Don't catch API routes
        if path.startswith('api/'):
            return jsonify({'error': 'Not found'}), 404
        
        # Serve archive files for other routes
        archive_dir = app.config['ARCHIVE_DIR']
        if archive_dir.exists():
            file_path = archive_dir / path
            if file_path.exists() and file_path.is_file():
                return send_from_directory(archive_dir, path)
        
        return jsonify({'error': 'Not found'}), 404
