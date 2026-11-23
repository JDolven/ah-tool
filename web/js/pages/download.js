// Run it yourself (web version only): the Windows package, the AH Tool Logger addon and the source code.
// The server lists what it has in /download/manifest.json (built with the Docker image, ahtool/downloads.py).
import { esc, num, errorState, skeletonRows } from '../util.js';

const SOURCE_URL = 'https://github.com/JDolven/ah-tool';

function size(bytes) {
  return bytes >= 1048576 ? `${num(bytes / 1048576, 1)} MB` : `${num(Math.max(1, Math.round(bytes / 1024)))} kB`;
}

function fileCard(f, text) {
  return `<section class="card dl-card">
    <div class="card-head"><h2>${esc(f.title)}</h2><span class="card-note">${esc(size(f.size))}</span></div>
    <p class="dl-text">${text}</p>
    <a class="btn btn-gold" href="/download/${encodeURIComponent(f.name)}" download>Download ${esc(f.name)}</a>
    <p class="fine dl-hash">SHA-256 <code>${esc(f.sha256)}</code></p>
  </section>`;
}

export async function render(ctx) {
  const { el } = ctx;
  el.innerHTML = `
    <header class="page-head"><div><h1>Run it yourself</h1><p class="lead">Run AH Tool on your own PC: your own scans, your sales and auctions, and Windows alerts. Free software under the AGPL-3.0.</p></div></header>
    <div id="dl-body"><section class="card">${skeletonRows(4, 2)}</section></div>`;

  let m;
  try {
    const r = await fetch('/download/manifest.json', { headers: { Accept: 'application/json' } });
    if (r.status === 404) m = null;
    else if (!r.ok) throw new Error(`HTTP ${r.status}`);
    else m = await r.json();
  } catch (err) {
    if (ctx.alive()) el.querySelector('#dl-body').innerHTML = errorState(err);
    return;
  }
  if (!ctx.alive()) return;

  const files = Object.fromEntries(((m && m.files) || []).map((f) => [f.name, f]));
  const win = files['ahtool-windows.zip'];
  const addon = files['ahtool-logger-addon.zip'];
  const py = m && m.python;
  const site = esc(location.origin);

  const cards = [];
  if (win) {
    cards.push(fileCard(win, py
      ? `The app with Python ${esc(py.version)} included – nothing to install. Unzip anywhere and double-click <code>start.bat</code>.`
      : 'The app. Needs <a href="https://www.python.org/downloads/windows/" rel="noopener" target="_blank">Python 3.12 or newer</a> installed. Unzip anywhere and double-click <code>start.bat</code>.'));
  }
  if (addon) {
    cards.push(fileCard(addon, 'Records full Auction House scans for market activity, listings and sales. Unzip into <code>World of Warcraft\\_forever_\\Interface\\AddOns</code>. Also included in the Windows package.'));
  }
  cards.push(`<section class="card dl-card">
    <div class="card-head"><h2>Source code</h2><span class="card-note">AGPL-3.0</span></div>
    <p class="dl-text">Everything the site and the downloads are built from. Python standard library only.</p>
    <a class="btn" href="${SOURCE_URL}" rel="noopener" target="_blank">Open on GitHub</a>
  </section>`);

  el.querySelector('#dl-body').innerHTML = `
    ${m ? '' : '<section class="card"><p class="card-note">This site has no packages to download – get the app from the source code below.</p></section>'}
    <div class="dl-grid">${cards.join('')}</div>
    <section class="card">
      <div class="card-head"><h2>Getting started</h2></div>
      <ol class="dl-steps">
        <li>In WoW, install <a href="https://www.curseforge.com/wow/addons/auctionator" rel="noopener" target="_blank">Auctionator</a> and <a href="https://www.curseforge.com/wow/addons/atlaslootclassic" rel="noopener" target="_blank">AtlasLootClassic</a> (recipes), plus the AH Tool Logger addon. Log in once.</li>
        <li>Unzip <code>ahtool-windows.zip</code> to a folder of your own, e.g. <code>Documents\\AHTool</code> (not Program Files). If Windows warns about the download, right-click the zip → <em>Properties</em> → <em>Unblock</em> before unzipping.</li>
        <li>Double-click <code>start.bat</code>. The first start looks for your WoW folder and writes <code>config.json</code>; if it cannot find it, Notepad opens so you can set the two paths. Then your browser opens AH Tool.</li>
        <li>Scan at the Auction House and type <code>/reload</code> – the app picks up the new data by itself.</li>
      </ol>
    </section>
    <section class="card">
      <div class="card-head"><h2>Share prices with this site</h2><span class="card-note">optional</span></div>
      <p class="dl-text">Ask the site owner for a key, then add to <code>config.json</code> and restart <code>start.bat</code>:</p>
      <pre class="dl-pre">"sync_url": "${site}",
"sync_token": "&lt;your key&gt;"</pre>
      <p class="fine">Only market data is sent: never your sales, purchases, mail, own auctions or settings, and your characters' names are removed. See the README for details.</p>
    </section>`;
}
