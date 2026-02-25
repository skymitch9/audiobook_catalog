import { Component } from 'react';
import type { ErrorInfo, ReactNode } from 'react';

interface ErrorBoundaryProps {
  children: ReactNode;
}

interface ErrorBoundaryState {
  hasError: boolean;
  error: Error | null;
}

class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  constructor(props: ErrorBoundaryProps) {
    super(props);
    this.state = {
      hasError: false,
      error: null,
    };
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo): void {
    // Log error to console for debugging
    console.error('ErrorBoundary caught an error:', error, errorInfo);
    
    // Update state to display fallback UI
    this.setState({
      hasError: true,
      error: error,
    });
  }

  render(): ReactNode {
    if (this.state.hasError) {
      // Display fallback UI when an error occurs
      return (
        <div style={{
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          minHeight: '100vh',
          padding: '20px',
          textAlign: 'center',
        }}>
          <h1 style={{ color: '#d32f2f', marginBottom: '16px' }}>
            Something went wrong
          </h1>
          <p style={{ color: '#666', marginBottom: '24px', maxWidth: '600px' }}>
            We're sorry, but something unexpected happened. Please try refreshing the page.
          </p>
          {this.state.error && (
            <details style={{ marginTop: '16px', textAlign: 'left', maxWidth: '600px' }}>
              <summary style={{ cursor: 'pointer', color: '#1976d2' }}>
                Error details
              </summary>
              <pre style={{
                marginTop: '8px',
                padding: '12px',
                backgroundColor: '#f5f5f5',
                borderRadius: '4px',
                overflow: 'auto',
                fontSize: '12px',
              }}>
                {this.state.error.toString()}
              </pre>
            </details>
          )}
          <button
            onClick={() => window.location.reload()}
            style={{
              marginTop: '24px',
              padding: '10px 20px',
              backgroundColor: '#1976d2',
              color: 'white',
              border: 'none',
              borderRadius: '4px',
              cursor: 'pointer',
              fontSize: '16px',
            }}
          >
            Refresh Page
          </button>
        </div>
      );
    }

    return this.props.children;
  }
}

export default ErrorBoundary;
