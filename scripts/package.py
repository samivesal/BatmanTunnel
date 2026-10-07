"""Create a self-contained install bundle, including full corresponding source."""
import hashlib, pathlib, tarfile
root=pathlib.Path(__file__).resolve().parent.parent
out=root/'dist'/'batmantunnel.tar.gz'
def keep(member):
    parts=pathlib.PurePosixPath(member.name).parts
    if '.git' in parts or '__pycache__' in parts or member.name.endswith('.pyc'):return None
    return member
with tarfile.open(out,'w:gz',compresslevel=6) as t:
    for name in ('batmantunnel','engine','scripts','tests','systemd','.github','README.md','LICENSE','NOTICE','install.sh'):
        t.add(root/name,arcname='BatmanTunnel/'+name,filter=keep)
    for file in (root/'dist').iterdir():
        if file.name not in ('batmantunnel.tar.gz','SHA256SUMS'):t.add(file,arcname='BatmanTunnel/dist/'+file.name,filter=keep)
    for arch in ('amd64','arm64'):
        file=root/'bin'/('batmantunnel-engine-linux-'+arch)
        if not file.exists():raise SystemExit('Build both Linux architectures before packaging')
        t.add(file,arcname='BatmanTunnel/bin/'+file.name)
(root/'dist'/'SHA256SUMS').write_text(hashlib.sha256(out.read_bytes()).hexdigest()+'  batmantunnel.tar.gz\n')
print(str(out),out.stat().st_size,'bytes')
