# NetMirror Pro Downloader

Captures HLS streams from JW Player sites and downloads them as merged MP4 files using FFmpeg.

**Developed by [Faizan Ali](https://github.com/faizanali49)**

---

## Project Structure

```
netmirror/
├── installer-files
│   ├── extension/          ← Chrome extension (load this folder in Chrome)
│   │   ├── manifest.json
│   │   ├── background.js
│   │   ├── content.js
│   │   ├── popup.html
│   │   └── popup.js
│   └── ffmpeg.exe              ← downlaod them and place there
│   └── ffprobe.exe             ← downlaod them and place there
│   └── netmirror-server.exe    ← this file will create when you run "pyinstaller" command for merging server files and then a folder 
                              created where you find this file which you  have to move there
├── backend/            ← Python Flask server
│   ├── server.py       ← main server (you need to add this)
│   └── downloader.py   ← download engine
└── README.md
└── inno.iss

```

---

## Requirements

Before you start, install these on your machine:

1. **Python 3.10+** — https://www.python.org/downloads/
2. **FFmpeg** — https://ffmpeg.org/download.html  
   After installing, make sure `ffmpeg` works from the terminal.
3. **Google Chrome** — https://www.google.com/chrome/

---

## 📸 Screenshots

<p align="center">
  <img src="https://github.com/user-attachments/assets/12e31356-26c3-409f-8a11-376286eb9c83" alt="NetMirror Downloader" width="30%" />
  &nbsp;&nbsp;&nbsp;
  <img src="https://github.com/user-attachments/assets/e0474478-910c-4afd-8ec5-640900eb253e" alt="Video Downloads with Audio" width="30%" />
  &nbsp;&nbsp;&nbsp;
  <img src="https://github.com/user-attachments/assets/18a82388-bd0f-4760-9741-4840f17724f1" alt="Network Stream Support" width="30%" />
</p>

<br>

<p align="center">
  <img src="https://github.com/user-attachments/assets/57bc7cc9-de74-486b-ba1e-72ec19544efb" alt="NetMirror Downloader Preview" width="95%" />
</p>


## Step 1 — Install Python packages

Open a terminal in the `backend/` folder and run:

```bash
pip install flask flask-cors requests
```

---

## Step 2 — Start the backend server

In the `backend/` folder run:

```bash
python server.py
```

You should see something like:

```
* Running on http://127.0.0.1:5000
```

Keep this terminal open while using the extension.

---

## Step 3 — Load the extension in Chrome

1. Open Chrome and go to: `chrome://extensions`
2. Turn on **Developer mode** (toggle in the top right corner)
3. Click **Load unpacked**
4. Select the `extension/` folder from this project
5. The NetMirror icon should appear in your Chrome toolbar

---

## Step 4 — Test it

1. Make sure the server is running (`python server.py`)
2. Open a site that uses JW Player (e.g. net11.cc or net52.cc)
3. Play a video
4. Click the NetMirror extension icon
5. You should see video resolution cards appear in the **Capture** tab
6. Select one video card and one audio card
7. Click **Download**
8. Switch to the **Downloads** tab to watch progress

---

## How to make a single installer (Inno Setup)

If you want to ship this as a one-click `.exe` installer for Windows, follow these steps.

### What you need

- **Inno Setup** — https://jrsoftware.org/isinfo.php
- Your whole project folder ready (both `extension/` and `backend/`)
- Python bundled via **PyInstaller** (see below)

---

### Step A — Bundle the Python server into a .exe using PyInstaller

Run this from inside the `backend/` folder:

```bash
pip install pyinstaller
pyinstaller --onedir --clean --noconsole --name="netmirror-server" server.py
```

This creates `backend/dist/netmirror-server.exe` — a standalone executable that does not need Python installed.

---

### Step B — Gather all files

Create a folder called `installer-files/` and put everything inside:

```
installer-files/
├── netmirror-server.exe     ← from backend/dist/
├── extension/               ← the whole extension folder
│   ├── manifest.json
│   ├── background.js
│   ├── content.js
│   ├── popup.html
│   └── popup.js
└── ffmpeg.exe               ← download from ffmpeg.org, put it here
```

---

### Step C — Create the Inno Setup script

Open Inno Setup and create a new script. Paste this template and adjust the paths:

```iss
[Setup]
AppName=NetMirror Pro Downloader
AppVersion=2.0
AppPublisher=Faizan Ali
AppPublisherURL=https://github.com/faizanali49
DefaultDirName={autopf}\NetMirror
DefaultGroupName=NetMirror
OutputDir=output
OutputBaseFilename=NetMirror-Setup
Compression=lzma
SolidCompression=yes

[Files]
; Backend server executable
Source: "installer-files\netmirror-server.exe"; DestDir: "{app}"; Flags: ignoreversion

; Backend server dependencies (CRITICAL FIX)
Source: "installer-files\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs

; FFmpeg binary
Source: "installer-files\ffmpeg.exe"; DestDir: "{app}"; Flags: ignoreversion

; Chrome extension files
Source: "installer-files\extension\*"; DestDir: "{app}\extension"; Flags: ignoreversion recursesubdirs

[Icons]
Name: "{group}\Start NetMirror Server"; Filename: "{app}\netmirror-server.exe"

Name: "{group}\Open Extension Folder"; Filename: "{app}\extension"

Name: "{group}\Uninstall NetMirror"; Filename: "{uninstallexe}"

Name: "{commondesktop}\NetMirror Server"; Filename: "{app}\netmirror-server.exe"

Name: "{commondesktop}\NetMirror Extension"; Filename: "{app}\extension"

[Run]
Filename: "{app}\netmirror-server.exe"; Description: "Start NetMirror Server now"; Flags: postinstall nowait skipifsilent

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "NetMirrorServer"; ValueData: """{app}\netmirror-server.exe"""; Flags: uninsdeletevalue

[Code]
// So here we can add custom Pascal script here if needed

```

---

### Step D — Add a post-install message

After the installer runs, the user still needs to load the extension manually.
Add this to the `[Run]` section to open a help page:

```iss
[Run]
Filename: "{app}\netmirror-server.exe"; Flags: nowait postinstall
Filename: "chrome://extensions"; Description: "Open Chrome Extensions page to load the extension"; Flags: postinstall shellexec skipifsilent
```

Or add a `[Messages]` override to show manual instructions at the end.

---

### Step E — Compile and test

1. In Inno Setup, go to **Build → Compile**
2. The `.exe` installer appears in the `output/` folder
3. Run it on a clean machine to test

---

## FAQ

**Server shows "port already in use"**  
Another process is using port 5000. Either kill it or edit `server.py` to use a different port, and update the `API` constant at the top of `popup.js` to match.

**Extension shows "server offline"**  
The Python server is not running. Go to `backend/` and run `python server.py`.

**No video cards appear after playing a video**  
The site might use a non-standard player. Check the **Network** tab in the extension popup to see if any `.m3u8` URLs were captured.

**FFmpeg not found error in server**  
Make sure `ffmpeg` is on your system PATH, or place `ffmpeg.exe` in the same folder as `server.py` and update the FFmpeg call in `server.py` to use `./ffmpeg`.

---

## License

Free to use but do not forget to give a star 😊.
