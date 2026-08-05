// @vitest-environment jsdom
// Feature: live pipeline status card + admin manual-run trigger
import { describe, it, expect, beforeEach, vi } from 'vitest';

const addDocMock = vi.fn();
vi.mock('https://www.gstatic.com/firebasejs/10.8.0/firebase-firestore.js', () => ({
  collection: (_db, name) => ({ __name: name }),
  doc: (coll, id) => ({ __coll: coll, __id: id }),
  addDoc: (...args) => addDocMock(...args),
  onSnapshot: vi.fn(),
  query: vi.fn(),
  orderBy: vi.fn(),
  limit: vi.fn(),
  getDocs: vi.fn(),
}));

const { renderStatus, requestRun, getToken, setToken } = await import('../pipeline-status.js');

const baseSteps = [
  { key: 'audit', label: 'Purchase audit', state: 'done', detail: 'current' },
  { key: 'upload', label: 'Upload to Drive', state: 'active', detail: '' },
  { key: 'publish', label: 'Commit & deploy', state: 'pending', detail: '' },
];

const el = () => document.createElement('div');

describe('renderStatus', () => {
  it('shows a placeholder when no run has ever been recorded', () => {
    const d = el();
    renderStatus(d, null);
    expect(d.textContent).toContain('no runs recorded yet');
  });

  it('renders the running state with an upload progress bar', () => {
    const d = el();
    renderStatus(d, {
      state: 'running', startedAt: new Date().toISOString(), updatedAt: new Date().toISOString(),
      stepKey: 'upload', steps: baseSteps,
      progress: { file: 'Book.m4b', pct: 42, index: 2, total: 3, sizeMb: 700 },
      summary: {},
    });
    expect(d.textContent).toContain('RUNNING');
    expect(d.textContent).toContain('Book.m4b');
    expect(d.querySelector('.pl-progress__bar span').style.width).toBe('42%');
  });

  it('flags a run whose heartbeat went stale instead of showing it as live', () => {
    const d = el();
    const old = new Date(Date.now() - 40 * 60 * 1000).toISOString();
    renderStatus(d, { state: 'running', startedAt: old, updatedAt: old, steps: baseSteps, summary: {} });
    expect(d.textContent).toContain('NO HEARTBEAT');
    expect(d.textContent).not.toContain('RUNNING');
  });

  it('reports an idle run as a real success, not an empty card', () => {
    const d = el();
    renderStatus(d, {
      state: 'success', finishedAt: new Date().toISOString(), updatedAt: new Date().toISOString(),
      steps: baseSteps, summary: { idle: true, books: 1067 },
    });
    expect(d.textContent).toContain('SUCCESS');
    expect(d.textContent).toContain('nothing new to upload');
    expect(d.textContent).toContain('1067 books total');
  });

  it('surfaces the error text on a failed run', () => {
    const d = el();
    renderStatus(d, {
      state: 'failed', updatedAt: new Date().toISOString(), steps: baseSteps,
      error: 'Google Drive auth failed', summary: {},
    });
    expect(d.textContent).toContain('FAILED');
    expect(d.textContent).toContain('Google Drive auth failed');
  });

  it('escapes book titles rather than injecting markup', () => {
    const d = el();
    renderStatus(d, {
      state: 'success', updatedAt: new Date().toISOString(), steps: baseSteps,
      summary: { newBooks: ['<img src=x onerror=alert(1)>'] },
    });
    expect(d.querySelector('img')).toBeNull();
    expect(d.textContent).toContain('<img src=x onerror=alert(1)>');
  });
});

describe('requestRun', () => {
  beforeEach(() => {
    addDocMock.mockReset();
    localStorage.clear();
  });

  it('refuses to queue a run when no token is stored', async () => {
    await expect(requestRun({}, 'admin')).rejects.toThrow(/No trigger token/);
    expect(addDocMock).not.toHaveBeenCalled();
  });

  it('refuses a token too short to be real', async () => {
    setToken('tooshort');
    await expect(requestRun({}, 'admin')).rejects.toThrow(/No trigger token/);
    expect(addDocMock).not.toHaveBeenCalled();
  });

  it('writes a request carrying the token when one is stored', async () => {
    setToken('x'.repeat(32));
    await requestRun({}, '!Sky');
    expect(addDocMock).toHaveBeenCalledTimes(1);
    const [, payload] = addDocMock.mock.calls[0];
    expect(payload.token).toBe('x'.repeat(32));
    expect(payload.requestedBy).toBe('!Sky');
    expect(typeof payload.requestedAt).toBe('string');
  });

  it('caps requestedBy so a long name cannot fail the rules validation', async () => {
    setToken('x'.repeat(32));
    await requestRun({}, 'n'.repeat(500));
    const [, payload] = addDocMock.mock.calls[0];
    expect(payload.requestedBy.length).toBe(80);
  });

  it('round-trips the token through localStorage', () => {
    setToken('  ' + 'a'.repeat(20) + '  ');
    expect(getToken()).toBe('a'.repeat(20));
  });
});

describe('skipped steps', () => {
  it('renders steps an idle run never reached as skipped, not pending', () => {
    const d = el();
    renderStatus(d, {
      state: 'success', updatedAt: new Date().toISOString(),
      steps: [
        { key: 'detect', label: 'Detect new books', state: 'done', detail: '0 to upload' },
        { key: 'upload', label: 'Upload to Drive', state: 'skipped', detail: '' },
      ],
      summary: { idle: true },
    });
    const skipped = d.querySelector('.pl-step--skipped');
    expect(skipped).not.toBeNull();
    expect(skipped.textContent).toContain('Upload to Drive');
    expect(d.querySelector('.pl-step--pending')).toBeNull();
  });
});
