# cbz-gallery-downloader

A small Windows command-line tool that downloads galleries from nhentai.net as `.cbz` files, with the
gallery's tags embedded as `ComicInfo.xml` (read by Komga, Kavita, Mihon/Tachiyomi, YACReader, Calibre).

For personal archiving only. **18+ content.** See the disclaimer at the bottom.

## Features
- Download by link or ID, from a text file, or in **watch mode** (copy links, they download automatically).
- Parallel image downloads (16 at a time), with a live progress line and a session summary.
- One `.cbz` per gallery, written only when every page downloaded. Tags, artist, language, etc. go into `ComicInfo.xml`.
- A local SQLite database (`downloaded.db`) remembers finished IDs, so they are skipped without any network request.
  The watch-mode backlog is stored there too, so closing the window loses nothing.
- Stops with a clear message if the cookie has expired, leaving the rest of the queue saved.

## Setup (Windows, Python 3.9+)
```
pip install nhentai curl_cffi
```
The site needs a cookie from your own browser session. The tool reuses the one saved by the `nhentai` CLI:

1. Open nhentai.net in your browser and let the page load normally.
2. Press F12, open **Application > Cookies > nhentai.net**, and copy the value of `cf_clearance`.
3. In the **Console** tab run `navigator.userAgent` and copy the result.
4. Save both (use the same browser, and the same VPN state, as in step 1):
   ```
   nhentai --cookie "cf_clearance=PASTE_VALUE" --useragent "PASTE_USER_AGENT"
   ```

**Treat that cookie like a password.** Never paste it into issues, logs, screenshots or commits. Running the command
above stores it in your shell history; clear it afterwards if the machine is shared. If downloads start failing with
403, repeat these steps to get a fresh cookie.

## Usage
```
python nh.py https://nhentai.net/g/123456/      # one or more links / IDs
python nh.py links.txt                          # every link or ID in a text file (one per line)
python nh.py --watch                            # watch the clipboard for nhentai.net/g/<id> links
python nh.py --forget 123456                    # remove an ID from the database so it can be downloaded again
```
`watch.bat` starts watch mode with a double-click.

Output goes to `%USERPROFILE%\Downloads\nhentai\<title>.cbz`. If a different gallery already uses that title, the new file
is named `<title> (<id>).cbz`. Pages keep the format the site serves (usually `.webp`).

`downloaded.db` is created next to `nh.py` on first run. At startup, any `.cbz` already in the output folder is imported
into it (the ID is read from its `ComicInfo.xml`).

## Notes
- Windows only (cookie location and clipboard access).
- `PAGE_THREADS` in `nh.py` controls parallel image downloads. Values above about 24 got throttled in testing.
- Every page of a gallery is held in memory until the `.cbz` is written (typically 10 to 100 MB).

## Disclaimer
This project is not affiliated with or endorsed by nhentai.net. It hosts no content and ships no cookies or credentials.
It only automates requests that your own browser session is already allowed to make. You are responsible for complying
with the site's terms, copyright law, and the laws where you live, and for being of legal age to view the material.
Provided as is, without warranty (see LICENSE).
