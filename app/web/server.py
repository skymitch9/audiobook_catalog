"""
Flask web server for serving React frontend and API endpoints.
"""

from pathlib import Path
from flask import Flask, send_from_directory, jsonify
from flask_cors import CORS

from app.web.routes import register_routes


def create_app():
    """Create and configure the Flask application."""
    # Disable Flask's default static folder to avoid conflicts
    app = Flask(__name__, static_folder=None)
    
    # Enable CORS for all routes
    CORS(app)
    
    # Configuration
    app.config['REACT_BUILD_DIR'] = Path(__file__).parent.parent.parent / 'site' / 'build'
    app.config['ARCHIVE_DIR'] = Path(__file__).parent.parent.parent / 'site' / 'archive'
    app.config['COVERS_DIR'] = Path(__file__).parent.parent.parent / 'site' / 'covers'
    app.config['CATALOG_CSV'] = Path(__file__).parent.parent.parent / 'site' / 'catalog.csv'
    
    # Register routes
    register_routes(app)
    
    return app


def run_server(host='0.0.0.0', port=5000, debug=True):
    """Run the Flask development server."""
    app = create_app()
    app.run(host=host, port=port, debug=debug)


if __name__ == '__main__':
    run_server()
