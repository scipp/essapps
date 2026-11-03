/*
A tour: a diagram that is a function of a configuration, and slides that are
points of that configuration space, each with narration.

    Tour({
      size: [1200, 780],               // the stage's coordinate system
      keys: ['level', 'caller'],       // the keys of a configuration
      slides: [{ level: 1, caller: 'notebook', title, body, code, planned }],
      groups: [['Workflow', 0, 3]],    // the tour strip: name, first slide, end
      layout(cfg, d) {                 // the diagram, in stage coordinates
        d.box('spec', [x, y, w, h], visible, 'container wf', html);
        d.arrow('p2b', visible, [[x0, y0], [x1, y1]], 'label', [lx, ly], opts);
      },
      offTour: cfg => ({ title, body, code }),  // a configuration that is no slide
      panel: (cfg, tour) => html,               // the side panel, at every show
      click: (button, tour) => {},              // a click on a button in the panel
    });

A box or an arrow keeps its id across configurations, so a change of
configuration moves, fades, and redraws it instead of replacing it. One that a
configuration does not declare, or declares invisible, fades out. A visible box
whose content changed is outlined for a moment.

An arrow is a polyline. Its label sits at `at`, by default above the middle of
the first segment; `pts = null` draws the label alone. Options: `anchor` (the
label's text-anchor), `both` (heads at both ends), and `cls` (classes of the
arrow, such as `unknown` or `link` in tour.css).

`body` is HTML, `code` is Python source, and `planned` names what the slide
shows that does not exist yet. The page provides the elements #tour, #stage,
#caption, #code, and #panel.
*/
function Tour({ size: [W, H], keys, slides, groups, layout, offTour, panel, click }) {
  const $ = id => document.getElementById(id);
  const SVGNS = 'http://www.w3.org/2000/svg';
  const stage = $('stage');
  stage.style.aspectRatio = `${W} / ${H}`;
  stage.style.setProperty('--u', `calc(100cqw / ${W})`);
  stage.innerHTML = `<svg class="arrows" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true"><defs>
    <marker id="head" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
      <path d="M0,0 L10,5 L0,10 z" fill="currentColor"></path></marker></defs></svg>`;
  const svg = stage.lastChild;
  const els = {}, arrowEls = {};
  const pct = r => ({ left: r[0] / W * 100 + '%', top: r[1] / H * 100 + '%', width: r[2] / W * 100 + '%', height: r[3] / H * 100 + '%' });

  // ---------- the diagram ----------
  function render(cfg, animate) {
    const B = {}, A = {};
    layout(cfg, {
      box: (id, r, vis, cls, html) => { B[id] = { r, vis, cls, html }; },
      arrow: (id, vis, pts, label = '', at = null, opts = {}) => {
        A[id] = { vis, pts, label, at: at ?? [(pts[0][0] + pts[1][0]) / 2, pts[0][1] - 8], ...opts };
      },
    });
    for (const [id, b] of Object.entries(B)) {
      let el = els[id];
      if (!el) {
        el = els[id] = document.createElement('div');
        el.innerHTML = '<div class="content"></div>';
        stage.insertBefore(el, svg);
      }
      const content = el.firstChild;
      const wasOn = el.dataset.on === '1';
      const sig = b.vis ? b.html : '';
      const changed = animate && b.vis && el.dataset.sig !== sig;
      el.className = 'box ' + b.cls + (b.vis ? '' : ' off');
      Object.assign(el.style, pct(b.r));
      if (content.dataset.html !== b.html) {
        if (animate && wasOn && b.vis) {
          content.classList.add('fading');
          setTimeout(() => { content.innerHTML = content.dataset.html; content.classList.remove('fading'); }, 200);
        } else content.innerHTML = b.html;
        content.dataset.html = b.html;
      }
      el.dataset.on = b.vis ? '1' : '0';
      el.dataset.sig = sig;
      if (changed) {
        el.classList.add('changed');
        clearTimeout(el._t);
        el._t = setTimeout(() => el.classList.remove('changed'), 1800);
      }
    }
    for (const [id, el] of Object.entries(els)) {
      if (!(id in B)) { el.classList.add('off'); el.dataset.on = '0'; el.dataset.sig = ''; }
    }
    for (const [id, a] of Object.entries(A)) {
      let g = arrowEls[id];
      if (!g) {
        g = arrowEls[id] = document.createElementNS(SVGNS, 'g');
        g.append(document.createElementNS(SVGNS, 'path'), document.createElementNS(SVGNS, 'text'));
        svg.append(g);
      }
      const [path, text] = g.children;
      g.setAttribute('class', (a.vis ? '' : 'off') + (a.cls ? ' ' + a.cls : ''));
      if (!a.pts) path.style.d = 'none';
      else {
        // pull both ends back a little so the heads touch the box edges
        const pts = a.pts.map(p => [...p]);
        const pull = (end, from, by) => {
          const dx = Math.sign(end[0] - from[0]), dy = Math.sign(end[1] - from[1]);
          end[0] -= dx * by; end[1] -= dy * by;
        };
        pull(pts[pts.length - 1], pts[pts.length - 2], 3);
        pull(pts[0], pts[1], a.both ? 3 : 1);
        path.style.d = `path("M${pts.map(p => p.join(',')).join(' L')}")`;
        path.setAttribute('marker-end', 'url(#head)');
        if (a.both) path.setAttribute('marker-start', 'url(#head)'); else path.removeAttribute('marker-start');
      }
      text.style.textAnchor = a.anchor ?? 'middle';
      text.style.transform = `translate(${a.at[0]}px, ${a.at[1]}px)`;
      text.textContent = a.label;
    }
    for (const [id, g] of Object.entries(arrowEls)) if (!(id in A)) g.classList.add('off');
  }

  // ---------- text panels ----------
  const esc = s => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  const highlight = src => esc(src).replace(/('[^'\n]*'|"[^"\n]*")|(#.*$)/gm,
    (m, s, c) => s ? `<span class="s">${s}</span>` : `<span class="c">${c}</span>`);
  const planned = p => p ? `<div class="planned">planned: ${p}</div>` : '';

  function caption(cfg, idx) {
    if (idx >= 0) {
      const s = slides[idx];
      return { eyebrow: `Step ${idx + 1} of ${slides.length}`, title: s.title, body: planned(s.planned) + s.body, code: s.code };
    }
    const c = offTour(cfg);
    return { eyebrow: 'Off the tour', title: c.title, body: planned(c.planned) + c.body, code: c.code };
  }

  function renderStrip() {
    $('tour').innerHTML = groups.map(([name, a, b]) => `<div class="tour-group"><span>${name}</span>` +
      slides.slice(a, b).map((s, k) => `<button class="chip" data-i="${a + k}" title="${esc(s.title)}" aria-current="${a + k === tour.current}">${a + k + 1}</button>`).join('') +
      '</div>').join('') +
      `<div class="nav"><button data-step="-1" ${tour.current === 0 ? 'disabled' : ''}>← Back</button><button data-step="1" ${tour.current === slides.length - 1 ? 'disabled' : ''}>Next →</button></div>`;
  }

  // ---------- navigation ----------
  let last = 0;  // the slide shown last, where Back and Next return to from off the tour
  const same = (a, b) => keys.every(k => a[k] === b[k]);
  const tour = {
    cfg: null, current: 0,
    slideAt: c => slides.findIndex(s => same(s, c)),
    show(cfg, idx = tour.slideAt(cfg), animate = true) {
      tour.cfg = cfg; tour.current = idx;
      if (idx >= 0) last = idx;
      render(cfg, animate);
      const c = caption(cfg, idx);
      $('caption').innerHTML = `<p class="eyebrow">${c.eyebrow}</p><h2>${c.title}</h2>${c.body}`;
      $('code').innerHTML = highlight(c.code);
      renderStrip();
      if (panel) $('panel').innerHTML = panel(cfg, tour);
      if (idx >= 0 && location.hash !== '#s' + (idx + 1)) { try { history.replaceState(null, '', '#s' + (idx + 1)); } catch (e) { /* the frame may refuse */ } }
    },
    go(i) { i = Math.max(0, Math.min(slides.length - 1, i)); tour.show({ ...slides[i] }, i); },
    step(d) { tour.go(tour.current < 0 ? last : tour.current + d); },
  };

  $('tour').addEventListener('click', e => {
    const t = e.target.closest('button');
    if (t?.dataset.i) tour.go(+t.dataset.i);
    else if (t?.dataset.step) tour.step(+t.dataset.step);
  });
  if (click) $('panel').addEventListener('click', e => { const t = e.target.closest('button'); if (t) click(t, tour); });
  document.addEventListener('keydown', e => {
    if (e.altKey || e.ctrlKey || e.metaKey) return;
    if (e.key === 'ArrowRight' || e.key === 'PageDown') { tour.step(1); e.preventDefault(); }
    if (e.key === 'ArrowLeft' || e.key === 'PageUp') { tour.step(-1); e.preventDefault(); }
  });
  const fromHash = () => { const m = /^#s(\d+)$/.exec(location.hash); return m ? Math.min(slides.length, Math.max(1, +m[1])) - 1 : 0; };
  window.addEventListener('hashchange', () => { const i = fromHash(); if (i !== tour.current) tour.go(i); });
  const start = fromHash();
  tour.show({ ...slides[start] }, start, false);
  return tour;
}

// helpers for layouts: a box's header, and edges of a rectangle [x, y, w, h]
Tour.h = (title, sub, extra = '') => `<div class="hdr">${title}</div>${sub ? `<div class="sub">${sub}</div>` : ''}${extra}`;
Tour.right = r => r[0] + r[2];
Tour.bottom = r => r[1] + r[3];
Tour.midY = r => r[1] + r[3] / 2;
