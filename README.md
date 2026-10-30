# Media Studio

A Windows desktop app for converting video, audio, and pictures, and compressing individual media files or whole folders toward a chosen size. The interface uses Python's Tkinter; FFmpeg performs the media processing.

## Run from source

Use a normal Windows Python installation with Tcl/Tk. The source was checked with Python 3.14. A Python installer from [python.org](https://www.python.org/downloads/windows/) includes Tkinter when its Tcl/Tk component is selected.

Open PowerShell in this folder:

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe media_studio.py
```

`tkinterdnd2` adds drag and drop. The app also starts without that optional package.

On Windows, the app offers to download FFmpeg and ffprobe when it cannot find them. It checks beside the app, in `%LOCALAPPDATA%\MediaStudio\ffmpeg`, and on `PATH`. You can instead obtain both tools yourself and place them beside `media_studio.py` or on `PATH`. The app's download code tries a pinned Gyan 7.1.1 build first and verifies that archive's SHA-256, then tries BtbN fallback builds. Fallback archives do not currently have a pinned hash.

## Use

- Convert a video, audio, or picture file using the conversion tab.
- Compress a video or audio file toward a target size. The default target is 9.5 MiB.
- Process supported files in a folder, including subfolders, using the folder tabs.
- Choose an output location and review overwrite prompts before starting.

Codec support and hardware acceleration depend on the FFmpeg build, GPU, and drivers available on the computer. Keep originals until you have checked the result.

## Build a Windows app

The checked development tool versions are recorded separately:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean 'Media Studio.spec'
```

The app is written to `dist\Media Studio\Media Studio.exe`. Copy the entire `dist\Media Studio` folder when sharing a portable build. FFmpeg is not included by this source build recipe; the app can find or download it at runtime.

For an optional per-user installer, install Inno Setup 7 and compile `Media Studio installer.iss` after building the portable folder. Its output is `dist\Media Studio Setup.exe`. The script includes every file in the portable folder: review that folder and the third-party notices before distributing an installer.

## Repository contents

This repository contains application source, build recipes, and notices. Downloaded tools, converted media, installed copies, backups, and generated installers are excluded. A repository download is source code; it is not an installer.

## License

The original application code is licensed under [GNU GPL version 3 only](LICENSE) (`GPL-3.0-only`). See [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md) for separate dependencies. Included third-party license texts apply to their respective components.
