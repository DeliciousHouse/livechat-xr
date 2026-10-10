# Free offline Windows SAPI voice; original probe text, no API/network.
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Speech
$speaker = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $speaker.SelectVoice("Microsoft Zira Desktop")
    $speaker.Rate = -1
    $speaker.SetOutputToWaveFile((Join-Path $PSScriptRoot "res\raw\speech.wav"))
    $speaker.Speak("Live Chat X R audio probe. One. Two. Three. Four. Five. Speech should remain audible beside game audio. Six. Seven. Eight. Nine. Ten. This numbered test now repeats.")
} finally { $speaker.Dispose() }
