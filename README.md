# ReEncode

Desktop GUI for scanning media, checking codecs, estimating savings, and converting files.

## Requirements

- Python 3.12+
- ffmpeg and ffprobe in PATH

## Setup

1. Create and activate a virtual environment.

Windows (PowerShell):

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Linux (bash):

```bash
python3 -m venv .venv
source .venv/bin/activate
```

2. Install dependencies.

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

3. Install FFmpeg.

Windows:

```powershell
winget install Gyan.FFmpeg
```

Linux (Ubuntu/Debian):

```bash
sudo apt update
sudo apt install -y ffmpeg
```

4. Verify tools.

```bash
ffmpeg -version
ffprobe -version
```

## Run

```bash
python main.py
```

## Build App (Windows)

```powershell
python -m pip install -r requirements-build.txt
python -m PyInstaller --noconfirm --clean ReEncode.spec
```

Output is created as `dist\ReEncode\ReEncode.exe`.

## Use

1. Add folder(s) in Sources.
2. Click Scan.
3. Review recommendations.
4. Convert selected files.
5. Check Failed tab if needed.

Converted files are created next to the original files with a reencoded suffix.

## Test

```bash
python -m unittest discover tests
```

## Troubleshooting

- ffmpeg/ffprobe not found: reinstall FFmpeg and reopen terminal/app.
- Files missing from scan: check folder permissions and selected folders.
- Conversion failure: open Failed tab for ffmpeg error details.
