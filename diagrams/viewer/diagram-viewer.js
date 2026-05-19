// SVG-fetch variant of the diagram viewer.
// Adapted from ~/dev/market-simulation/docs/diagram.js (which renders Mermaid);
// this version fetches pre-rendered .svg files from <div class="diagram-host" data-src=...>,
// inlines them, and attaches a fullscreen button that opens svg-pan-zoom in a modal.
//
// Requires (loaded before this file):
//   - svg-pan-zoom (https://cdn.jsdelivr.net/npm/svg-pan-zoom@3.6.1/dist/svg-pan-zoom.min.js)
//   - diagram-viewer.css

(function () {
  'use strict';

  let currentPanZoom = null;
  let currentModal = null;

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

  function showError(host, src, errorMessage) {
    const err = document.createElement('div');
    err.className = 'render-error';
    err.innerHTML =
      '<strong>Diagram failed to load.</strong>' +
      '<p style="margin: 6px 0 0;">Could not fetch or parse <code>' + escapeHtml(src) + '</code>.</p>' +
      '<pre>' + escapeHtml(errorMessage) + '</pre>' +
      '<p style="margin-top: 8px; font-size: 0.85em;">Run <code>./scripts/render-diagrams.sh</code> to render + inline SVGs. The inliner makes the page work via <code>file://</code> without a dev server.</p>';
    host.appendChild(err);
  }

  function attachFullscreenButton(host, svgEl) {
    const btn = document.createElement('button');
    btn.className = 'fullscreen-btn';
    btn.type = 'button';
    btn.innerHTML = '⛶';
    btn.title = 'Open fullscreen (pan / zoom)';
    btn.setAttribute('aria-label', 'Open diagram fullscreen');
    btn.addEventListener('click', function () {
      openDiagramModal(svgEl);
    });
    host.appendChild(btn);
  }

  function openDiagramModal(originalSvg) {
    if (currentModal) closeDiagramModal();

    const clone = originalSvg.cloneNode(true);
    clone.removeAttribute('width');
    clone.removeAttribute('height');
    clone.style.width = '100%';
    clone.style.height = '100%';
    clone.setAttribute('preserveAspectRatio', 'xMidYMid meet');

    const modal = document.createElement('div');
    modal.className = 'diagram-modal';
    modal.innerHTML =
      '<div class="diagram-modal-header">' +
        '<span class="diagram-modal-hint">Scroll to zoom · Drag to pan · Esc to close</span>' +
        '<button class="diagram-modal-close" type="button">Close</button>' +
      '</div>' +
      '<div class="diagram-modal-body"></div>';
    modal.querySelector('.diagram-modal-body').appendChild(clone);
    document.body.appendChild(modal);
    currentModal = modal;

    modal.querySelector('.diagram-modal-close').addEventListener('click', closeDiagramModal);
    modal.addEventListener('click', function (e) {
      if (e.target === modal) closeDiagramModal();
    });

    if (typeof svgPanZoom !== 'undefined') {
      setTimeout(function () {
        try {
          currentPanZoom = svgPanZoom(clone, {
            panEnabled: true,
            zoomEnabled: true,
            dblClickZoomEnabled: true,
            mouseWheelZoomEnabled: true,
            controlIconsEnabled: true,
            fit: true,
            center: true,
            minZoom: 0.3,
            maxZoom: 10,
            zoomScaleSensitivity: 0.3
          });
        } catch (err) {
          console.warn('svg-pan-zoom failed to initialize:', err);
        }
      }, 50);
    }

    document.body.style.overflow = 'hidden';
  }

  function closeDiagramModal() {
    if (currentPanZoom) {
      try { currentPanZoom.destroy(); } catch (e) { /* ignore */ }
      currentPanZoom = null;
    }
    if (currentModal) {
      currentModal.remove();
      currentModal = null;
    }
    document.body.style.overflow = '';
  }

  async function renderDiagram(host) {
    // Prefer a pre-inlined <svg> child (produced by scripts/inline-svgs.py).
    // This makes the page work via file:// without fetch().
    const existing = host.querySelector(':scope > svg');
    if (existing) {
      host.querySelectorAll('noscript').forEach(function (n) { n.remove(); });
      attachFullscreenButton(host, existing);
      return;
    }

    // Fallback: fetch the SVG via data-src. Requires the page to be served
    // over http:// (a dev server, GitHub Pages, etc.); fails on file:// CORS.
    const src = host.getAttribute('data-src');
    if (!src) return;
    try {
      const res = await fetch(src);
      if (!res.ok) throw new Error('HTTP ' + res.status + ' ' + res.statusText);
      const svgText = await res.text();

      const parser = new DOMParser();
      const doc = parser.parseFromString(svgText, 'image/svg+xml');
      const svg = doc.documentElement;
      if (!svg || svg.tagName.toLowerCase() !== 'svg') {
        throw new Error('Fetched file is not an SVG document.');
      }

      host.querySelectorAll('noscript').forEach(function (n) { n.remove(); });
      host.appendChild(svg);
      attachFullscreenButton(host, svg);
    } catch (err) {
      showError(host, src, (err && (err.message || String(err))) || 'Unknown error.');
    }
  }

  function renderAllDiagrams() {
    const hosts = Array.from(document.querySelectorAll('.diagram-host[data-src]'));
    hosts.forEach(renderDiagram);
    renderAllMermaid();
  }

  function showMermaidError(targetEl, source, errorMessage, headline) {
    const errorBlock = document.createElement('div');
    errorBlock.className = 'render-error';
    errorBlock.innerHTML =
      '<strong>' + escapeHtml(headline || 'Mermaid diagram failed to render.') + '</strong>' +
      '<pre>' + escapeHtml(errorMessage) + '</pre>' +
      (source
        ? '<details style="margin-top: 8px;"><summary style="cursor: pointer; font-size: 12.5px;">Show source</summary><pre>' + escapeHtml(source) + '</pre></details>'
        : '');
    targetEl.replaceWith(errorBlock);
  }

  async function renderAllMermaid() {
    const blocks = Array.from(document.querySelectorAll('pre.mermaid'));
    if (blocks.length === 0) return;

    if (typeof mermaid === 'undefined') {
      blocks.forEach(function (block) {
        showMermaidError(
          block,
          block.textContent.trim(),
          'The Mermaid library failed to load. Check your network connection or CSP configuration.',
          'Mermaid library not available.'
        );
      });
      return;
    }

    try {
      mermaid.initialize({
        startOnLoad: false,
        theme: 'default',
        securityLevel: 'loose',
        flowchart: { htmlLabels: true, curve: 'basis', nodeSpacing: 30, rankSpacing: 50 },
        sequence: { mirrorActors: false, boxMargin: 8, messageMargin: 28, noteMargin: 8 }
      });
    } catch (initErr) {
      blocks.forEach(function (block) {
        showMermaidError(
          block,
          block.textContent.trim(),
          (initErr && (initErr.message || String(initErr))) || 'Unknown initialization error.',
          'Mermaid initialization failed.'
        );
      });
      return;
    }

    for (let i = 0; i < blocks.length; i++) {
      const block = blocks[i];
      const source = block.textContent.trim();
      const id = 'mermaid-diagram-' + i + '-' + Date.now();
      try {
        const result = await mermaid.render(id, source);
        const container = document.createElement('div');
        // Reuse the diagram-host styles + fullscreen button infrastructure.
        container.className = 'diagram-host mermaid-rendered';
        container.innerHTML = result.svg;
        if (typeof result.bindFunctions === 'function') {
          result.bindFunctions(container);
        }
        block.replaceWith(container);

        const svg = container.querySelector('svg');
        if (svg) attachFullscreenButton(container, svg);
      } catch (err) {
        const errorMessage =
          (err && (err.message || err.str || String(err))) || 'Unknown rendering error.';
        showMermaidError(block, source, errorMessage);
      }
    }
  }

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && currentModal) closeDiagramModal();
  });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', renderAllDiagrams);
  } else {
    renderAllDiagrams();
  }
})();
