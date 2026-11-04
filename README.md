# tuxedo-weather

A replacement weather server for the Honeywell Tuxedo Touch alarm panel. Honeywell's weather service shut down, and the panel's weather tile and forecast screen went blank. This server answers the panel's requests with current conditions, a 4-day forecast and sun times, using free public data. No API key needed.

It was built by reverse engineering the panel's firmware (TUXW V5.3.21.0). The protocol is stricter than it looks, and [docs/PROTOCOL.md](docs/PROTOCOL.md) spells out every rule.

## Run it

```bash
docker build -t tuxedo-weather .
docker run -d --name tuxedo-weather --restart unless-stopped \
  -v tuxedo-weather-data:/data -p 10086:10086 tuxedo-weather
```

Then point the panel at it. The panel is hardcoded to `weather.tuxconnect.info`, with `weather1.tuxconnect.info` and `weather2.tuxconnect.info` as fallbacks, always on port 10086. Add all three to your local DNS (Pi-hole, your router, etc.) with your server's address. The panel fetches sun times at startup and then once a day, so restart it once to see everything right away.

Without Docker: `pip install -r requirements.txt && python app.py`.

## Where the weather comes from

- **Location:** the panel sends a ZIP code, looked up once on [zippopotam.us](https://zippopotam.us), with Open-Meteo's geocoding as a fallback. US ZIP codes only.
- **Forecast, icons and sun times:** [Open-Meteo](https://open-meteo.com), using NOAA's National Blend of Models, with Open-Meteo's default model as a fallback.
- **Current conditions:** the nearest National Weather Service station, through [api.weather.gov](https://www.weather.gov/documentation/services-web-api), when its report is less than 90 minutes old. Otherwise the model fills in.

Results are cached for 10 minutes and saved to `/data`. If the upstream services are down, the server keeps serving the last good data for up to 6 hours.

## Test a server without a panel

`emulator/` is a browser copy of the panel's home and weather screens. It fetches from a weather server exactly the way the panel does, runs the firmware's checks and parsers on the reply, and tells you whether the panel would accept it, and if not, why.

```bash
pip install -r requirements.txt
python -m emulator.serve --upstream 192.168.1.50:10086   # your server
python -m emulator.serve                                 # or this repo's, in-process
```

Open http://127.0.0.1:8800/. It works without the firmware. With your own copy of it, the screens use the panel's real images. See [emulator/README.md](emulator/README.md).

## Tests

```bash
pip install -r requirements.txt -r requirements-dev.txt && pytest
node --test emulator/firmware.test.mjs
```

The suite needs no network. `tests/test_firmware_contract.py` checks the panel's rules, including the one that took longest to find: the panel reads the reply with a single `recv()`, so the whole response has to go out in one write.

## License

MIT. See [LICENSE](LICENSE).

Not affiliated with Honeywell, Resideo or AccuWeather. Tuxedo Touch is a trademark of its owner. This repo contains no Honeywell firmware or images.
