// node --test emulator/
import assert from 'node:assert/strict';
import { test } from 'node:test';

import * as fw from './firmware.js';

const ok = (xml) => `HTTP/1.1 200 OK\r\nContent-Type: text/xml\r\nContent-Length: ${xml.length}\r\n\r\n${xml}`;
const CUR = '<current><location>Chicago, IL</location><temp>75</temp><rhumid>68</rhumid>'
  + '<wind_dir>NE</wind_dir><windspeed>5</windspeed><icon>1</icon></current>';
const FOR = '<forecast>' + ['Monday', 'Tuesday', 'Wednesday', 'Thursday'].map((n, i) =>
  `<day><name>${n}</name><high>${80 + i}</high><low>${60 + i}</low><icon>${i + 1}</icon></day>`).join('') + '</forecast>';
const SUN = '<day><date>Oct. 22, 2026 12:33 PM</date><sunrise>7:27 AM</sunrise><sunset>6:43 PM</sunset><city>-</city></day>';

test('http_check wants the literal 200 line and rejects any 404', () => {
  assert.equal(fw.httpCheck('HTTP/1.1 200 OK\r\n'), 0);
  assert.equal(fw.httpCheck('HTTP/1.0 200 OK\r\n'), -405);
  assert.equal(fw.httpCheck('HTTP/1.1 503 SERVICE UNAVAILABLE\r\n'), -405);
  assert.equal(fw.httpCheck('HTTP/1.1 200 OK\r\n\r\nHTTP/1.1 404'), -404);
});

test('dispatch is a case-sensitive substring search, first match wins', () => {
  assert.equal(fw.dispatch(CUR), 'cur');
  assert.equal(fw.dispatch(FOR), 'for');
  assert.equal(fw.dispatch(SUN), 'sun');
  assert.equal(fw.dispatch(SUN.replace('<city>-</city>', '')), 'none');
  assert.equal(fw.dispatch(SUN.replace('-</city>', 'undercurrent</city>')), 'cur');
});

test('a full boot sequence fills the state', () => {
  const state = {};
  for (const xml of [CUR, FOR, SUN]) assert.deepEqual(fw.receive(ok(xml), state).notes, []);
  assert.equal(state.city, 'Chicago, IL');
  assert.equal(state.temp, '75');
  assert.equal(state.days.length, 4);
  assert.equal(state.days[3].name, 'Thursday');
  assert.equal(state.sunset, '6:43 PM');
  assert.equal(state.date, 'Oct. 22, 2026 12:33 PM');
});

test('an unhandled element drops everything after it', () => {
  const state = {};
  const notes = fw.parseCurrent(CUR.replace('<temp>', '<city>x</city><temp>'), state);
  assert.equal(state.city, 'Chicago, IL');
  assert.equal(state.temp, undefined);
  assert.match(notes[0], /<city> is not handled/);
});

test('city before the sun fields means none are read', () => {
  const state = {};
  fw.parseSun('<day><city>-</city><date>d</date><sunrise>7:27 AM</sunrise></day>', state);
  assert.equal(state.sunrise, undefined);
});

test('24-hour sun times are flagged the way the panel misreads them', () => {
  const notes = fw.parseSun(SUN.replace('6:43 PM', '18:43'), {});
  assert.match(notes[0], /sunset "18:43" reads as 30:00/);
});

test('a response cut off after the headers reaches no parser', () => {
  const result = fw.receive('HTTP/1.1 200 OK\r\nContent-Length: 200\r\n\r\n', {});
  assert.equal(result.parser, 'none');
});

test('a failed http_check leaves the state alone', () => {
  const state = { temp: '70' };
  fw.receive('HTTP/1.1 503 SERVICE UNAVAILABLE\r\n\r\nweather unavailable', state);
  assert.deepEqual(state, { temp: '70' });
});

test('icon codes use the firmware table', () => {
  assert.equal(fw.iconFile('2'), '2_mostly_sunny.png');
  assert.equal(fw.iconFile('9'), null);
  assert.equal(fw.iconFile('x'), null);        // toInt fails -> 0 -> NULL
  assert.equal(fw.iconFile('45'), '7_cloudy.png');
  assert.equal(fw.iconFile('-1'), '7_cloudy.png');
});

test('screen text matches RefreshScreen and RefreshHomeWeather', () => {
  const state = {};
  for (const xml of [CUR, FOR, SUN]) fw.receive(ok(xml), state);
  const w = fw.weatherScreenText(state);
  assert.equal(w.temp, '75°F');
  assert.equal(w.humidity, 'Humidity: 68%');
  assert.equal(w.high, '80°F       High');
  assert.equal(w.days[0].name, 'Tuesday');
  assert.equal(w.sunset, 'Sunset:  6:43 PM');
  const h = fw.homeTileText(state);
  assert.equal(h.humidity, 'Humidity 68%');
  assert.equal(h.wind, 'NE at 5 mph');
  assert.equal(w.wind, 'NE at 5 mph');
});

test('the wind line only changes once <windspeed> is read', () => {
  const state = { wind: 'S at 3 mph' };
  fw.parseCurrent(CUR.replace('<windspeed>5</windspeed>', ''), state);
  assert.equal(state.wind, 'S at 3 mph');
  // The local the line is built in starts empty on every call (0x4cf70).
  fw.parseCurrent(CUR.replace('<wind_dir>NE</wind_dir>', ''), state);
  assert.equal(state.wind, '5 mph');
});

test('an empty panel shows the firmware placeholders', () => {
  const w = fw.weatherScreenText({});
  assert.equal(w.temp, 'Temp N/A');
  assert.equal(w.high, '°F       High');
  assert.equal(w.days[2].name, 'N/A');
});

// --- The one-line verdict for a boot (cur, for, sun)

const boot = (overrides = {}) => ['cur', 'for', 'sun'].map((endpoint) => {
  const xml = { cur: CUR, for: FOR, sun: SUN }[endpoint];
  const raw = overrides[endpoint] ?? ok(xml);
  return { endpoint, raw, ...fw.receive(raw, {}) };
});

test('a clean boot says the panel would show it', () => {
  const v = fw.verdict(boot());
  assert.equal(v.level, 'ok');
  assert.match(v.text, /accept all three/);
});

test('a missing sun response means the screen never updates', () => {
  const v = fw.verdict(boot({ sun: ok(SUN.replace('<city>-</city>', '')) }));
  assert.equal(v.level, 'bad');
  assert.match(v.text, /\/us\/sun/);
  assert.match(v.text, /never updates/);
});

test('headers without a body point at the single recv()', () => {
  const v = fw.verdict(boot({ cur: 'HTTP/1.1 200 OK\r\nContent-Length: 200\r\n\r\n' }));
  assert.equal(v.level, 'bad');
  assert.match(v.text, /one write/);
});

test('a non-200 or HTTP/1.0 status is called out', () => {
  assert.match(fw.verdict(boot({ for: 'HTTP/1.0 200 OK\r\n\r\n' + FOR })).text, /HTTP\/1\.1 200 OK/);
});

test('an unreachable server is reported as such', () => {
  const results = boot();
  results[0] = { endpoint: 'cur', error: 'could not reach x:1: refused' };
  const v = fw.verdict(results);
  assert.equal(v.level, 'bad');
  assert.match(v.text, /could not reach/);
});

test('parsed with problems is a warning, not a pass', () => {
  const v = fw.verdict(boot({ sun: ok(SUN.replace('6:43 PM', '18:43')) }));
  assert.equal(v.level, 'warn');
  assert.match(v.text, /sunset "18:43"/);
});
