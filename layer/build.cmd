@echo off
rem Build livechat_xr_layer.dll. Needs the MSVC x64 tools (VS 2022 Build Tools); in CI the env is already set.
setlocal
where cl >nul 2>nul || call "%ProgramFiles(x86)%\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat" >nul || exit /b 1
cd /d "%~dp0"
cl /nologo /std:c++17 /O2 /MT /EHsc /W3 /D_CRT_SECURE_NO_WARNINGS /LD /Iinclude livechat_xr_layer.cpp /Fe:livechat_xr_layer.dll /link d3d11.lib gdi32.lib user32.lib
