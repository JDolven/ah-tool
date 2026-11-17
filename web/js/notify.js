// Browser Notification API wrapper. Permission is asked only from a button click.
// The on/off preference is a per-browser convenience (localStorage); alert state itself lives in the backend.
const KEY = 'ahtool-browser-notify';

export function supported() {
  return typeof window !== 'undefined' && 'Notification' in window;
}

function pref() {
  try {
    return localStorage.getItem(KEY) !== 'off';
  } catch (e) {
    return true;
  }
}

function setPref(on) {
  try {
    localStorage.setItem(KEY, on ? 'on' : 'off');
  } catch (e) {
    /* ignore */
  }
}

/** 'unsupported' | 'denied' | 'default' (not asked) | 'off' (granted but disabled here) | 'on' */
export function state() {
  if (!supported()) return 'unsupported';
  const p = Notification.permission;
  if (p === 'denied') return 'denied';
  if (p !== 'granted') return 'default';
  return pref() ? 'on' : 'off';
}

export async function enable() {
  if (!supported()) return 'unsupported';
  let p = Notification.permission;
  if (p === 'default') {
    try {
      p = await Notification.requestPermission();
    } catch (e) {
      p = Notification.permission;
    }
  }
  if (p === 'granted') setPref(true);
  return state();
}

export function disable() {
  setPref(false);
  return state();
}

export function describe(s = state()) {
  return {
    unsupported: 'This browser does not support notifications.',
    denied: 'Blocked in the browser – allow notifications for this site in the address bar settings.',
    default: 'Off – click to allow notifications while this page is open.',
    off: 'Allowed, but turned off for this browser.',
    on: 'On – you get a desktop notification while this page is open.',
  }[s];
}

/** Show one notification summarising new alerts. Returns true if shown. */
export function show(alerts) {
  if (state() !== 'on' || !alerts || !alerts.length) return false;
  const first = alerts[0];
  const title = alerts.length === 1 ? 'AH Tool alert' : `AH Tool – ${alerts.length} new alerts`;
  const lines = alerts.slice(0, 4).map((a) => a.message || a.name || `Item #${a.item_id}`);
  if (alerts.length > 4) lines.push(`+${alerts.length - 4} more`);
  try {
    const n = new Notification(title, { body: lines.join('\n'), tag: `ahtool-alert-${first.id}`, icon: document.querySelector('link[rel=icon]')?.href });
    n.onclick = () => {
      window.focus();
      location.hash = alerts.length === 1 && first.item_id ? `#/item/${first.item_id}` : '#/watchlist';
      n.close();
    };
    return true;
  } catch (e) {
    return false;
  }
}

export function test() {
  return show([{ id: 'test', message: 'Test notification – browser notifications work.', item_id: null }]);
}
