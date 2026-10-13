"""Install the pinned Playwright Chrome from Google's official distribution.

Only a CI/development dependency. No browser or Node dependency is added to the
add-on image. The browser version comes from the npm integrity-pinned manifest.
"""
import json
import os
import platform
from pathlib import Path
import re
import tempfile
import urllib.request
import zipfile


def main():
    if platform.system() != 'Linux' or platform.machine() not in ('x86_64', 'amd64'):
        raise SystemExit('This test installer requires Linux amd64')
    manifest = Path(__file__).resolve().parents[1] / 'node_modules/playwright-core/browsers.json'
    browser = next(b for b in json.loads(manifest.read_text())['browsers'] if b['name'] == 'chromium-headless-shell')
    version = browser['browserVersion']
    if not re.fullmatch(r'\d+\.\d+\.\d+\.\d+', version):
        raise ValueError('Invalid pinned browser version')
    directory = Path(tempfile.mkdtemp(prefix='haas-ui-browser-'))
    archive = directory / 'browser.zip'
    url = f'https://storage.googleapis.com/chrome-for-testing-public/{version}/linux64/chrome-headless-shell-linux64.zip'
    with urllib.request.urlopen(url, timeout=60) as response, archive.open('wb') as output:
        while chunk := response.read(1024 * 1024):
            output.write(chunk)
    with zipfile.ZipFile(archive) as source:
        if any(not name.startswith('chrome-headless-shell-linux64/') or '..' in Path(name).parts for name in source.namelist()):
            raise ValueError('Unexpected browser archive paths')
        source.extractall(directory)
    archive.unlink()
    executable = directory / 'chrome-headless-shell-linux64/chrome-headless-shell'
    executable.chmod(0o755)
    print(f'UI_CHROMIUM_EXECUTABLE={executable}')
    if os.environ.get('GITHUB_ENV'):
        with open(os.environ['GITHUB_ENV'], 'a') as output:
            output.write(f'UI_CHROMIUM_EXECUTABLE={executable}\n')


if __name__ == '__main__':
    main()
