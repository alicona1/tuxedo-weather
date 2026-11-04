# The Tuxedo Touch weather protocol

What the Honeywell Tuxedo Touch (firmware TUXW V5.3.21.0) expects from a weather server, as read from the disassembly of its `tuxedo` binary and checked against a working panel. Addresses are virtual addresses in that binary. Each rule here broke the panel's weather screen at least once when we got it wrong, usually with no error anywhere.

`tests/test_firmware_contract.py` checks every rule against `app.py`, and the emulator (`emulator/`) checks them against any server.

## Connection

- **Hosts.** The panel connects to `weather.tuxconnect.info`, then `weather1.tuxconnect.info` and `weather2.tuxconnect.info` as fallbacks, always on port **10086**. Point all three at your server with local DNS.
- **Request.** A raw socket, not an HTTP library: `POST /us/<endpoint>/<zip>.xml HTTP/1.1` with CRLF line endings and a `Host` header. The endpoints are `cur`, `for` and `sun`.
- **Schedule.** Current conditions every 2 hours, the forecast every 6 hours, and sun times at startup and then **once a day**. On our panel the daily sun fetch came at about 4:50 AM, alongside the regular forecast and current fetches, with no restart.
- **One `recv()`.** `get_data()` (0x485bc) reads the reply with a single `recv()` into a 2048-byte buffer, with a 10-second timeout. The status line, headers and body must arrive in one TCP segment and fit in 2048 bytes. A server that writes the headers and body separately looks fine to curl and to a browser, but the panel only ever sees the headers. This was the cause of our panel's "Unknown City."
- **Status line.** `http_check` (0x46b24) rejects any reply containing `HTTP/1.1 404`, then requires the literal string `HTTP/1.1 200 OK`. `HTTP/1.0 200 OK` fails. Any other status makes the panel skip the reply and keep what it shows.

## Which parser runs

`CWeatherHelper::parse()` (0x655d4) doesn't look at the URL or the root element. It does a case-sensitive substring search on the reply, and the first match wins:

1. contains `current` → `parse_current_data`
2. else contains `forecast` → `parse_forecast_data`
3. else contains `city` → `parse_sunncs_data`

So no value may contain a lowercase `current` or `forecast` where it would send a reply to the wrong parser, and the sun reply must contain `city` somewhere.

**The screen only updates when the sun parser runs.** `parse_sunncs_data` emits `sigNotifyUpdateWeather`, which redraws both screens. Since sun is only fetched at startup and once a day, a bad sun reply leaves the weather screen blank for up to a day. A server should always answer `/us/sun` with 200.

## The XML

All three parsers loop with `QXmlStreamReader::readNextStartElement()` and no `skipCurrentElement()` fallback. **The first element a parser doesn't handle ends the loop**, and every field after it is silently dropped. Send exactly these elements, in this order. (`app.py` sends no XML declaration; we haven't tested whether one is safe.)

### `/us/cur/<zip>.xml`

```xml
<current><location>Chicago, IL</location><temp>75</temp><rhumid>68</rhumid><wind_dir>NE</wind_dir><windspeed>5</windspeed><icon>1</icon></current>
```

- `location` is shown on the home tile and in the weather screen's City field.
- `temp` is shown as `75°F`, `rhumid` as `Humidity: 68%`.
- Wind is built while parsing (0x4d2f0, 0x4d3dc): `<wind_dir> at <windspeed> mph`, e.g. `NE at 5 mph`, and both screens show it unchanged. It's only updated when `<windspeed>` is read.

### `/us/for/<zip>.xml`

```xml
<forecast>
  <day><name>Monday</name><high>80</high><low>60</low><icon>1</icon></day>
  <day><name>Tuesday</name><high>81</high><low>61</low><icon>2</icon></day>
  <day><name>Wednesday</name><high>82</high><low>62</low><icon>3</icon></day>
  <day><name>Thursday</name><high>83</high><low>63</low><icon>4</icon></day>
</forecast>
```

**The first `<day>` must be today.** `CWeatherCities::RefreshScreen()` (0x14a248) shows its high and low beside the current conditions, without a name, and lists the next three days by name. Four days in all.

### `/us/sun/<zip>.xml`

```xml
<day><date>Oct. 22, 2026 12:33 PM</date><sunrise>7:27 AM</sunrise><sunset>6:43 PM</sunset><city>-</city></day>
```

- **`<city>` is a trigger, not data.** The sun parser never reads it, but without it the dispatcher never calls the parser. It must be **last**, so the three fields are read before the parser stops at it.
- **Sun times are 12-hour with no leading zero:** `7:27 AM`, `6:43 PM`. The screen shows them as sent, but `parse_sunncs_data` also reads the hour with `strtol(s)` and the minute with `strtol(s + 2)`, and `fetch_today_sun_time()` (0x40a50) adds 12 to the sunset hour for the panel's scheduler. `18:43` becomes hour 30, minute 0. A two-digit hour (10 to 12) reads the minute as 0.
- `<date>` is display-only, shown as `Last Update: <date>`.

## Sunrise and sunset scenes

The panel's scene scheduler uses the sun times directly. `CEventCaptureThread::CompareDateTime` (0x305ddc) runs once a minute. For a scene set to sunrise or sunset, it builds the trigger time with `QTime::setHMS()` from `fetch_today_sun_time()` (0x305f74, 0x305fec) and fires when the clock's hour and minute match it exactly, on the scene's date or on its selected weekdays. `CompareTimeRange` (0x306240) uses the same times for time ranges.

- A time `setHMS()` rejects never matches, so the scene silently never runs. That's what `18:43` did: hour 30.
- With the 12-hour format, the trigger is the right minute every day in the lower 48 and Hawaii. Where sunrise or sunset has a two-digit hour (Alaska), the scene fires at the top of that hour instead.
- Because sun times are fetched once a day, scenes follow the changing sunrise and sunset, DST changes included.

## Icons

`<icon>` is an **AccuWeather icon code**, not the number in the image's file name. `extractIcon()` (0x57290) indexes a table of 45 entries at 0xd089a8. The bounds check is unsigned, so anything over 44, negatives included, shows entry 4 (cloudy). A value that isn't a number reads as 0.

| Code | Image | AccuWeather meaning |
|------|-------|---------------------|
| 1, 5, 30, 33, 34 | sunny | Sunny, hazy sunshine, hot, clear (night) |
| 2, 3, 36 | mostly sunny | Mostly or partly sunny, intermittent clouds (night) |
| 6, 35, 38 | mostly cloudy | Mostly cloudy |
| 4, 7, 8 | cloudy | Intermittent clouds, cloudy, dreary (and anything over 44) |
| 11 | fog | Fog |
| 12-14, 39, 40 | storm | Showers |
| 15-17, 41, 42 | thunderstorm | Thunderstorms |
| 18 | rain | Rain |
| 19-21, 23, 43, 44 | chance of snow | Flurries, mostly cloudy with snow |
| 22, 26, 29, 31 | snow | Snow, freezing rain, rain and snow, cold |
| 24 | icy | Ice |
| 25 | sleet | Sleet |
| 32 | windy | Windy |
| 37 | haze | Hazy moonlight |
| 0, 9, 10, 27, 28 | none | No image; never send these |

`app.py` maps Open-Meteo's WMO weather codes onto these in `_panel_icon()`.
