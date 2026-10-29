@echo off
call "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat" >nul 2>&1
cd /d "%~dp0"
msbuild SampleIME.vcxproj /p:Configuration=Release /p:Platform=x64 /p:PlatformToolset=v143 /m /v:m %*
