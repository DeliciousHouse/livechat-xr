# Same no-Gradle toolchain as the read-only mobile probe scaffolding.
$ErrorActionPreference = "Stop"
$dev = "$env:LOCALAPPDATA\android-dev"
$bt = "$dev\sdk\build-tools\34.0.0"
$jar = "$dev\sdk\platforms\android-34\android.jar"
$jdk = (Get-ChildItem $dev -Directory -Filter "jdk-17*" | Select-Object -First 1).FullName
if (-not $jdk) { throw "Install JDK17 under $dev" }
$env:JAVA_HOME = $jdk
$env:Path = "$jdk\bin;$env:Path"
Set-Location $PSScriptRoot
$out = Join-Path $PSScriptRoot "build"
# Only generated files inside this probe's ignored build directory.
if (Test-Path $out) { Get-ChildItem $out -Recurse -File | ForEach-Object { $_.Delete() } }
New-Item -ItemType Directory -Force "$out\gen", "$out\classes", "$out\dex" | Out-Null
& "$bt\aapt2.exe" compile --dir res -o "$out\res.zip"
if ($LASTEXITCODE) { throw "aapt2 compile failed" }
& "$bt\aapt2.exe" link -I $jar --manifest AndroidManifest.xml -o "$out\unsigned.apk" "$out\res.zip" --java "$out\gen"
if ($LASTEXITCODE) { throw "aapt2 link failed" }
$src = @(Get-ChildItem -Recurse src, "$out\gen" -Filter *.java | ForEach-Object FullName)
& javac -source 11 -target 11 -encoding UTF-8 -classpath $jar -d "$out\classes" @src
if ($LASTEXITCODE) { throw "javac failed" }
& "$bt\d8.bat" --min-api 29 --lib $jar --output "$out\dex" @(Get-ChildItem -Recurse "$out\classes" -Filter *.class | ForEach-Object FullName)
if ($LASTEXITCODE) { throw "d8 failed" }
Push-Location "$out\dex"
try { & jar uf ..\unsigned.apk classes.dex; if ($LASTEXITCODE) { throw "jar failed" } } finally { Pop-Location }
& "$bt\zipalign.exe" -p -f 4 "$out\unsigned.apk" "$out\aligned.apk"
if ($LASTEXITCODE) { throw "zipalign failed" }
# Existing local disposable Android debug key; never a release/Store key.
$ks = "$dev\debug.keystore"
if (-not (Test-Path $ks)) { throw "Missing existing debug.keystore in $dev" }
& "$bt\apksigner.bat" sign --ks $ks --ks-pass pass:android --out "$out\livechatxr-audio-probe.apk" "$out\aligned.apk"
if ($LASTEXITCODE) { throw "apksigner sign failed" }
& "$bt\apksigner.bat" verify --verbose "$out\livechatxr-audio-probe.apk"
if ($LASTEXITCODE) { throw "apksigner verify failed" }
"built: $out\livechatxr-audio-probe.apk"
