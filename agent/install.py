"""Install only the task controller; reuse the existing MCP service and sessions."""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import shutil
import subprocess
import sys


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--python',default=sys.executable);args=parser.parse_args()
    root=Path(__file__).resolve().parent.parent;home=Path.home()
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    unit=home/'.config/systemd/user/codexpro-agent.service'
    launcher=home/'.local/bin/codexpro-agent'
    def write(path,text):
        path.parent.mkdir(parents=True,exist_ok=True)
        if path.exists():shutil.copy2(path,path.with_name(path.name+'.backup-'+stamp))
        path.write_text(text)
    write(unit,f'''[Unit]
Description=CodexPro autonomous task API using existing ChatGPT accounts
After=network-online.target codexpro.service

[Service]
Type=simple
WorkingDirectory={root}
ExecStart={args.python} -u -m agent.api
Environment=PATH={home}/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=PYTHONDONTWRITEBYTECODE=1
UMask=0077
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
''')
    import shlex
    write(launcher,'#!/bin/sh\ncd '+shlex.quote(str(root))+' || exit 1\nexec '+shlex.quote(args.python)+' -m agent.cli "$@"\n')
    launcher.chmod(0o755)
    subprocess.run(['systemctl','--user','daemon-reload'],check=True)
    subprocess.run(['systemctl','--user','enable','--now','codexpro-agent.service'],check=True)
    print('Installed codexpro-agent. Existing MCP/browser/research services were not restarted.')


if __name__=='__main__':main()
