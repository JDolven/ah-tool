// API client. Uses mock data when the page URL has ?mock=1.

export const MOCK = new URLSearchParams(location.search).get('mock') === '1';

let mockModule = null;
async function mockCall(method, path, body) {
  if (!mockModule) mockModule = await import('../mock.js');
  await new Promise((r) => setTimeout(r, 150 + Math.random() * 250));
  // Round-trip through JSON so pages never share object identity with mock state.
  const res = mockModule.mockApi(method, path, body === undefined ? undefined : JSON.parse(JSON.stringify(body)));
  return res === undefined ? null : JSON.parse(JSON.stringify(res));
}

const cache = new Map(); // path -> { t, promise }
const TTL = 20000;

export function clearCache() {
  cache.clear();
}

// ---------------------------------------------------------------- chosen realm
// The realm picked in the sidebar is remembered in this browser and sent with every call (?realm=...). Empty means
// the server's default (the realm with the newest data; on the web version the owner's).
const REALM_KEY = 'ahtool-realm';

export function getRealm() {
  try {
    return localStorage.getItem(REALM_KEY) || '';
  } catch (e) {
    return '';
  }
}

export function setRealm(realm) {
  try {
    if (realm) localStorage.setItem(REALM_KEY, realm);
    else localStorage.removeItem(REALM_KEY);
  } catch (e) {
    /* storage disabled: the choice lasts until the page is closed */
    memRealm = realm || '';
  }
  clearCache();
}

let memRealm = '';

function withRealm(path) {
  const realm = getRealm() || memRealm;
  if (!realm || !path.startsWith('/api/') || path.startsWith('/api/sync') || /[?&]realm=/.test(path)) return path;
  return `${path}${path.includes('?') ? '&' : '?'}realm=${encodeURIComponent(realm)}`;
}

// ---------------------------------------------------------------- web version
// The shared web version keeps nothing personal on the server: professions, known recipes, shopping list, watchlist,
// settings and alerts live here in localStorage and are sent along with every call (POST /api/x). The server answers
// with the new state after a change, and we store it. See ahtool/webstate.py.
export const WEB = !MOCK && document.documentElement.dataset.mode === 'web';
const STATE_KEY = 'ahtool-state-v1';

export function loadState() {
  try {
    const s = JSON.parse(localStorage.getItem(STATE_KEY) || 'null');
    return s && typeof s === 'object' ? s : {};
  } catch (e) {
    return {};
  }
}

export function saveState(s) {
  try {
    localStorage.setItem(STATE_KEY, JSON.stringify(s));
  } catch (e) {
    /* storage full or disabled: the change lasts until the page is closed */
  }
}

/** Replace the whole saved setup (import). Validation happens on the server at the next call. */
export function replaceState(s) {
  const keep = loadState().eval;
  saveState({ ...s, eval: keep });
  clearCache();
}

/** A GET only changes state when it evaluated alerts (status poll). Merge just that, so a poll that was in flight
 *  during a click never undoes the click. */
function mergeAlerts(s) {
  const cur = loadState();
  cur.alerts = s.alerts;
  cur.alertstate = s.alertstate;
  cur.eval = s.eval;
  for (const [id, w] of Object.entries(s.watch || {})) {
    const c = cur.watch && cur.watch[id];
    if (!c) continue;
    for (const k of ['last_alert_price', 'last_alert_ts']) {
      if (w[k] === undefined) delete c[k];
      else c[k] = w[k];
    }
  }
  saveState(cur);
}

let writeQueue = Promise.resolve();

function tunnel(method, path, body) {
  if (method === 'GET') return tunnelOnce(method, path, body);
  // Writes run one at a time: each one must start from the state the previous one produced.
  const run = writeQueue.then(() => tunnelOnce(method, path, body));
  writeQueue = run.catch(() => {});
  return run;
}

async function tunnelOnce(method, path, body) {
  const res = await fetch('/api/x', {
    method: 'POST',
    headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
    body: JSON.stringify({ m: method, p: path, b: body === undefined ? null : body, s: loadState() }),
  });
  let data = null;
  try {
    data = await res.json();
  } catch (e) {
    data = null;
  }
  if (!res.ok) {
    const err = new Error(data && data.error ? data.error : `HTTP ${res.status}`);
    err.status = res.status;
    throw err;
  }
  if (data && data.s) {
    if (method === 'GET') mergeAlerts(data.s);
    else saveState(data.s);
  }
  return data ? data.r : null;
}

async function request(method, path, body) {
  path = withRealm(path);
  if (MOCK) return mockCall(method, path, body);
  if (WEB) return tunnel(method, path, body);
  const opts = { method, headers: { Accept: 'application/json' } };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  let data = null;
  try {
    data = await res.json();
  } catch (e) {
    data = null;
  }
  if (!res.ok) {
    const msg = data && data.error ? data.error : `HTTP ${res.status}`;
    const err = new Error(msg);
    err.status = res.status;
    throw err;
  }
  return data;
}

export function get(path, { fresh = false } = {}) {
  const now = Date.now();
  const hit = cache.get(path);
  if (!fresh && hit && now - hit.t < TTL) return hit.promise;
  const promise = request('GET', path).catch((err) => {
    cache.delete(path);
    throw err;
  });
  cache.set(path, { t: now, promise });
  return promise;
}

/** Any write invalidates every cached GET – user state feeds many views. */
async function write(method, path, body) {
  try {
    return await request(method, path, body);
  } finally {
    clearCache();
  }
}

export function post(path, body) {
  return write('POST', path, body);
}

export function put(path, body) {
  return write('PUT', path, body ?? {});
}

export function del(path) {
  return write('DELETE', path);
}

const enc = encodeURIComponent;

export const api = {
  status: (fresh) => get('/api/status', { fresh }),
  ingest: () => post('/api/ingest'),
  search: (q, limit = 12) => get(`/api/items/search?q=${enc(q)}&limit=${limit}`),
  item: (id) => get(`/api/item/${enc(id)}`),
  tooltip: (id) => get(`/api/tooltip/${enc(id)}`, { fresh: true }),
  professions: () => get('/api/professions'),
  profession: (id, mode = 'current', mine = false) =>
    get(`/api/professions/${enc(id)}?mode=${enc(mode)}${mine ? '&mine=1' : ''}`),
  gather: (id) => get(`/api/professions/${enc(id)}/gather`),
  deals: (threshold = 0.25, minValue = 0, reason = '', includeLow = false) =>
    get(`/api/deals?threshold=${threshold}&min_value=${Math.round(minValue)}&reason=${enc(reason)}${includeLow ? '&include_low=1' : ''}`),
  market: () => get('/api/stats/market'),
  postings: () => get('/api/postings'),

  // v2 – user state
  myProfessions: () => get('/api/myprofessions'),
  setMyProfessions: (list) => put('/api/myprofessions', list),
  setKnown: (spellId, known) => put(`/api/known/${enc(spellId)}`, { known }),
  myCrafts: (mode = 'current', limit = 50) => get(`/api/mycrafts?mode=${enc(mode)}&limit=${limit}`),
  shopping: (fresh) => get('/api/shopping', { fresh }),
  setShopping: (spellId, qty) => put(`/api/shopping/${enc(spellId)}`, { qty }),
  clearShopping: () => del('/api/shopping'),
  watchlist: () => get('/api/watchlist'),
  setWatch: (itemId, body) => put(`/api/watchlist/${enc(itemId)}`, body),
  removeWatch: (itemId) => del(`/api/watchlist/${enc(itemId)}`),
  alerts: (limit = 50, fresh = false) => get(`/api/alerts?limit=${limit}`, { fresh }),
  markAlertsSeen: () => post('/api/alerts/seen'),
  settings: () => get('/api/settings'),
  setSettings: (partial) => put('/api/settings', partial),
  notifyTest: () => post('/api/notify/test'),

  // v4 – profit trend, trending crafts, leveling planner
  recipeTrend: (spellId, days = 30) => get(`/api/recipe/${enc(spellId)}/trend?days=${days}`),
  trendingCrafts: (limit = 8, mine = false, includeLow = false) =>
    get(`/api/trending-crafts?limit=${limit}&mine=${mine ? 1 : 0}${includeLow ? '&include_low=1' : ''}`),
  /** opts: {start,target,mode,recover,sources,known_only} – serialised as the query string. */
  leveling: (profId, opts = {}) => get(`/api/leveling/${enc(profId)}?${levelQuery(opts)}`),
  // v5 – AH Tool Logger (full scans, sales, own auctions)
  itemMarket: (id, qty) => get(`/api/item/${enc(id)}/market${qty ? `?qty=${qty}` : ''}`),
  activity: (days = 7) => get(`/api/market/activity?days=${days}`),
  mySales: (days = 30) => get(`/api/market/my?days=${days}`),
  report: (date = '') => get(`/api/market/report${date ? `?date=${enc(date)}` : ''}`),
  levelingToShopping: (profId, opts = {}) => post(`/api/leveling/${enc(profId)}/to-shopping?${levelQuery(opts)}`),
};

export function levelQuery(o) {
  const q = new URLSearchParams();
  for (const k of ['start', 'target', 'mode', 'recover', 'sources']) if (o[k] !== undefined && o[k] !== null && o[k] !== '') q.set(k, String(o[k]));
  q.set('known_only', o.known_only ? '1' : '0');
  return q.toString();
}
