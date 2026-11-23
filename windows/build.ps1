# Build and test the portable x64 distribution from a Windows checkout.
# Requires 64-bit CPython 3.12 with Tk, VS 2022 C++ Build Tools and 7-Zip.
[CmdletBinding()]
param([string]$Python = "python")

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$root = Split-Path $PSScriptRoot -Parent
$work = Join-Path $root "dist/windows-build"
$output = Join-Path $root "dist/windows-x64"
$native = Join-Path $work "native"
$venv = Join-Path $work "venv"
$venvPython = Join-Path $venv "Scripts/python.exe"
$sdkDirectory = Join-Path $work "libusb"

function Invoke-Checked([string]$Executable, [string[]]$ArgumentList) {
    & $Executable @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw "$Executable exited with code $LASTEXITCODE."
    }
}

function Get-VerifiedAsset([string]$FileName, [string]$Sha256) {
    $destination = Join-Path $work $FileName
    if (!(Test-Path -LiteralPath $destination) -or (Get-FileHash $destination).Hash -ne $Sha256) {
        Invoke-WebRequest -Uri "https://github.com/libusb/libusb/releases/download/v1.0.29/$FileName" -OutFile $destination
    }
    if ((Get-FileHash $destination).Hash -ne $Sha256) {
        throw "SHA-256 mismatch for $FileName."
    }
    return $destination
}

if ($env:OS -ne "Windows_NT") { throw "Run this build on Windows x64." }
$vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio/Installer/vswhere.exe"
$vs = & $vswhere -latest -products '*' -version '[17.0,18.0)' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (!$vs) { throw "Install Visual Studio 2022 Build Tools with the Desktop development with C++ workload." }
& "$vs/Common7/Tools/Launch-VsDevShell.ps1" -Arch amd64 -HostArch amd64 -SkipAutomaticLocation
$sevenZip = (Get-Command "7z.exe" -ErrorAction SilentlyContinue).Source
if (!$sevenZip) { $sevenZip = Join-Path $env:ProgramFiles "7-Zip/7z.exe" }
if (!(Test-Path $sevenZip)) { throw "Install 7-Zip to unpack the official libusb SDK." }

Push-Location $root
try {
    New-Item -ItemType Directory -Force $work, $output, $native, $sdkDirectory | Out-Null
    Invoke-Checked $Python @("-c", "import struct,sys,tkinter; assert sys.version_info[:2] == (3,12) and struct.calcsize('P') == 8, '64-bit Python 3.12 required'")
    Invoke-Checked $Python @("-m", "venv", $venv)
    Invoke-Checked $venvPython @("-m", "pip", "install", "--disable-pip-version-check", "pyinstaller==6.16.0", "pillow==11.3.0")

    $sdk = Get-VerifiedAsset "libusb-1.0.29.7z" "964a38152ca9a104cd00ec8d2f0617b89cd814f9b635e29763c68563d951521d"
    $usbSource = Get-VerifiedAsset "libusb-1.0.29.tar.bz2" "5977fc950f8d1395ccea9bd48c06b3f808fd3c2c961b44b0c2e6e29fc3a70a85"
    Invoke-Checked $sevenZip @("x", "-y", "-o$sdkDirectory", $sdk, "include/libusb.h", "VS2022/MS64/dll/*")
    $library = Join-Path $sdkDirectory "VS2022/MS64/dll/libusb-1.0.lib"
    $usbDll = Join-Path $native "libusb-1.0.dll"
    Copy-Item -Force "$sdkDirectory/VS2022/MS64/dll/libusb-1.0.dll" $usbDll
    $cFlags = @("/nologo", "/TC", "/std:c17", "/O2", "/MT", "/utf-8", "/I$sdkDirectory/include")
    $reader = Join-Path $native "read_usb.exe"
    $mock = Join-Path $native "usb_mock.exe"
    Invoke-Checked "cl.exe" ($cFlags + @("/Fo$native/reader.obj", "/Fe$reader", "read_usb.c", "/link", $library))
    Invoke-Checked "cl.exe" ($cFlags + @("/Fo$native/mock.obj", "/Fe$mock", "tests/universal_usb_mock.c", "/link", $library))
    $savedMock = $env:TYT_TEST_USB_MOCK
    try {
        $env:TYT_TEST_USB_MOCK = $mock
        Invoke-Checked $venvPython @("-B", "-m", "unittest", "discover", "-s", "tests", "-q")
    } finally {
        $env:TYT_TEST_USB_MOCK = $savedMock
    }

    $pack = @("-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--windowed", "--noupx",
        "--name", "TYT Reader", "--icon", "$root/macos/AppIcon.png",
        "--workpath", "$work/pyinstaller", "--specpath", $work, "--distpath", $output,
        "--add-binary", "${reader};.", "--add-binary", "${usbDll};.",
        "--add-data", "$root/macos/i18n;macos/i18n", "--add-data", "$root/macos/AppIcon.png;macos",
        "--add-data", "$root/vendor/yaml/LICENSE;vendor/yaml")
    foreach ($catalog in Get-ChildItem "$PSScriptRoot/*.tsv" -File) {
        $pack += @("--add-data", ($catalog.FullName + ";windows"))
    }
    # Absolute entry and asset paths: the generated .spec file lives in a build directory.
    $pack += "$root/windows_app.py"
    Invoke-Checked $venvPython $pack

    $app = Join-Path $output "TYT Reader"
    $licenses = Join-Path $app "licenses"
    New-Item -ItemType Directory -Force $licenses | Out-Null
    Copy-Item -Force $usbSource $licenses
    Copy-Item -Force "$root/vendor/yaml/LICENSE" "$licenses/PyYAML.txt"
    Invoke-Checked $venvPython @("-c", "import pathlib,shutil,sys; shutil.copyfile(pathlib.Path(sys.base_prefix)/'LICENSE.txt',sys.argv[1])", "$licenses/Python.txt")
    foreach ($option in @("--self-test", "--smoke-ui")) {
        $process = Start-Process -FilePath "$app/TYT Reader.exe" -ArgumentList $option -PassThru
        if (!$process.WaitForExit(120000)) {
            $process.Kill()
            throw "Packaged app timed out during $option."
        }
        if ($process.ExitCode -ne 0) { throw "Packaged app failed $option with code $($process.ExitCode)." }
    }
    $zip = Join-Path $output "TYT-Reader-Windows-x64.zip"
    Compress-Archive -Path $app -DestinationPath $zip -Force
    Get-FileHash -Algorithm SHA256 $zip
} finally {
    Pop-Location
}
