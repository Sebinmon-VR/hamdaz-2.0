"""Zip Nova's Teams app package with the bot's id filled in.

    python teams-nova/make_package.py <the Nova app registration's id>

Writes teams-nova/nova-teams-app.zip — the file to upload in Teams (Apps →
Manage your apps → Upload an app). The id is the Azure Bot's Microsoft App ID,
the same as NOVA_APP_ID; the Teams app uses it as its own id too.
"""

import re
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main(app_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", app_id):
        raise SystemExit(f"{app_id!r} doesn't look like an app id (a GUID)")
    manifest = (HERE / "manifest.json").read_text(encoding="utf-8")
    out = HERE / "nova-teams-app.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as package:
        package.writestr("manifest.json", manifest.replace("{{NOVA_APP_ID}}", app_id.lower()))
        package.write(HERE / "color.png", "color.png")
        package.write(HERE / "outline.png", "outline.png")
    return out


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    print("wrote", main(sys.argv[1]))
