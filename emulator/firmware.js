// What the panel firmware (TUXW V5.3.21.0) does with a weather response, as
// far as disassembly shows. No DOM here, so node can test it.
// Addresses are virtual addresses in the tuxedo binary.

// extractIcon() (0x57290): table at 0xd089a8, codes 0-44. A NULL entry gives
// no image. The bounds check is unsigned (cmp r6, #0x2c; bls at 0x572c4), so
// anything past the table, negatives included, gets entry 4, 7_cloudy.png.
export const ICONS = [
  null, '1_sunny.png', '2_mostly_sunny.png', '2_mostly_sunny.png', '7_cloudy.png',
  '1_sunny.png', '4_mostly_cloudy.png', '7_cloudy.png', '7_cloudy.png', null,
  null, '14_fog.png', '9_storm.png', '9_storm.png', '9_storm.png',
  '9_thunderstorm.png', '9_thunderstorm.png', '9_thunderstorm.png', '5_rain.png', '6_chance_of_snow.png',
  '6_chance_of_snow.png', '6_chance_of_snow.png', '12_snow.png', '6_chance_of_snow.png', '13_icy.png',
  '11_sleet.png', '12_snow.png', null, null, '12_snow.png',
  '1_sunny.png', '12_snow.png', '18_windy.png', '1_sunny.png', '1_sunny.png',
  '4_mostly_cloudy.png', '2_mostly_sunny.png', '16_haze.png', '4_mostly_cloudy.png', '9_storm.png',
  '9_storm.png', '9_thunderstorm.png', '9_thunderstorm.png', '6_chance_of_snow.png', '6_chance_of_snow.png',
];

export function iconFile(code) {
  // QString::toInt: whole string as a number, or 0 if it isn't one.
  const n = /^\s*[+-]?\d+\s*$/.test(code ?? '') ? Number.parseInt(code, 10) : 0;
  return n >= 0 && n < ICONS.length ? ICONS[n] : ICONS[4];
}

// http_check (0x46b24): strstr for a 404 first, then the literal 200 line.
export function httpCheck(raw) {
  if (raw.includes('HTTP/1.1 404')) return -404;
  if (raw.includes('HTTP/1.1 200 OK')) return 0;
  return -405;
}

export function body(raw) {
  const i = raw.indexOf('\r\n\r\n');
  return i < 0 ? '' : raw.slice(i + 4);
}

// CWeatherHelper::parse() (0x655d4): case-sensitive substring search,
// first match wins.
export function dispatch(text) {
  if (text.includes('current')) return 'cur';
  if (text.includes('forecast')) return 'for';
  if (text.includes('city')) return 'sun';
  return 'none';
}

// Just enough of QXmlStreamReader for these flat documents: a list of start
// tags, end tags and text, stopping at the first thing that isn't well formed.
function tokens(xml) {
  const out = [];
  const re = /<\?[^>]*\?>|<(\/?)([A-Za-z_][\w.-]*)\s*(\/?)>|([^<]+)|(<)/g;
  let m;
  while ((m = re.exec(xml))) {
    if (m[0].startsWith('<?')) continue;
    if (m[5]) { out.push({ error: 'malformed tag' }); break; }
    if (m[4] !== undefined) { out.push({ text: m[4] }); continue; }
    if (m[1]) out.push({ end: m[2] });
    else {
      out.push({ start: m[2] });
      if (m[3]) out.push({ end: m[2] });
    }
  }
  return out;
}

const decode = (s) => s.replace(/&lt;/g, '<').replace(/&gt;/g, '>')
  .replace(/&quot;/g, '"').replace(/&apos;/g, "'").replace(/&amp;/g, '&');

// Walks `xml` like the firmware parsers: readNextStartElement() loops with
// no skipCurrentElement() fallback, so the first unhandled element ends the
// loop and everything after it is dropped. `handle(path, tag)` says whether
// a tag is read as text ('text'), entered as a container ('enter'), or not
// handled (anything else).
function walk(xml, root, handle, onValue, notes) {
  const toks = tokens(xml);
  let i = 0;
  const next = () => toks[i++];
  const first = toks.findIndex((t) => t.start);
  if (first < 0 || toks[first].start !== root) {
    notes.push(`root element is not <${root}>`);
    return;
  }
  i = first + 1;
  const stack = [root];
  for (;;) {
    let t = next();
    while (t && t.text !== undefined && !t.text.trim()) t = next();
    if (!t) { notes.push(`document ended inside <${stack.at(-1)}>`); return; }
    if (t.error) { notes.push(t.error); return; }
    if (t.end) {
      stack.pop();
      if (!stack.length) return;
      continue;
    }
    if (t.text !== undefined) { notes.push(`unexpected text in <${stack.at(-1)}>`); return; }
    const how = handle(stack, t.start);
    if (how === 'enter') { stack.push(t.start); onValue(stack, t.start, null); continue; }
    if (how !== 'text') {
      notes.push(`<${t.start}> is not handled here, so the loop stops and the rest is dropped`);
      return;
    }
    const tag = t.start;
    let text = '';
    for (t = next(); t && t.text !== undefined; t = next()) text += t.text;
    if (!t || t.end !== tag) {
      notes.push(`<${tag}> is not plain text; readElementText fails`);
      return;
    }
    onValue(stack, tag, decode(text));
  }
}

// parse_current_data (0x4cf54)
const CURRENT = { location: 'city', temp: 'temp', rhumid: 'humidity',
  wind_dir: 'windDir', windspeed: 'windSpeed', icon: 'icon' };

export function parseCurrent(xml, state) {
  const notes = [];
  state.date = ' ';
  const seen = new Set();
  // The wind line is built here, not on screen. A local that starts empty on
  // every call (0x4cf70) gets <wind_dir> + " at " (0x4d2f0), then
  // <windspeed> + " mph" (0x4d3dc), and only then goes into the global at
  // 0xd0ed9c. Both screens show that global as is (0x14a87c, 0x57658), so no
  // <windspeed> means the wind line doesn't change.
  let wind = '';
  walk(xml, 'current', (_, tag) => (tag in CURRENT ? 'text' : null),
    (_, tag, value) => {
      state[CURRENT[tag]] = value;
      seen.add(tag);
      if (tag === 'wind_dir') wind = value + ' at ';
      if (tag === 'windspeed') state.wind = wind + value + ' mph';
    }, notes);
  for (const tag in CURRENT) if (!seen.has(tag)) notes.push(`<${tag}> was never read`);
  return notes;
}

// parse_forecast_data (0x58ef8): <day> containers holding name/high/low/icon.
const DAY = ['name', 'high', 'low', 'icon'];

export function parseForecast(xml, state) {
  const notes = [];
  state.date = ' ';
  const days = [];
  walk(xml, 'forecast',
    (stack, tag) => (stack.length === 1 ? (tag === 'day' ? 'enter' : null) : (DAY.includes(tag) ? 'text' : null)),
    (stack, tag, value) => {
      if (value === null) days.push({});
      else days.at(-1)[tag] = value;
    }, notes);
  state.days = days;
  if (days.length < 4) notes.push(`only ${days.length} <day> elements; the screen shows today plus 3 days`);
  days.forEach((d, i) => {
    for (const tag of DAY) if (!(tag in d)) notes.push(`day ${i + 1} has no <${tag}>`);
  });
  return notes;
}

// parse_sunncs_data (0x64c1c). <city> isn't read: it only steers the
// dispatcher, so it has to come after the three fields.
const SUN = ['date', 'sunrise', 'sunset'];

export function parseSun(xml, state) {
  const walked = [];
  state.date = ' ';
  const seen = new Set();
  walk(xml, 'day', (_, tag) => (SUN.includes(tag) ? 'text' : null),
    (_, tag, value) => { state[tag] = value; seen.add(tag); }, walked);
  // Stopping at <city> is how this parser is meant to end.
  const notes = walked.filter((n) => !n.startsWith('<city> is not handled'));
  for (const tag of SUN) if (!seen.has(tag)) notes.push(`<${tag}> was never read`);
  // fetch_today_sun_time() (0x40a50): hour = strtol(s), minute = strtol(s + 2),
  // then 12 is added to the sunset hour.
  for (const key of ['sunrise', 'sunset']) {
    const s = state[key];
    if (typeof s !== 'string') continue;
    const h = strtol(s), m = strtol(s.slice(2));
    const hour = key === 'sunset' ? h + 12 : h;
    if (hour > 23 || m > 59) notes.push(`${key} "${s}" reads as ${hour}:${String(m).padStart(2, '0')}`);
  }
  return notes;
}

export function strtol(s) {
  const m = /^\s*([+-]?\d+)/.exec(s);
  return m ? Number.parseInt(m[1], 10) : 0;
}

// The whole path from one recv() to the panel's weather state. Returns what
// happened, for the log. `state` only changes if the response gets parsed.
export function receive(raw, state) {
  const check = httpCheck(raw);
  if (check !== 0) return { check, parser: null, notes: ['http_check failed; the screen keeps what it shows'] };
  const text = body(raw);
  const parser = dispatch(text);
  const run = { cur: parseCurrent, for: parseForecast, sun: parseSun }[parser];
  if (!run) return { check, parser, notes: ['no parser matched; nothing changes'] };
  return { check, parser, notes: run(text, state) };
}

export const PARSERS = { cur: 'parse_current_data', for: 'parse_forecast_data', sun: 'parse_sunncs_data' };

// Why one response failed, in terms of what to change on the server.
function failure(r) {
  const where = `/us/${r.endpoint}`;
  if (r.error) return `${where}: ${r.error}`;
  if (r.check === -404) return `${where}: the response contains "HTTP/1.1 404", so http_check rejects it`;
  if (r.check !== 0) {
    const status = (r.raw || '').split('\r\n')[0] || 'nothing';
    return `${where}: http_check needs "HTTP/1.1 200 OK" in the first recv(), got "${status}"`;
  }
  if (r.parser === 'none' && !body(r.raw).trim()) {
    return `${where}: the body wasn't in the first recv(). Send the headers and body in one write`;
  }
  if (r.parser === 'none') return `${where}: no parser matched. It needs to contain "${{ cur: 'current', for: 'forecast', sun: 'city' }[r.endpoint]}"`;
  return `${where}: went to ${PARSERS[r.parser]} instead of ${PARSERS[r.endpoint]}`;
}

// One line for a whole boot: results of cur, for and sun, each a receive()
// result plus { endpoint, raw } or { endpoint, error }.
export function verdict(results) {
  const failed = results.filter((r) => r.error || r.check !== 0 || r.parser !== r.endpoint);
  if (failed.length) {
    const sunFailed = failed.some((r) => r.endpoint === 'sun');
    const tail = sunFailed ? ' Without the sun response the screen never updates.' : '';
    return { level: 'bad', text: `The panel would reject this. ${failure(failed[0])}.${tail}` };
  }
  const notes = results.flatMap((r) => r.notes.map((n) => `/us/${r.endpoint}: ${n}`));
  if (notes.length) {
    return { level: 'warn', text: `The panel would accept all three responses, with problems: ${notes.join('; ')}.` };
  }
  return { level: 'ok', text: 'The panel would accept all three responses and update the screen.' };
}

// Text the screens show, from CWeatherCities::RefreshScreen (0x14a248) and
// CHomeWeather::RefreshHomeWeather (0x574e4). apl_getTempUnit() is F here.
const DEG = '°F';
const has = (v) => typeof v === 'string' && v !== '';

export function weatherScreenText(state) {
  const days = state.days || [];
  const today = days[0] || {};
  return {
    temp: has(state.temp) ? state.temp + DEG : 'Temp N/A',
    humidity: has(state.humidity) ? `Humidity: ${state.humidity}%` : 'Humidity N/A',
    high: (today.high ?? '') + DEG + '       High',
    low: (today.low ?? '') + DEG + '       Low',
    sunrise: 'Sunrise: ' + (state.sunrise ?? ''),
    sunset: 'Sunset:  ' + (state.sunset ?? ''),
    wind: state.wind ?? 'Wind Cond N/A',
    icon: iconFile(state.icon),
    days: [1, 2, 3].map((i) => {
      const d = days[i] || {};
      return {
        name: d.name ?? 'N/A',
        high: (d.high ?? '') + DEG + '       High',
        low: (d.low ?? '') + DEG + '       Low',
        icon: iconFile(d.icon),
      };
    }),
    lastUpdate: 'Last Update: ' + (state.date ?? ''),
  };
}

export function homeTileText(state) {
  return {
    city: state.city ?? '',
    temp: has(state.temp) ? state.temp + DEG : 'Temp N/A',
    humidity: has(state.humidity) ? `Humidity ${state.humidity}%` : 'Humidity N/A',
    wind: state.wind ?? 'Wind Cond N/A',
    icon: iconFile(state.icon),
  };
}
