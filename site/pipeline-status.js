// pipeline-status.js — live pipeline status card + manual "run now" trigger.
//
// Reads pipeline_status/current (written by the home machine via a service
// account) and renders what the sync run is doing right now, so new books can
// be checked on from anywhere instead of walking to the PC or waiting out the
// silent 8-hourly schedule.
//
// The trigger works by writing a request doc that a watcher on that machine
// polls for. Firestore rules make pipeline_requests create-only and UNREADABLE
// — see the note in firestore.rules. The token below is the only thing that
// makes a request real, so it is entered once by the admin and kept in
// localStorage; it is never baked into this file or committed.
//
// ES module, browser-native, no build step (matches the rest of site/).

import {
  collection, doc, addDoc, onSnapshot, query, orderBy, limit, getDocs,
} from 'https://www.gstatic.com/firebasejs/10.8.0/firebase-firestore.js';
import { col } from './fb-env.js';

const TOKEN_KEY = 'pipelineTriggerToken';

export function getToken() {
  try { return localStorage.getItem(TOKEN_KEY) || ''; } catch { return ''; }
}
export function setToken(v) {
  try { localStorage.setItem(TOKEN_KEY, (v || '').trim()); } catch { /* private mode */ }
}

const esc = (s) => String(s ?? '').replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function ago(iso) {
  if (!iso) return '';
  const secs = (Date.now() - new Date(iso).getTime()) / 1000;
  if (Number.isNaN(secs)) return '';
  if (secs < 60) return `${Math.max(0, Math.round(secs))}s ago`;
  if (secs < 3600) return `${Math.round(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.round(secs / 3600)}h ago`;
  return `${Math.round(secs / 86400)}d ago`;
}

function fmtDuration(sec) {
  if (!sec && sec !== 0) return '';
  if (sec < 60) return `${sec}s`;
  return `${Math.floor(sec / 60)}m ${sec % 60}s`;
}

const STEP_MARK = { done: '✓', active: '▸', failed: '✕', pending: '·', skipped: '–' };

/**
 * A run that never called finish_run (power cut, forced reboot) would otherwise
 * show "running" forever. Treat a stale heartbeat as unknown rather than live.
 */
function isStale(status) {
  if (status?.state !== 'running') return false;
  const t = new Date(status.updatedAt || 0).getTime();
  return Number.isFinite(t) && (Date.now() - t) > 15 * 60 * 1000;
}

function renderSteps(status) {
  return (status.steps || []).map((s) => {
    const isActive = s.state === 'active';
    const cls = `pl-step pl-step--${esc(s.state)}`;
    const detail = s.detail ? `<span class="pl-step__detail">${esc(s.detail)}</span>` : '';
    let bar = '';
    if (isActive && status.progress && status.stepKey === 'upload') {
      const p = status.progress;
      const pct = Math.max(0, Math.min(100, Number(p.pct) || 0));
      bar = `
        <div class="pl-progress">
          <div class="pl-progress__bar"><span style="width:${pct}%"></span></div>
          <div class="pl-progress__label">
            ${esc(p.file)} · ${pct}%${p.sizeMb ? ` · ${esc(p.sizeMb)} MB` : ''}
            ${p.total ? ` · file ${esc(p.index)}/${esc(p.total)}` : ''}
          </div>
        </div>`;
    }
    return `<li class="${cls}">
        <span class="pl-step__mark">${STEP_MARK[s.state] || '·'}</span>
        <span class="pl-step__label">${esc(s.label)}</span>
        ${detail}
      </li>${bar}`;
  }).join('');
}

function renderSummary(status) {
  const s = status.summary || {};
  const bits = [];
  if (s.idle) bits.push('nothing new to upload');
  if (s.uploaded) bits.push(`${s.uploaded} uploaded`);
  if (s.failed) bits.push(`${s.failed} failed`);
  if (s.books) bits.push(`${s.books} books total`);
  if (Array.isArray(s.newBooks) && s.newBooks.length) {
    bits.push(`new: ${s.newBooks.slice(0, 3).map(esc).join(', ')}${s.newBooks.length > 3 ? '…' : ''}`);
  }
  return bits.length ? `<div class="pl-summary">${bits.join(' · ')}</div>` : '';
}

export function renderStatus(el, status) {
  if (!status) {
    el.innerHTML = `<div class="pl-card pl-card--idle">
      <div class="pl-head"><span class="pl-dot pl-dot--idle"></span><strong>Pipeline</strong>
      <span class="pl-state">no runs recorded yet</span></div>
      <p class="pl-hint">The status card fills in the first time the pipeline runs
      with credentials configured.</p></div>`;
    return;
  }

  const stale = isStale(status);
  const state = stale ? 'unknown' : (status.state || 'unknown');
  const label = {
    running: 'RUNNING', success: 'SUCCESS', partial: 'PARTIAL',
    failed: 'FAILED', unknown: 'NO HEARTBEAT',
  }[state] || state.toUpperCase();

  const when = status.state === 'running' && !stale
    ? `started ${ago(status.startedAt)}`
    : `${ago(status.finishedAt || status.updatedAt)}${status.durationSec ? ` · took ${fmtDuration(status.durationSec)}` : ''}`;

  el.innerHTML = `
    <div class="pl-card pl-card--${esc(state)}">
      <div class="pl-head">
        <span class="pl-dot pl-dot--${esc(state)}"></span>
        <strong>Pipeline</strong>
        <span class="pl-state">${esc(label)}</span>
        <span class="pl-when">${esc(when)}</span>
        <span class="pl-trigger">${esc(status.trigger || '')}</span>
      </div>
      ${stale ? `<div class="pl-error">No heartbeat for over 15 minutes — the run may
        have been interrupted. Check output_files/pipeline_8h.log on the machine.</div>` : ''}
      ${status.error ? `<div class="pl-error">${esc(status.error)}</div>` : ''}
      <ul class="pl-steps">${renderSteps(status)}</ul>
      ${renderSummary(status)}
    </div>`;
}

export function watchStatus(db, el) {
  const ref = doc(collection(db, col('pipeline_status')), 'current');
  return onSnapshot(
    ref,
    (snap) => renderStatus(el, snap.exists() ? snap.data() : null),
    (err) => {
      el.innerHTML = `<div class="pl-card pl-card--failed">
        <div class="pl-head"><span class="pl-dot pl-dot--failed"></span><strong>Pipeline</strong>
        <span class="pl-state">STATUS UNAVAILABLE</span></div>
        <div class="pl-error">${esc(err.message || err)}</div></div>`;
    },
  );
}

export async function loadHistory(db, el, n = 8) {
  try {
    const q = query(collection(db, col('pipeline_runs')), orderBy('startedAt', 'desc'), limit(n));
    const snap = await getDocs(q);
    if (snap.empty) { el.innerHTML = ''; return; }
    const rows = snap.docs.map((d) => {
      const r = d.data();
      const s = r.summary || {};
      const outcome = s.idle ? 'nothing new'
        : [s.uploaded ? `${s.uploaded} uploaded` : '', s.books ? `${s.books} books` : '']
          .filter(Boolean).join(' · ') || '—';
      return `<tr>
        <td>${esc(new Date(r.startedAt).toLocaleString())}</td>
        <td class="pl-hist__state pl-hist__state--${esc(r.state)}">${esc(r.state)}</td>
        <td>${esc(r.trigger || '')}</td>
        <td>${esc(fmtDuration(r.durationSec))}</td>
        <td>${outcome}</td>
      </tr>`;
    }).join('');
    el.innerHTML = `<h3 class="pl-hist__title">Recent runs</h3>
      <table class="pl-hist">
        <thead><tr><th>Started</th><th>Result</th><th>Trigger</th><th>Took</th><th>Outcome</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
  } catch (e) {
    el.innerHTML = `<p class="pl-hint">Could not load history: ${esc(e.message || e)}</p>`;
  }
}

/**
 * Queue a manual run. Resolves to a human-readable confirmation.
 * Rejects if no token has been set — without it the watcher discards the request.
 */
export async function requestRun(db, requestedBy) {
  const token = getToken();
  if (!token || token.length < 16) {
    throw new Error('No trigger token saved. Paste the PIPELINE_TRIGGER_TOKEN from .env below first.');
  }
  await addDoc(collection(db, col('pipeline_requests')), {
    token,
    requestedAt: new Date().toISOString(),
    requestedBy: String(requestedBy || 'admin').slice(0, 80),
  });
  return 'Requested — the machine checks every 3 minutes, so it starts within ~3 min (or is skipped if a run is already going or the cooldown is active).';
}
