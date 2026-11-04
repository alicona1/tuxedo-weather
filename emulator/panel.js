// Draws the panel's home and weather screens from the firmware's own images,
// at the positions its constructors use. Each item says where its position
// came from: 'fw' is read from the disassembly, 'est' is estimated from the
// photos of the real panel. Without the images (they come from the firmware,
// which isn't in the repo), the same layout is drawn as plain boxes.
import * as fw from './firmware.js';

// What serve.py can do: which server it fetches from, whether the page may
// pick another one, and whether the firmware images were extracted.
const config = await fetch('config').then((r) => r.json())
  .catch(() => ({ upstream: '', override: false, assets: true }));
const IMAGES = config.assets;

const asset = (dir, file) => encodeURI(`assets/${dir}/LoungeLizard/${dir}/${file}`);

// Qt Embedded only has bitmap Helvetica at these pixel sizes (lib/fonts/
// helvetica_*.qpf), so a QFont point size snaps to the nearest one.
const QPF_SIZES = [8, 10, 12, 14, 18, 24];
const px = (pt) => QPF_SIZES.reduce((a, b) => (Math.abs(b - pt) <= Math.abs(a - pt) ? b : a));
// QFont(family, size, weight): 50 is normal, 75 is bold.
const font = (pt, weight = 50) => ({ size: px(pt), bold: weight >= 75 });
// CustomTextItems that never get setFont(). Estimated from the photos.
const DEFAULT_FONT = { size: 14, bold: true };

const panel = document.getElementById('panel');
const shown = {};          // what the screens draw, updated on sigNotifyUpdateWeather
const state = {};          // the parsers' globals
const items = {};          // id -> element, for updates

function add(parent, spec) {
  const { kind = 'text', x, y, src } = spec;
  const asImage = kind === 'img' && (IMAGES || spec.icon150);
  const el = document.createElement(kind === 'img' && IMAGES ? 'img' : 'div');
  el.className = 'item';
  el.style.left = `${x}px`;
  el.style.top = `${y}px`;
  el.dataset.src = src;
  if (kind === 'img' && !IMAGES) {
    // Weather icons become a 150x120 box naming the image; other images
    // become an outline if they have a size (spec.box), or nothing.
    const [w, h] = spec.icon150 ? [150, 120] : spec.box || [0, 0];
    if (!w) el.classList.add('hidden');
    el.classList.add('box');
    if (spec.icon150) el.classList.add('icon', 'hidden');
    if (spec.round) el.classList.add('round');
    Object.assign(el.style, { width: `${w}px`, height: `${h}px` });
  } else if (asImage) {
    el.alt = '';
    if (spec.file) el.src = spec.file;
    else el.classList.add('hidden');
    if (spec.w) el.style.width = `${spec.w}px`;
    if (spec.h) el.style.height = `${spec.h}px`;
  } else if (kind === 'button' && !IMAGES) {
    el.classList.add('box', 'button', 'hit');
    Object.assign(el.style, { width: '65px', height: '65px' });
    el.textContent = spec.label || '';
  } else if (kind === 'button') {
    // CButtonIcon: icon centered on a 65x65 button face.
    Object.assign(el.style, {
      width: '65px', height: '65px', display: 'flex', alignItems: 'center',
      justifyContent: 'center', backgroundImage: `url("${spec.face}")`,
    });
    el.classList.add('hit');
    const icon = document.createElement('img');
    icon.src = spec.icon;
    icon.alt = spec.label || '';
    icon.style.maxWidth = '58px';
    icon.style.maxHeight = '58px';
    el.append(icon);
    if (spec.label) el.title = spec.label;
  } else {
    el.classList.add('text');
    const f = spec.font || DEFAULT_FONT;
    el.style.fontSize = `${f.size}px`;
    el.style.fontWeight = f.bold ? 'bold' : 'normal';
    // setTextWidth(): wraps at that width (it includes the 4px margins).
    if (spec.width) { el.style.width = `${spec.width}px`; el.classList.add('wrap'); }
    if (spec.className) el.classList.add(spec.className);
    el.textContent = spec.text ?? '';
  }
  if (spec.onClick) { el.classList.add('hit'); el.addEventListener('click', spec.onClick); }
  parent.append(el);
  if (spec.id) items[spec.id] = el;
  return el;
}

function group(parent, x, y, src, id) {
  const g = document.createElement('div');
  g.className = 'item';
  Object.assign(g.style, { left: `${x}px`, top: `${y}px` });
  g.dataset.src = src;
  parent.append(g);
  if (id) items[id] = g;
  return g;
}

function screen(name) {
  const s = document.createElement('div');
  s.className = 'item screen';
  Object.assign(s.style, { left: 0, top: 0, width: '800px', height: '480px' });
  s.dataset.screen = name;
  panel.append(s);
  if (IMAGES) add(s, { kind: 'img', x: 0, y: 0, file: asset('background', 'background.png'), src: 'fw' });
  return s;
}

// CStatusPrompt: status_<colour>.png with bold 20 text. Its y isn't traced.
function statusBar(s) {
  const bar = add(s, { kind: 'img', x: 0, y: 0, box: [800, 32], file: asset('status', 'status_green.png'), src: 'est' });
  if (!IMAGES) bar.classList.add('statusbox');
  add(s, { x: 0, y: 2, text: 'READY TO ARM', font: font(20, 75), className: 'status', src: 'est' });
}

const button = (s, x, y, dir, file, label, onClick, face = 'but_65x65.png') => add(s, {
  kind: 'button', x, y, src: 'fw', label, onClick,
  icon: asset(dir, file), face: asset('buttons', face),
});

// --- Home screen: CHomeScreen::CHomeScreen (0x61a08)

function buildHome() {
  const s = screen('home');
  statusBar(s);

  // CPieIcon petals and their icons.
  if (!IMAGES) add(s, { kind: 'img', x: 212, y: 52, box: [376, 376], round: true, src: 'est' });
  add(s, { kind: 'img', x: 204, y: 44, file: asset('pie', 'pie_3_1.png'), src: 'fw' });
  add(s, { kind: 'img', x: 395, y: 44, file: asset('pie', 'pie_3_2.png'), src: 'fw' });
  add(s, { kind: 'img', x: 233, y: 264, file: asset('pie', 'pie_3_3.png'), src: 'fw' });
  add(s, { kind: 'img', x: 245, y: 105, file: asset('home', 'p08_security.png'), src: 'fw' });
  add(s, { kind: 'img', x: 456, y: 105, file: asset('home', 'p09_automation.png'), src: 'fw' });
  add(s, { kind: 'img', x: 357, y: 299, file: asset('home', 'p12_multimedia.png'), src: 'fw' });
  add(s, { kind: 'img', x: 350, y: 190, w: 100, h: 100, file: asset('Voice', 'Inactive.png'), src: 'fw' });
  // CPieIcon computes its label position at run time; these follow the photo.
  const label = (text, cx, y) => {
    const el = add(s, { x: cx, y, text, font: font(16, 75), src: 'est' });
    el.style.transform = 'translateX(-50%)';
  };
  label('Security', 295, 190);
  label('Automation', 506, 190);
  label('Multimedia', 407, 392);

  // CHomeClock (0x508a4) at (0, 180).
  const clock = group(s, 0, 180, 'fw');
  add(clock, { kind: 'img', x: 0, y: 0, box: [159, 154], file: asset('background', 'background_home_time.png'), src: 'fw' });
  add(clock, { id: 'clockDay', x: 10, y: 10, font: font(27), src: 'fw' });
  add(clock, { id: 'clockDate', x: 10, y: 50, src: 'fw' });
  add(clock, { id: 'clockTime', x: 10, y: 90, font: font(27), src: 'fw' });

  // CHomeWeather (0x586a4) at (618, 76). Tapping it opens the weather screen.
  const tile = group(s, 618, 76, 'fw', 'homeWeather');
  tile.classList.add('hit');
  tile.addEventListener('click', () => show('weather'));
  add(tile, { kind: 'img', x: 0, y: 0, box: [159, 274], file: asset('background', 'background_home_weather.png'), src: 'fw' });
  add(tile, { id: 'homeIcon', kind: 'img', x: 5, y: 0, icon150: true, src: 'fw' });
  add(tile, { id: 'homeCity', x: 25, y: 110, width: 120, src: 'fw' });
  // "Condition N/A" sits at (25, 150); no response field feeds it.
  add(tile, { id: 'homeTemp', x: 25, y: 160, font: font(22, 75), src: 'fw' });
  add(tile, { id: 'homeHumidity', x: 25, y: 200, src: 'fw' });
  add(tile, { id: 'homeWind', x: 25, y: 225, width: 120, src: 'fw' });

  button(s, 8, 383, 'ButtonIcons', '05_setup.png', 'Setup');
  button(s, 81, 383, 'ButtonIcons', '09_photos.png', 'Photos');
  button(s, 154, 383, 'ButtonIcons', 'p13_Intro_video.png', 'Videos');
  button(s, 586, 383, 'ButtonIcons', '06_camera_rec.png', 'Cameras');
  button(s, 654, 383, 'ButtonIcons', '03_panic.png', 'Panic', null, 'but_65x65_panic.png');
  // CConnectionStatus (0x5c184) at (8, 452): a bar, the IP text, then status
  // icons every 32 px from x = 144 (Z-Wave, wired, Wi-Fi; then recording,
  // SD card and download, which stay hidden or blank here). The icon states
  // are the ones the panel showed on 2026-11-03: Z-Wave on, no cable, Wi-Fi
  // at four bars.
  const strip = group(s, 8, 452, 'fw');
  add(strip, { kind: 'img', x: 0, y: 0, box: [304, 24], file: asset('buttons', 'but_304x24.png'), src: 'fw' });
  add(strip, { x: 0, y: 0, text: 'IP:192.168.1.189', src: 'fw' });
  add(strip, { kind: 'img', x: 144, y: 0, file: asset('UI', 'zwave_on.png'), src: 'fw' });
  add(strip, { kind: 'img', x: 176, y: 0, file: asset('UI', 'connection_off.png'), src: 'fw' });
  add(strip, { kind: 'img', x: 208, y: 0, file: asset('UI', 'bars_4.png'), src: 'fw' });
}

// --- Weather screen: CWeatherCities::CWeatherCities (0x14fc40)

function buildWeather() {
  const s = screen('weather');
  add(s, { kind: 'img', x: 0, y: 0, file: asset('ScreenBackgrounds', '21  --  5-Day Weather Forecast.png'), src: 'fw' });
  if (!IMAGES) {
    // The cells of that background, measured from its alpha channel.
    for (const [x, w] of [[27, 291], [326, 146], [480, 146], [634, 146]]) {
      add(s, { kind: 'img', x, y: 97, box: [w, 256], src: 'fw' });
    }
  }
  statusBar(s);
  // CBaseGraphicsView (0x3fcc0): home and back at (8, 8) and (727, 8).
  button(s, 8, 8, 'ButtonIcons', '02_home.png', 'Home', () => show('home'));
  button(s, 727, 8, 'ButtonIcons', '01_back_arrow.png', 'Back', () => show('home'));

  // Centered: x = (800 - width) / 2, y = 40.
  const title = add(s, { x: 400, y: 40, text: 'Weather Forecast', font: font(50, 75), src: 'fw' });
  title.style.transform = 'translateX(-50%)';

  add(s, { x: 48, y: 100, text: 'Today', font: font(22, 75), src: 'fw' });
  add(s, { id: 'wIcon', kind: 'img', x: 32, y: 125, icon150: true, src: 'fw' });
  add(s, { id: 'wTemp', x: 182, y: 125, width: 150, font: font(22, 75), text: 'Temp N/A', src: 'fw' });
  add(s, { id: 'wHumidity', x: 182, y: 165, width: 120, font: font(20, 75), text: 'Humidity N/A', src: 'fw' });
  add(s, { id: 'wWind', x: 182, y: 215, width: 120, font: font(20, 75), text: 'Wind Cond N/A', src: 'fw' });
  add(s, { id: 'wHigh', x: 48, y: 260, font: font(20, 75), text: 'N/A', src: 'fw' });
  add(s, { id: 'wLow', x: 48, y: 300, font: font(20, 75), text: 'N/A', src: 'fw' });
  add(s, { id: 'wSunrise', x: 182, y: 260, text: 'Sunrise: N/A', src: 'fw' });
  add(s, { id: 'wSunset', x: 182, y: 300, text: 'Sunset: N/A', src: 'fw' });

  // Three day columns: x starts at 326 and steps by 153 (0x15063c).
  for (let i = 0; i < 3; i++) {
    const x = 326 + 153 * i;
    add(s, { id: `d${i}Name`, x: x + 24, y: 100, font: font(20, 75), text: 'N/A', src: 'fw' });
    add(s, { id: `d${i}Icon`, kind: 'img', x: x - 2, y: 125, icon150: true, src: 'fw' });
    add(s, { id: `d${i}High`, x, y: 260, font: font(20, 75), text: 'N/A', src: 'fw' });
    add(s, { id: `d${i}Low`, x, y: 300, font: font(20, 75), text: 'N/A', src: 'fw' });
  }

  button(s, 8, 407, 'ButtonIcons', '69-refreshdata_t.png', 'Refresh', () => fetchWeather());
  add(s, { x: 80, y: 450, width: 450, font: font(14), text: 'Weather forecast provided by Accuweather', src: 'fw' });
  add(s, { x: 73, y: 375, font: font(20), text: 'Region', src: 'fw' });
  add(s, { x: 145, y: 370, text: 'USA', className: 'field', src: 'fw' }).style.width = '117px';
  add(s, { x: 271, y: 375, font: font(20), text: 'ZIP Code', src: 'fw' });
  const zip = add(s, { id: 'wZip', x: 381, y: 370, className: 'field', src: 'est' });
  zip.style.width = '80px';
  add(s, { x: 505, y: 375, font: font(20), text: 'City', src: 'fw' });
  const city = add(s, { id: 'wCity', x: 550, y: 370, className: 'field', src: 'est' });
  city.style.width = '232px';
  add(s, { id: 'wStatus', x: 519, y: 427, text: 'Last Update: 00/00/0000', src: 'fw' });
}

function setIcon(el, file) {
  el.classList.toggle('hidden', !file);
  if (!file) return;
  if (IMAGES) el.src = asset('Weather', file);
  else el.textContent = file.replace(/\.png$/, '');
}

// sigNotifyUpdateWeather -> RefreshScreen() and RefreshHomeWeather().
function render() {
  const w = fw.weatherScreenText(shown);
  items.wTemp.textContent = w.temp;
  items.wHumidity.textContent = w.humidity;
  items.wWind.textContent = w.wind;
  items.wHigh.textContent = w.high;
  items.wLow.textContent = w.low;
  items.wSunrise.textContent = w.sunrise;
  items.wSunset.textContent = w.sunset;
  setIcon(items.wIcon, w.icon);
  w.days.forEach((d, i) => {
    items[`d${i}Name`].textContent = d.name;
    items[`d${i}High`].textContent = d.high;
    items[`d${i}Low`].textContent = d.low;
    setIcon(items[`d${i}Icon`], d.icon);
  });
  items.wCity.textContent = shown.city ?? '';
  items.wStatus.textContent = w.lastUpdate;

  const h = fw.homeTileText(shown);
  items.homeCity.textContent = h.city;
  items.homeTemp.textContent = h.temp;
  items.homeHumidity.textContent = h.humidity;
  items.homeWind.textContent = h.wind;
  setIcon(items.homeIcon, h.icon);
}

function tick() {
  const now = new Date();
  const two = (n) => String(n).padStart(2, '0');
  const hour = now.getHours() % 12 || 12;
  items.clockDay.textContent = now.toLocaleDateString('en-US', { weekday: 'long' });
  items.clockDate.textContent = `${two(now.getMonth() + 1)}/${two(now.getDate())}/${now.getFullYear()}`;
  items.clockTime.textContent = `${two(hour)}:${two(now.getMinutes())} ${now.getHours() < 12 ? 'AM' : 'PM'}`;
}

function show(name) {
  for (const s of panel.querySelectorAll('[data-screen]')) s.classList.toggle('hidden', s.dataset.screen !== name);
  document.getElementById('screen').textContent = name === 'home' ? 'Show weather screen' : 'Show home screen';
  panel.dataset.current = name;
}

// --- Fetching, as the panel does at boot: cur, for, then sun.

const { PARSERS } = fw;

function log(endpoint, zipcode, raw, result, error) {
  const d = document.createElement('details');
  const summary = document.createElement('summary');
  let verdict;
  if (error) {
    d.className = 'bad';
    verdict = error;
  } else if (result.check !== 0) {
    d.className = 'bad';
    verdict = `http_check returned ${result.check}`;
  } else if (result.parser !== endpoint) {
    d.className = 'bad';
    verdict = result.parser === 'none' ? 'no parser matched' : `went to ${PARSERS[result.parser]} instead`;
  } else {
    d.className = result.notes.length ? 'warn' : 'ok';
    verdict = `${PARSERS[endpoint]}${result.notes.length ? `, ${result.notes.length} problem(s)` : ', clean'}`;
  }
  summary.textContent = `${new Date().toLocaleTimeString()}  /us/${endpoint}/${zipcode}.xml: ${verdict}`;
  d.append(summary);
  if (result?.notes.length) {
    const ul = document.createElement('ul');
    for (const n of result.notes) {
      const li = document.createElement('li');
      li.textContent = n;
      ul.append(li);
    }
    d.append(ul);
  }
  if (raw !== null) {
    const pre = document.createElement('pre');
    pre.textContent = `${raw.length} bytes in the first recv()\n\n${raw}`;
    d.append(pre);
  }
  if (d.className !== 'ok') d.open = true;
  document.getElementById('log').prepend(d);
}

let busy = false;

function showVerdict(results, server) {
  const v = fw.verdict(results);
  const el = document.getElementById('verdict');
  el.className = v.level;
  el.textContent = `${server}: ${v.text}`;
}

async function fetchWeather() {
  if (busy) return;
  busy = true;
  const zipcode = document.getElementById('zip').value.trim();
  const server = document.getElementById('server').value.trim() || config.upstream;
  const pick = config.override && server !== config.upstream ? `?upstream=${encodeURIComponent(server)}` : '';
  items.wZip.textContent = zipcode;
  items.wStatus.textContent = 'Requesting weather data';
  const results = [];
  try {
    for (const endpoint of ['cur', 'for', 'sun']) {
      let raw = null;
      try {
        const resp = await fetch(`panel/${endpoint}/${zipcode}${pick}`, { cache: 'no-store' });
        const bytes = new Uint8Array(await resp.arrayBuffer());
        // QString::fromAscii on the recv() buffer: one byte per character.
        const text = Array.from(bytes, (b) => String.fromCharCode(b)).join('');
        if (!resp.ok) throw new Error(text || `${resp.status}`);
        raw = text;
      } catch (e) {
        log(endpoint, zipcode, null, null, e.message);
        results.push({ endpoint, error: e.message });
        continue;
      }
      const result = fw.receive(raw, state);
      log(endpoint, zipcode, raw, result);
      results.push({ endpoint, raw, ...result });
      // Only parse_sunncs_data emits sigNotifyUpdateWeather.
      if (result.check === 0 && result.parser === 'sun') {
        Object.assign(shown, structuredClone(state));
        render();
      }
    }
    showVerdict(results, server);
  } finally {
    busy = false;
    if (items.wStatus.textContent === 'Requesting weather data') {
      items.wStatus.textContent = fw.weatherScreenText(shown).lastUpdate;
    }
  }
}

// --- Page wiring

function fit() {
  const frame = document.getElementById('frame');
  const inner = frame.clientWidth - 48;
  const scale = Math.min(1, inner / 800);
  panel.style.transform = `scale(${scale})`;
  frame.style.height = `${480 * scale + 48}px`;
}

if (!IMAGES) panel.classList.add('noimages');
const serverField = document.getElementById('server');
serverField.value = config.upstream;
serverField.readOnly = !config.override;
serverField.title = config.override
  ? 'HOST or HOST:PORT of the weather server to test'
  : 'Fixed by --upstream while the emulator is open to the network';
serverField.addEventListener('keydown', (e) => { if (e.key === 'Enter') fetchWeather(); });

buildHome();
buildWeather();
// #weather opens the weather screen, #sources turns the outlines on.
const hash = location.hash.slice(1).split(',');
show(hash.includes('weather') ? 'weather' : 'home');
if (hash.includes('sources')) {
  panel.classList.add('sources');
  document.getElementById('sources').checked = true;
}
tick();
setInterval(tick, 1000);
fit();
window.addEventListener('resize', fit);

document.getElementById('boot').addEventListener('click', () => fetchWeather());
document.getElementById('screen').addEventListener('click', () => show(panel.dataset.current === 'home' ? 'weather' : 'home'));
document.getElementById('sources').addEventListener('change', (e) => panel.classList.toggle('sources', e.target.checked));
document.getElementById('zip').addEventListener('keydown', (e) => { if (e.key === 'Enter') fetchWeather(); });
fetchWeather();
