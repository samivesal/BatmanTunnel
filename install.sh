#!/usr/bin/env bash
set -euo pipefail
REPOSITORY="${BATMAN_REPOSITORY:-samivesal/BatmanTunnel}"
RELEASE="${BATMAN_RELEASE:-v0.2.0}"
SOURCE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || true)"
INSTALL_ROOT=/opt/batmantunnel
if [[ ${EUID:-$(id -u)} -ne 0 ]]; then echo 'Run this installer as root.' >&2; exit 1; fi
if [[ $(uname -s) != Linux ]]; then echo 'Linux is required.' >&2; exit 1; fi
for tool in python3 openssl systemctl ip; do command -v "$tool" >/dev/null || { echo "$tool is required. Install it using your OS package manager." >&2; exit 1; }; done
python3 -c 'import sys; assert sys.version_info >= (3,10), "Python 3.10+ required"'
TMP_ROOT="$(mktemp -d)"
trap 'python3 -c "import shutil,sys;shutil.rmtree(sys.argv[1])" "$TMP_ROOT"' EXIT
if [[ ! -f "$SOURCE/batmantunnel/cli.py" ]]; then
  [[ "$REPOSITORY" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || { echo 'Run install.sh from the extracted bundle, or set BATMAN_REPOSITORY to your actual GitHub owner/repository.' >&2; exit 1; }
  command -v curl >/dev/null || { echo 'curl is required.' >&2; exit 1; }
  curl --fail --show-error --silent --location --proto '=https' --tlsv1.2 "https://github.com/$REPOSITORY/releases/download/$RELEASE/batmantunnel.tar.gz" -o "$TMP_ROOT/source.tar.gz"
  curl --fail --show-error --silent --location --proto '=https' --tlsv1.2 "https://github.com/$REPOSITORY/releases/download/$RELEASE/SHA256SUMS" -o "$TMP_ROOT/SHA256SUMS"
  python3 - "$TMP_ROOT/source.tar.gz" "$TMP_ROOT/SHA256SUMS" <<'VERIFY'
import hashlib,pathlib,sys
actual=hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest()
lines=pathlib.Path(sys.argv[2]).read_text().splitlines()
expected=[line.split()[0] for line in lines if line.split()[-1]=='batmantunnel.tar.gz']
if len(expected)!=1 or actual!=expected[0]:raise SystemExit('Release checksum verification failed')
VERIFY
  python3 - "$TMP_ROOT/source.tar.gz" "$TMP_ROOT/source" <<'PY'
import pathlib,sys,tarfile
root=pathlib.Path(sys.argv[2]);root.mkdir()
with tarfile.open(sys.argv[1]) as tar:
    for member in tar.getmembers():
        target=(root/member.name).resolve()
        if not target.is_relative_to(root.resolve()) or not (member.isfile() or member.isdir()):raise SystemExit('Unsafe archive')
    tar.extractall(root,filter='data') if sys.version_info >= (3,12) else tar.extractall(root)
children=list(root.iterdir())
if len(children)!=1 or not children[0].is_dir():raise SystemExit('Invalid source layout')
PY
  SOURCE="$(find "$TMP_ROOT/source" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
fi
case $(uname -m) in x86_64) ARCH=amd64;; aarch64|arm64) ARCH=arm64;; *) echo 'Supported CPU architectures: amd64, arm64.' >&2; exit 1;; esac
NATIVE="$SOURCE/bin/batmantunnel-engine-linux-$ARCH"
if [[ ! -f "$NATIVE" ]]; then
  # Build from the included corresponding source if a release binary is absent.
  command -v go >/dev/null || { echo 'No binary for this architecture. Install Go 1.26.6+ and run again, or download a complete release bundle.' >&2; exit 1; }
  PATH="$PATH" bash "$SOURCE/scripts/build-engine.sh"
  NATIVE="$SOURCE/bin/batmantunnel-engine"
fi
python3 -m compileall -q "$SOURCE/batmantunnel"
# An existing pair is preserved. Keep the previous code for local rollback.
if [[ -d "$INSTALL_ROOT" && "$SOURCE" != "$INSTALL_ROOT" ]]; then
  BACKUP="${INSTALL_ROOT}.previous.$(date +%s)"
  cp -a "$INSTALL_ROOT" "$BACKUP"
fi
systemctl stop batmantunnel 2>/dev/null || true
mkdir -p "$INSTALL_ROOT" /etc/batmantunnel
chmod 700 /etc/batmantunnel
python3 - "$SOURCE" "$INSTALL_ROOT" "$NATIVE" <<'PY'
import pathlib,shutil,sys
src,dst,native=map(pathlib.Path,sys.argv[1:])
if src.resolve()!=dst.resolve():
    for name in ('batmantunnel','engine','dist','scripts','systemd'):
        shutil.copytree(src/name,dst/name,dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__','.git','batmantunnel.tar.gz'))
    for name in ('README.md','LICENSE','NOTICE','install.sh'):shutil.copy2(src/name,dst/name)
(dst/'bin').mkdir(exist_ok=True)
if native.resolve()!=(dst/'bin/batmantunnel-engine').resolve():shutil.copy2(native,dst/'bin/batmantunnel-engine')
(dst/'bin/batmantunnel-engine').chmod(0o755)
# A copy of the complete corresponding source is offered by the local panel.
import tarfile
with tarfile.open(dst/'dist/batmantunnel.tar.gz','w:gz') as t:
    for name in ('batmantunnel','engine','scripts','systemd','README.md','LICENSE','NOTICE','install.sh'):
        t.add(dst/name,arcname='BatmanTunnel/'+name,filter=lambda m: None if '__pycache__' in m.name else m)
    for item in (dst/'dist').iterdir():
        if item.name!='batmantunnel.tar.gz':t.add(item,arcname='BatmanTunnel/dist/'+item.name)
PY
cat > /usr/local/bin/batmantunnel <<'WRAPPER'
#!/usr/bin/env bash
set -euo pipefail
cd /opt/batmantunnel
exec /usr/bin/python3 -m batmantunnel.cli "$@"
WRAPPER
chmod 755 /usr/local/bin/batmantunnel
cp "$SOURCE/systemd/batmantunnel.service" /etc/systemd/system/batmantunnel.service
systemctl daemon-reload
if [[ -f /etc/batmantunnel/node.json ]]; then
  chown -R root:root /etc/batmantunnel
  systemctl enable --now batmantunnel
  echo 'Updated BatmanTunnel. Existing pairing and settings were preserved.'
else
  echo 'Installed BatmanTunnel. Run: batmantunnel'
  echo 'On the Iran server choose Setup Iran; on the abroad server choose Setup Abroad.'
fi
