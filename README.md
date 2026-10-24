# mobile-store-scraper

[![PyPI](https://img.shields.io/pypi/v/mobile-store-scraper)](https://pypi.org/project/mobile-store-scraper/) [![tests](https://github.com/Nitro4CSR/mobile-store-scraper/actions/workflows/ci.yml/badge.svg)](https://github.com/Nitro4CSR/mobile-store-scraper/actions/workflows/ci.yml)

Async scraper for **Google Play** and the **Apple App Store** with a **reliable version for every app**, even Play apps that only say *"Varies with device"* (multi-APK / app bundles). Dates come back in one **unified UTC format**, **every storefront country** is supported, and every value is a plain **attribute**.

```
pip install mobile-store-scraper
```

```python
import mobile_store_scraper as mss          # the import name uses underscores

app = mss.scrape(store="gp", app="com.naturalmotion.customstreetracer2", country="DE")

app.version        # '6.8.0'
app.last_updated   # '2026-10-14T07:10:38Z'
app.min_os_ver     # 'Android 7.0'
app.downloads      # 82008679        (exact when Play exposes it, else '50000000+')
app.released       # '2016-06-29T15:01:05Z'
app.publisher      # 'Zynga'

ios = mss.scrape(store="as", app="net.whatsapp.WhatsApp", country="JP")   # bundle id or track id
ios.version, ios.last_updated, ios.min_os_ver, ios.publisher             # ('26.41.75', '2026-10-22T15:42:36Z', 'iOS 15.1', 'WhatsApp Inc.')
```

Async:

```python
app = await mss.scrape_async(store="gp", app="com.android.chrome", country="US")

async with mss.Scraper() as s:                        # reuse connections + caches
    everywhere = await s.scrape_countries("gp", "com.whatsapp")          # all 225 Play countries
    many       = await s.scrape_many("as", [310633997, 284882215], "DE")  # one batched request
```

`store` accepts `"gp"`, `"google_play"`, `"play"`, `"android"` or `"as"`, `"app_store"`, `"apple"`, `"ios"`.

## Pace: you choose, default is as fast as possible

```python
mss.scrape(..., concurrency=None, rate_limit=None)    # default: unlimited
async with mss.Scraper(concurrency=20, rate_limit=10) as s: ...   # max 20 in flight, 10 req/s per host
```

`concurrency` caps simultaneous requests and `rate_limit` caps requests per second per host. The same options exist on `GooglePlay(...)`, `AppStore(...)`, the CLI (`--concurrency`, `--rate-limit`) and the bench.

Failures are handled no matter which pace you pick:
* A 429 puts the host into a shared, growing cooldown that recovers by itself.
* A redirect to Google's "unusual traffic" page (`google.com/sorry`) pauses the host and retries, then raises `IpBlocked`.
* A circuit breaker stops a sweep from prolonging that block.

In testing, ~1,500 unpaced Play listing requests within ~4 minutes got an IP flagged for roughly half an hour. If you sweep that hard, set a pace or a `proxy=`.

## Result attributes

Fields a store doesn't expose are **not defined** on that store's class; for example, the App Store has no `downloads`. Fields a store can expose but didn't for this app are `None`. Dates are ISO-8601 UTC strings, also available as `datetime` via `last_updated_dt` / `released_dt`. `app.to_dict()` and `app["version"]` work too.

**Both stores** (`AppData`): `store, app_id, country, language, url, title, version, version_source, last_updated, released, min_os_ver, publisher, developer_id, developer_website, description, release_notes, genre, genre_id, icon, screenshots, score, ratings, price, currency, free, content_rating, raw`

**Google Play** (`GooglePlayApp`) adds every key google-play-scraper returns, in snake_case: `summary, description_html, installs, min_installs, real_installs, reviews, histogram, sale, sale_time, original_price, sale_text, offers_iap, in_app_product_price, developer, developer_email, developer_address, privacy_policy, categories, header_image, video, video_image, content_rating_description, ad_supported, contains_ads, comments, last_updated_on`. On top of those it has:

* `downloads`
* `version_code`, `varies_with_device`, `device_versions`, `version_last_updated`
* `min_sdk`, `target_sdk`, `download_size`, `offered_by`

**App Store** (`AppStoreApp`): `track_id, bundle_id, seller, seller_url, developer_url, file_size_bytes, supported_devices, ipad_screenshots, appletv_screenshots, languages, genres, genre_ids, advisories, features, content_advisory_rating, score_current_version, ratings_current_version, formatted_price, is_game_center_enabled, kind`

## How versions are resolved

**Google Play** (`version_mode`):

* `"auto"` *(default)*:
  * The version comes from the public listing.
  * When the listing has none ("Varies with device"), the **Play Android API** is asked instead. That's `android.clients.google.com/fdfe/details`, the API the Play Store app uses, queried for the build a concrete device would install (default: Pixel 9a, Android 15, arm64).
  * The same API call fills `min_os_ver`, exact `downloads`, `target_sdk` and `download_size` when the listing lacks them.
* `"web"`: listing only, never logs in.
* `"api"`: always asks the API too, which adds `version_code` for every app.
* `"all_devices"`: queries several device profiles. Every build lands in `device_versions`, and `version` is the highest.

```python
app = mss.scrape("gp", "com.google.android.youtube", "US", version_mode="all_devices")
[(d.device, d.version) for d in app.device_versions]
# [('px_9a','21.42.243'), ('sm_s25u','21.41.526'), ('px_tablet','21.32.4'), ('google_kiwi_x86_64','21.43.523'), ...]
```

The API is logged into anonymously with a token from Aurora Store's community dispenser (`auroraoss.com`). The session is cached in your user cache directory, so it is only requested once. You can use your own account instead: `mss.Scraper(email="you@gmail.com", aas_token="aas_et/...")`.

**App Store**: the iTunes lookup API (up to 150 ids per request). When lookup has no result, the `apps.apple.com` page is used. `verify_with_web=True` cross-checks lookup against the page and keeps the newer version, because lookup's CDN can lag a release by a few hours.

## Dates

* `last_updated`: the store's *Updated on* / current-version release date.
  * Play: the exact Unix timestamp from the listing, identical across languages and countries. The store's own localized text is in `last_updated_on`, e.g. `"14.10.2026"`.
  * App Store: `currentVersionReleaseDate`.
* `version_last_updated` (Play, API): the upload date of exactly the build in `version`. It can be older than `last_updated`, because Play also bumps *Updated on* for staged rollouts and listing changes.

## Countries

`mss.PLAY_COUNTRIES` (225, from Google's supported-locations table) and `mss.APP_STORE_COUNTRIES` (184, every ISO code Apple's lookup accepts, probed live).

`scrape_countries()` returns `{country: result | AppNotFound | other exception}`. The Play API is called once per device, not once per country.

## Download exact APK builds (Google Play)

```
mobile-store-scraper download com.google.android.calculator            # what a Pixel 9a is offered
mobile-store-scraper download com.example.app --device px_tablet --info # list files/sizes only
mobile-store-scraper download com.example.app --vc 6805204              # ask for a specific version code
```

Each download contains the base APK, the split APKs (ABI / density / language), any OBB files and dex metadata. Every file is checked against the size and SHA-1/SHA-256 that Play reports. In Python:

```python
from mobile_store_scraper import PlayApiClient
async with PlayApiClient(device="px_9a") as c:
    info, paths = await c.download("com.example.app", "downloads/")
```

Limits:
* Only free apps, or apps the logged-in account owns.
* Play only serves currently live version codes, so old builds can't be fetched.
* Play Asset Delivery packs (large game content) are not downloaded yet.

## Catching staged rollouts

Play rolls a new build out to a growing share of accounts/devices. The tools below log what independent sessions are offered and when:

```
mobile-store-scraper sample com.naturalmotion.customstreetracer2 -n 10                 # fresh device ids, one account
mobile-store-scraper sample com.naturalmotion.customstreetracer2 -n 5 --strategy accounts
mobile-store-scraper watch  com.naturalmotion.customstreetracer2 --state-dir watch --always-sample
mobile-store-scraper report com.naturalmotion.customstreetracer2 --state-dir watch       # first-seen time per build
```

`watch` performs one check and is meant to run from a scheduler:
* It logs every observation to `<package>.observations.csv`.
* It tracks every build ever seen in `<package>.state.json`.
* It asks Play for the newest known version code by number (`--no-probe` disables this).
* It downloads new builds only when you pass `--download-dir`.

### Raspberry Pi (log-only watcher)

On Raspberry Pi OS Bookworm or newer (Python 3.10+):

```
curl -fsSL https://raw.githubusercontent.com/Nitro4CSR/mobile-store-scraper/main/deploy/raspberry-pi/install.sh -o install.sh
PACKAGE=com.naturalmotion.customstreetracer2 INTERVAL='*:0/30' SAMPLES=6 COUNTRY=DE bash install.sh
```

This installs a systemd timer that runs the check every 30 minutes. The timer survives reboots and catches up on runs it missed while the Pi was off. When you notice a new release, run `report` to see when the watcher first saw it and how (`main`, `sample:devices` or `probe_by_code`).

## CLI

```
mobile-store-scraper gp com.naturalmotion.customstreetracer2 -c DE
mobile-store-scraper as 310633997 -c all --concurrency 20 --rate-limit 10
mobile-store-scraper gp com.google.android.youtube -m all_devices --json
```

## Bench / tests

```
python bench/bench.py --quick            # live acceptance run, few countries
python bench/bench.py                    # all countries, both stores, unlimited pace
python bench/bench.py --concurrency 8 --rate-limit 10
python bench/bench.py --validate-countries
python bench/capture_fixtures.py         # refresh offline fixtures
pytest                                   # offline tests against captured real responses
```

## License

The code is MIT. The bundled Android device profiles (`src/mobile_store_scraper/data/devices.properties`) are GPL-3.0-or-later, as marked in that file. They come from EFF's rs-google-play and originate with the Calyx Institute and AuroraOSS.

This project is not affiliated with Google, Apple or AuroraOSS. Respect the stores' terms of service and the dispenser's fair use.
