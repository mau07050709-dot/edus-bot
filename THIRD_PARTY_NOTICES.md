# Third-party notices and distribution review

Dependencies are installed separately from requirements files. This source export does not vendor their code, browser binaries or executable bundles.

| Component | License reported by the installed distribution | Upstream information |
| --- | --- | --- |
| PySide6-Essentials and Shiboken6 | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only | https://doc.qt.io/qtforpython-6/licenses.html |
| Playwright Python | Apache-2.0 | https://github.com/microsoft/playwright-python/blob/main/LICENSE |
| keyring | MIT | https://github.com/jaraco/keyring |
| tzdata Python package | Apache-2.0 | https://github.com/python/tzdata |
| PyInstaller, optional build tool | GPL-2.0-or-later with its distribution exception; some files under Apache-2.0 | https://pyinstaller.org/en/stable/license.html |

Version-specific notices and transitive dependency licenses must be retained when distributing binaries. Qt distribution obligations require particular attention. Using PyInstaller's exception does not remove obligations imposed by bundled dependencies. Brave is obtained separately under its own terms.

## Included assets

The generic `calendario.ico` icon and synthesized `diana.wav` alert were generated during development of this project. They are application resources rather than CCSS artwork. Private screenshots and personal background images were excluded.

## Application source

No LICENSE file is provided because the owner has not chosen the application's public license. Third-party notices do not grant rights to the application source. Obtain the owner's license selection before a public release intended for reuse.
