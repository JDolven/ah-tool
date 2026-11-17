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

async function request(method, path, body) {
  if (MOCK) return mockCall(method, path, body);
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
  levelingToShopping: (profId, opts = {}) => post(`/api/leveling/${enc(profId)}/to-shopping?${levelQuery(opts)}`),
};

export function levelQuery(o) {
  const q = new URLSearchParams();
  for (const k of ['start', 'target', 'mode', 'recover', 'sources']) if (o[k] !== undefined && o[k] !== null && o[k] !== '') q.set(k, String(o[k]));
  q.set('known_only', o.known_only ? '1' : '0');
  return q.toString();
}
