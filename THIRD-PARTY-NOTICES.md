# Third-party components

This source repository does not include the Python runtime, Tcl/Tk, tkinterdnd2 package, PyInstaller bootloader, FFmpeg, or ffprobe binaries. Installing dependencies or producing a packaged application introduces those separate components.

- **Python / Tcl/Tk:** the GUI uses the Python standard library and Tkinter. The existing bundle's Python license text is preserved in `licenses/Python-LICENSE.txt`. Review [Python's license information](https://docs.python.org/3/license.html) and the notices accompanying the actual runtime you redistribute.
- **tkinterdnd2 / tkdnd:** optional drag-and-drop package. The existing bundle's tkinterdnd2 notice is preserved in `licenses/tkinterdnd2-LICENSE.txt`. Obtain the package and its notices from [tkinterdnd2](https://github.com/Eliav2/tkinterdnd2); its underlying Tk extension is [tkdnd](https://github.com/petasis/tkdnd).
- **PyInstaller:** a development dependency used to package Windows builds. See [PyInstaller's license and bootloader exception](https://pyinstaller.org/en/stable/license.html).
- **FFmpeg / ffprobe:** separate command-line programs used by the app. The runtime downloader currently prefers [Gyan's 7.1.1 full build](https://github.com/GyanD/codexffmpeg/releases/tag/7.1.1), with [BtbN builds](https://github.com/BtbN/FFmpeg-Builds) as fallbacks. Source and licensing information is available from [FFmpeg](https://ffmpeg.org/legal.html) and the chosen build provider. The application's original bundle describes its pinned full build as GPL version 3 or later. Review the exact build, corresponding source, notices, and redistribution requirements before attaching binaries to a release.

These notices identify dependencies; they do not change those components' terms or establish that a future binary package is ready for redistribution. The original application source is licensed under GPL-3.0-only; see `LICENSE`.
