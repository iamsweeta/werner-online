"""Start the local server and open the page only after health checks succeed."""
from __future__ import annotations
import json
import hashlib
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

BASE=Path(__file__).resolve().parent
URL='http://127.0.0.1:8423'
VERSION='60.0'
INSTALLATION_ID=hashlib.sha256(str(BASE.resolve()).encode()).hexdigest()[:16]

def health(url=URL):
    try:
        with urllib.request.urlopen(url+'/health',timeout=1) as response:
            return json.load(response)
    except (OSError,ValueError,urllib.error.URLError):return None

def select_port():
    free_port=None
    for port in range(8423,8444):
        url=f'http://127.0.0.1:{port}'
        with socket.socket() as sock:
            try:sock.bind(('127.0.0.1',port))
            except OSError:
                existing=health(url)
                if existing and existing.get('version')==VERSION and existing.get('installation_id')==INSTALLATION_ID:
                    return port,True
            else:
                if free_port is None:free_port=port
    if free_port is not None:return free_port,False
    raise RuntimeError('No free local port in 8423–8443. Close an old app window and try again.')


def main():
    port,running=select_port();url=f'http://127.0.0.1:{port}'
    browser_url=url+'/?build='+VERSION+'&theme=light'
    if running:
        webbrowser.open(browser_url)
        print('This installation is already running at '+url);return 0
    process=subprocess.Popen([sys.executable,'-m','uvicorn','app.v42_main:app','--host','127.0.0.1','--port',str(port),'--no-access-log'],cwd=BASE)
    try:
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            if process.poll() is not None:return process.returncode or 1
            data=health(url)
            if data and data.get('version')==VERSION and data.get('installation_id')==INSTALLATION_ID:
                webbrowser.open(browser_url)
                print('Running '+VERSION+' at '+url+'. Keep this window open. Press Ctrl+C to stop.')
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

def diagnostics():
    from urllib.parse import urlencode
    port,running=select_port()
    if not running:
        print('Start this installation with START_WINDOWS.cmd first.');return 1
    query=urlencode({'origin':'Санкт-Петербург','destination':'Москва'})
    webbrowser.open(f'http://127.0.0.1:{port}/api/diagnostics?'+query)
    return 0


if __name__=='__main__':raise SystemExit(diagnostics() if '--diagnostics' in sys.argv else main())
