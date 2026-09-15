"""Start the local server and open the page only after health checks succeed."""
from __future__ import annotations
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

BASE=Path(__file__).resolve().parent
URL='http://127.0.0.1:8423'
VERSION='50.1'

def health():
    try:
        with urllib.request.urlopen(URL+'/health',timeout=1) as response:
            return json.load(response)
    except (OSError,ValueError,urllib.error.URLError):return None

def main():
    existing=health()
    if existing:
        if existing.get('version')==VERSION:
            webbrowser.open(URL+'/?build='+VERSION)
            print('The app is already running at '+URL);return 0
        print('Port 8423 is used by another version. Close its console window and run START_WINDOWS.cmd again.')
        return 1
    process=subprocess.Popen([sys.executable,'-m','uvicorn','app.v42_main:app','--host','127.0.0.1','--port','8423','--no-access-log'],cwd=BASE)
    try:
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            if process.poll() is not None:return process.returncode or 1
            data=health()
            if data and data.get('version')==VERSION:
                webbrowser.open(URL+'/?build='+VERSION)
                print('Keep this window open. Press Ctrl+C to stop.')
                return process.wait()
            time.sleep(.25)
        print('Server did not become ready. See the error above.')
        return 1
    except KeyboardInterrupt:return 0
    finally:
        if process.poll() is None:
            process.terminate()
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:process.kill()

if __name__=='__main__':raise SystemExit(main())
