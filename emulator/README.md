# Panel emulator

Test a weather server against the Honeywell Tuxedo Touch without a panel. The emulator fetches from your server exactly the way the panel's firmware (TUXW V5.3.21.0) does, runs the firmware's checks and parsers on the raw response, and draws the panel's home and weather screens from the result. If the panel would reject a response, misread it, or never update the screen, the emulator says so and says why.

## Test your server

You need Python 3.9 or later (Flask 3.1's minimum). From the repo root:

```sh
pip install -r requirements.txt
python -m emulator.serve --upstream 192.168.1.50:10086
```

Open http://127.0.0.1:8800/. The panel only ever connects to port 10086, so the port is optional: `--upstream 192.168.1.50` means the same thing. Without `--upstream`, the emulator runs this repo's own server (`app.py`) in-process. That's a known good reference.

Each **Fetch** does what the panel does at boot: it requests `/us/cur`, `/us/for` and `/us/sun` for the ZIP code. The line under the controls gives the result:

- **Green:** the panel would accept all three responses and update the screen.
- **Amber:** the panel accepts them, but something is off. For example, a field it never reads, or a 24-hour sun time it would misread.
- **Red:** the panel would reject a response, with the reason. For example, a status line other than `HTTP/1.1 200 OK`, or the body not arriving in the first `recv()`. A failed `/us/sun` means the screen never updates.

Below that, each response is logged with exactly the bytes the panel would have received.

To try a different server without restarting, type it in the **Server** box. That box only works while the emulator listens on this machine only, which is the default. With `--host 0.0.0.0` (open to your network), the server is fixed by `--upstream`, so other people on the network can't make your machine connect wherever they like.

## The panel's look (optional)

Without the firmware, the screens are drawn as outlines in the panel's layout, and the response checks work the same. Each weather icon shows as a box naming the image file the panel would display.

To see the real screens, extract the images from your own copy of the firmware. This repo can't include the firmware or the images, because they're Honeywell's. You need `app2.hdr` from the TUXW V5.3.21.0 update package, which Alarm Grid hosts on its [Tuxedo Touch firmware page](https://www.alarmgrid.com/downloads/tuxedo-touch-firmware-updates) (`TUXW_V5.3.21.0_CN.zip`), plus [binwalk](https://github.com/ReFirmLabs/binwalk) and [jefferson](https://github.com/onekey-sec/jefferson):

```sh
mkdir -p TUXW_V5.3.21.0_CN && cp /path/to/app2.hdr TUXW_V5.3.21.0_CN/
cd TUXW_V5.3.21.0_CN && binwalk -e app2.hdr
cd _app2.hdr.extracted && jefferson 80.jffs2 -d jffs2-root
cd ../.. && python -m emulator.extract_assets
```

These are the binwalk 2.x commands; binwalk 3 writes to `extractions/` instead. If the files end up somewhere else, pass the binary's path: `python -m emulator.extract_assets path/to/jffs2-root/tuxedo`.

That writes 694 images to `emulator/assets/`, which is git- and docker-ignored. Don't commit or publish them. Another firmware version may lay its screens out differently from what's described here.

## How faithful it is

- **Data path.** `serve.py` sends the panel's request (`POST /us/<ep>/<zip>.xml` with its `Host` header) and keeps only the first `recv()` of up to 2048 bytes, like `get_data()` (0x485bc). `firmware.js` then applies `http_check` (0x46b24), the `parse()` substring dispatcher (0x655d4), and the three parsers, including the way an unhandled element ends the loop. The screens only redraw after the sun response parses, because that's when the panel emits `sigNotifyUpdateWeather`.
- **Layout.** Positions, fonts and text formats come from the constructors and refresh functions (`CHomeScreen` 0x61a08, `CHomeWeather` 0x586a4, `CHomeClock` 0x508a4, `CConnectionStatus` 0x5c184, `CWeatherCities` 0x14fc40, `RefreshScreen` 0x14a248, `RefreshHomeWeather` 0x574e4). A few positions are computed at run time and estimated from photos instead: the pie labels, the status bar, and the ZIP and City fields. Tick **Show layout sources** (or open `#sources`) to see which is which.
- **Fonts.** Qt Embedded only has bitmap Helvetica at 8, 10, 12, 14, 18 and 24 px, so each requested size snaps to the nearest one. The browser draws them with Helvetica or Arial. Line wrapping matched the panel in our photos ("Humidity: 94%" wraps on both), but text right at a width limit could still differ.
- **Wind.** `parse_current_data` builds `<wind_dir> at <windspeed> mph` (e.g. `NW at 9 mph`), and both screens show it unchanged, with no "Wind:" label. It only changes when `<windspeed>` is read.
- **Not traced.** The home tile's condition line, and the default font of items that never get `setFont()`.
- **Status strip.** The Z-Wave, cable and Wi-Fi icons are fixed to what our panel showed; the real ones follow the network status.

`#weather` opens the weather screen directly.

## Tests

`tests/test_extract_assets.py` and `tests/test_emulator_serve.py` run with pytest. The firmware logic has its own Node tests, which CI doesn't run:

```sh
node --test emulator/firmware.test.mjs
```
