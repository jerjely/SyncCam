<p align="center"><img src="assets/icon_256.png" width="128" alt="SyncCam logo"></p>

# SyncCam

Records video from a USB camera together with signals that an Arduino samples:

- the **TTL sync signal** that also goes to the Neuropixels (OneBox / Open Ephys), on A0
- a **microphone**, on A1
- an **IR LED**, which the Arduino drives and which the camera sees, on D8

Every video frame, every Arduino sample and every TTL edge is saved on the same clock,
so you can line up the video with the Neuropixels recording through the shared TTL.
The IR LED checks that alignment in the video itself.

Windows 10/11 and macOS (Apple silicon and Intel).

![SyncCam while recording](docs/screenshots/3_recording.png)

## Download

Get the installer for your computer from the [latest release](https://github.com/jerjely/SyncCam/releases/latest):

| Your computer | File |
|---|---|
| Windows 10 / 11 | `SyncCam-x.y.z-Windows-setup.exe` |
| Mac with Apple silicon (M1 or newer) | `SyncCam-x.y.z-macOS-AppleSilicon.pkg` |
| Mac with an Intel processor | `SyncCam-x.y.z-macOS-Intel.pkg` |

The installers are not code-signed:

- **Windows:** click **More info** → **Run anyway**. No administrator rights are needed.
- **Mac:** if macOS refuses to open the .pkg, go to **System Settings → Privacy & Security** and click **Open Anyway**.
  The first time you open a camera, macOS asks for camera access. Click **Allow**. If you clicked
  **Don't Allow**, turn SyncCam on under **Privacy & Security → Camera**, then open the camera again.

## Arduino

Upload `firmware/SyncCam_Arduino/SyncCam_Arduino.ino` once with the Arduino IDE (Arduino Uno or Nano).

| Arduino pin | Connect to |
|---|---|
| A0 | TTL sync signal (the same TTL that goes to the Neuropixels), 0–5 V |
| A1 | Microphone module output (MAX4466, MAX9814 or similar, biased at about 2.5 V) |
| D8 | IR LED through a resistor (for example 180 Ω for a 20 mA LED), or a transistor/MOSFET for brighter LEDs |
| GND | Common ground with the TTL source and the microphone |

Put the IR LED where the camera sees it (at the edge of the picture, or pointing at the lens).

The sketch samples both analog inputs at the chosen rate (1–10 kHz, default 5 kHz, 10-bit) and
streams them at 1,000,000 baud. Each sample carries a counter, so lost samples are detected and
reported. Commands are documented at the top of the sketch.

**IR LED modes**

| Mode | What the LED does |
|---|---|
| Off / On | Steady |
| Follow TTL | Mirrors the TTL input, so the Neuropixels sync pulses show up in the video |
| Pulses | Flashes with the period and width you set (timed on the Arduino) |

## Using it

1. **Camera:** pick the camera (**Scan** finds connected ones), size and frame rate, then **Open camera**.
2. **Arduino:** pick the port and **Connect**. The board restarts when the port opens, which takes about 2 s.
   Set the TTL threshold (default 2.5 V). The red dashed line in the TTL plot shows it.
3. **IR LED:** choose a mode. Then drag a box around the LED in the camera picture. SyncCam then saves
   the LED's brightness for every frame.
4. **Recording:** choose a folder and a name, then **Record** and **Stop recording**.

**Simulator (no hardware)** and **Test pattern (no camera)** let you try everything without hardware.
In the test pattern, the square in the top-left corner acts as the IR LED.

## What gets saved

Each recording goes into its own folder, `<name>_<date>_<time>/`:

| File | Contents |
|---|---|
| `video.avi` (or `.mp4`) | The camera video |
| `frames.csv` | For every frame: `time_s`, wall clock, the matching Arduino `sample`, LED-box brightness |
| `analog.wav` | 3 channels at the sample rate, raw values: TTL (0–1023), microphone (0–1023), IR LED (0/1) |
| `microphone.wav` | The microphone alone, centred and amplified so you can listen to it |
| `ttl_events.csv` | TTL rising and falling edges: sample, `time_s`, video frame |
| `led_events.csv` | IR LED on and off edges (from the Arduino): sample, `time_s`, video frame |
| `session.json` | Settings, measured frame rate, Arduino clock fit, drop counts, LED-to-video delay |

All `time_s` values are seconds since **Record** was pressed, on the computer's clock.
Arduino sample *n* happened at `first_sample_time_s + n / measured_rate_hz` (both in `session.json`).

**Use `frames.csv` for frame times,** not the frame rate written in the video file. USB cameras
never run at exactly their nominal rate. The file's header uses the rate measured just before recording.

### Lining up with the Neuropixels

The TTL edges in `ttl_events.csv` are the same pulses the OneBox recorded. Match them to the
TTL events in Open Ephys / SpikeGLX, then use the `video_frame` column, or `frames.csv`, to find
the frame for any Neuropixels time.

A frame's time is when the computer received it, which is a little after the exposure. The IR LED
lets you measure this delay: `session.json → led_video_lag` gives the median delay between the
Arduino turning the LED on and the first frame showing it. Subtract it from frame times when you
need millisecond-level alignment. The spread is up to one frame interval.

## Limits

- Arduino Uno/Nano ADC: 10-bit. The microphone channel works for detecting sounds and cues,
  but it is not a measurement microphone. At 5 kHz it records up to 2.5 kHz.
- Frame timing depends on the camera and its USB driver. Check `measured_fps` and the
  dropped-frame counts in `session.json`.
- If the disk cannot keep up, frames are dropped and counted, never silently skipped.
  `frames.csv` lists only the frames that are in the video.

## Running from source

Python 3.10 or newer.

- Windows: `install_windows.bat`, then `start_windows.bat`
- Mac: `install_mac.command`, then `start_mac.command`
  (if GitHub Desktop dropped the executable bit: `chmod +x *.command`)

Or: `pip install -r requirements.txt` and `python main.py`.

`python main.py --selftest` runs the built-in test with the test pattern and the simulated Arduino.

## Building the installers

The installers are built by GitHub Actions (`.github/workflows/build.yml`) on Windows,
macOS Apple silicon and macOS Intel. Each build runs the self-test on the built app and again
on the installed app before anything is published.

- **Actions → Build installers → Run workflow**, type a version such as `1.0.0` → builds, tests and publishes the release
- leave the version empty → build and test only (installers kept as workflow artifacts)
- or push a tag `v1.0.0`

## Licence

MIT. © 2026 Gergely Szarka
