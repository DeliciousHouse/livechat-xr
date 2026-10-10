# Quest background-audio probe — physical test pending

Debug-only package `com.livechatxr.audioprobe`, Android 10+ / target SDK34. No network, microphone, account, overlay or live chat. APK delivery does **not** pass Gate 2; only Brendan's audible in-game observation can establish feasibility. Do not infer audibility from a live process or `playback_ms`.

## Install and observe (Brendan, Quest 3)

Use an already authorized Quest USB-debugging connection and Android platform-tools (`adb` on PATH). Download the attached `livechatxr-audio-probe.apk` into the current directory; verify its SHA256 before sideloading.

```text
adb install -r livechatxr-audio-probe.apk
adb shell am start -n com.livechatxr.audioprobe/.MainActivity
```

1. Open **Quest Audio Probe** in the headset's 2D launcher / Unknown Sources. Allow its notification permission if offered. Press **Start** once; confirm the spoken numbers one through ten repeat (about 24 seconds per loop). Repeated Start does not create another player.
2. Launch Population: ONE normally. Time **audible speech inside the immersive session**, target 10 minutes; stop earlier if speech fails. Note whether game and speech coexist, game volume changes, or either pauses. Do not change other apps' settings/data to force success.
3. Return to the 2D probe. Note whether speech continued, paused/resumed or stopped. Press **Stop**, verify silence and notification removal. No automatic restart after permanent focus loss; return and Start for a new attempt.

Optional app-only lifecycle/focus evidence: `adb logcat -s QuestAudioProbe:I '*:S'` (Ctrl+C ends capture). Logs contain elapsed/player-running milliseconds, focus changes and cleanup, not proof of physical audibility. Transient loss/duck pauses speech, gain resumes; permanent loss/denied focus stops the service. A game may duck under this legitimate transient-may-duck focus request; record that, do not override OS policy.

Emergency stop / rollback (only this probe):

```text
adb shell am force-stop com.livechatxr.audioprobe
adb uninstall com.livechatxr.audioprobe
```

Result: Horizon OS version ___; Population: ONE version ___; date ___; speech audible before game ___; elapsed audible in-game duration ___ / 10 min; game/speech coexistence and volume ___; focus interruption (time/change/log if available) ___; return-to-app behavior ___; Stop silences/removes notification ___; outcome PASS / FAIL / INCONCLUSIVE ___ . Record device observations, not identities.

## Build and verified artifact

From repo root in Windows PowerShell, with JDK17 and Android SDK34/build-tools34 under `%LOCALAPPDATA%\android-dev`, plus the existing disposable `debug.keystore`:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File probe/quest-audio/build.ps1
$env:QUEST_AUDIO_APK = (Resolve-Path probe/quest-audio/build/livechatxr-audio-probe.apk).Path
python -m unittest discover -s tests -p test_quest_audio.py -v
```

Build uses aapt2/javac/d8/zipalign/apksigner, no Gradle or new dependency. Output: `probe/quest-audio/build/livechatxr-audio-probe.apk`, attached to the card. Bundled PCM speech uses original test text generated offline with free installed Windows SAPI **Microsoft Zira Desktop**; optional regeneration: `powershell -NoProfile -ExecutionPolicy Bypass -File probe/quest-audio/generate-speech.ps1`. No voice model is distributed. Regeneration/toolchain/signing timestamps can change hashes; the following identifies this delivered build.

- APK SHA256: `febca010f2039b55a231c6068ba19477ff8627c4d030e71ab9965016aafc4595`.
- apksigner `verify --verbose --print-certs`: exit 0, v3 verified, one RSA2048 signer, `CN=LiveChat XR debug`; certificate SHA256 `75b775fae1a066ecb84a7952319b83bb22048434b166172cae4dc38ab2c1d411`.
- aapt2: correct package/2D MAIN+LAUNCHER, SDK29/34, nonexported `AudioService`, `mediaPlayback` type 0x2 and required foreground-service permissions. ZIP: valid dex containing both classes and exact bundled 24.03-second mono PCM speech.
- Contract: real Service compiled/executed against tiny JVM doubles; duplicate Start, Stop/destroy, transient pause/gain, permanent loss, media error, denied focus and null-intent cleanup pass. Packaged manifest/audio/dex boundary passes. This is **not** an Android runtime or Quest gameplay test.
- UI source review: native system-font controls, DESIGN.md dark/violet palette, 56dp targets, scalable text and scrolling. No emulator, Android platform-tools or connected test runtime installed on the build host: actual 2D rendering/vision inspection and Android playback remain unverified. No synthetic headset screenshot.
