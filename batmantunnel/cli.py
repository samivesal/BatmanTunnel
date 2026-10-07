"""English terminal setup for a complete paired BatmanTunnel deployment."""
import argparse, getpass, http.client, json, os, pathlib, secrets, signal, subprocess, sys, time
from .common import *
from . import VERSION

def root_required():
    if os.geteuid()!=0:raise ValueError('Run this command with sudo or as root.')

def ask(label,default='',validate=None):
    while True:
        value=input(label+(f' [{default}]' if default else '')+': ').strip() or default
        try:return validate(value) if validate else value
        except (ValueError,TypeError):print('Invalid value. Please try again.')

def port(value):
    n=int(value)
    if not 1024<=n<=60000:raise ValueError('Use a port between 1024 and 60000')
    return n

def setup(role,directory):
    root_required();path=directory/'node.json'
    if path.exists():
        print('This node is already configured. Back up its configuration before replacing it.')
        if input('Type REPLACE to stop and replace this pair: ').strip()!='REPLACE':return
    if role=='iran':
        print('\nSETUP IRAN — public entrypoint\nUse the public IPv4 addresses of your two servers.')
        iran=ask('Iran public IPv4',validate=ipv4);abroad=ask('Abroad public IPv4',validate=ipv4)
        mappings=[]
        while len(mappings)<8:
            listen=ask('Iran public service port','443',lambda s: endpoint('0.0.0.0:'+str(int(s))))
            target=ask('Backend on abroad server (IPv4:port)','127.0.0.1:443',endpoint)
            protocol=ask('Service protocol: tcp, udp or both','tcp',lambda s:s if s in ('tcp','udp','both') else (_ for _ in ()).throw(ValueError()))
            mappings.append({'listen':f'{listen[0]}:{listen[1]}','target':f'{target[0]}:{target[1]}','protocol':protocol})
            if ask('Add another service? y/n','n').lower()!='y':break
        options={}
        if ask('Advanced port / direct carrier settings? y/n','n').lower()=='y':
            for key,label,default in [('control_port','Peer HTTPS port',9443),('carrier_base','Carrier port range start (18 ports)',23000),('local_base','Local route port range start (288 ports)',32000)]:options[key]=ask(label,str(default),port)
            options['sni']=ask('Direct SNI hostname','www.speedtest.net')
            options['spoof_iran']=ask('Iran source IPv4 for spoof (blank disables)',validate=lambda s:ipv4(s) if s else '')
            options['spoof_abroad']=ask('Abroad source IPv4 for spoof (blank disables)',validate=lambda s:ipv4(s) if s else '')
        plan=create_plan(iran,abroad,mappings,**options)
    else:
        print('\nSETUP ABROAD — service backend\nPaste the secret pairing link from the Iran server. Input is hidden.')
        plan=decode_link(getpass.getpass('Pairing link: '))
    # Validate every native configuration before stopping an existing installation.
    with tempfile.TemporaryDirectory() as temp:provision(plan,role,temp)
    subprocess.run(['systemctl','stop','batmantunnel'],check=False,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    directory.mkdir(parents=True,exist_ok=True,mode=0o700);directory.chmod(0o700)
    if path.exists():
        backup=directory/('node.backup.'+str(int(time.time()))+'.json');backup.write_bytes(path.read_bytes());backup.chmod(0o600)
    cfg={'plan':plan,'role':role,'policy':policy_defaults(),'admin_key':secrets.token_urlsafe(36),'panel_port':8787}
    atomic_json(path,cfg)
    state=directory/'runtime.json'
    if state.exists():state.unlink()
    subprocess.run(['systemctl','enable','--now','batmantunnel'],check=True)
    print('\nBatmanTunnel is running. Allow peer TCP '+str(plan['control_port'])+' and the configured carrier range on both servers.')
    print('Carrier range: '+str(plan['carrier_base'])+'–'+str(plan['carrier_base']+17)+' (TCP/UDP); raw carriers also require their IP protocols.')
    if role=='iran':
        link=encode_link(plan);pair=directory/'pair.link';pair.write_text(link+'\n');pair.chmod(0o600)
        print('\nSECRET PAIRING LINK — paste only into your abroad server:\n'+link)
        print('\nThe link is also stored at '+str(pair)+'. Keep it private.')
        print('Once the other server joins, initial measurement takes about 3–5 minutes.')
    panel_info(directory)

def load(directory):return json.loads((directory/'node.json').read_text())

def request(directory,path,body=None):
    cfg=load(directory);c=http.client.HTTPConnection('127.0.0.1',cfg.get('panel_port',8787),timeout=10)
    try:
        c.request('POST' if body is not None else 'GET','/api/'+path,None if body is None else json.dumps(body),{'Authorization':'Bearer '+cfg['admin_key'],'Content-Type':'application/json'})
        r=c.getresponse();data=json.loads(r.read())
        if r.status>=400:raise ValueError(data.get('error','Controller request failed'))
        return data
    finally:c.close()

def status(directory):
    s=request(directory,'status')
    print('\nBatmanTunnel '+VERSION+' | '+s['role'].upper())
    print('Peer: '+('connected' if s['peer_connected'] else 'waiting / unreachable'))
    print('Active: '+str(s['active'] or 'waiting for a verified route'))
    print('Comparison: '+(str(s['test_progress'])+'%' if s['testing'] else 'idle'))
    print('Auto selection: '+str(s['policy']['auto'])+' | interval: '+str(s['policy']['test_interval']//3600)+' hours\n')
    for r in s['profiles']:
        detail=r['skip'] or ('incompatible with TCP service' if not r['compatible'] else ('verified' if r['last'] and r['last']['ok'] else 'not verified'))
        print(f"{r['id']:18} {r['score']:6.1f}  {'running' if r['running'] else 'stopped':7}  {detail}")

def panel_info(directory):
    cfg=load(directory);role=cfg['role'];remote=cfg['plan'][role];p=cfg.get('panel_port',8787)
    print('\nFrom your computer, run:\n  ssh -L 8787:127.0.0.1:'+str(p)+' root@'+remote)
    print('Then open http://127.0.0.1:8787\nAdmin key: '+cfg['admin_key'])

def main():
    parser=argparse.ArgumentParser(description='BatmanTunnel paired adaptive tunnel manager')
    parser.add_argument('command',nargs='?',default='menu',choices=['menu','setup-iran','setup-abroad','run','status','test','policy','panel','pair-link','logs','restart','stop','version'])
    parser.add_argument('--directory',type=pathlib.Path,default=DEFAULT_DIR)
    parser.add_argument('--interval',type=int,choices=[1,3,6,12]);parser.add_argument('--auto',choices=['on','off'])
    args=parser.parse_args();directory=args.directory
    try:
        cmd=args.command
        if cmd=='version':print(VERSION);return
        root_required()
        if cmd=='menu':
            while True:
                print('\nBATMANTUNNEL / '+VERSION+'\n1. Setup Iran\n2. Setup Abroad\n3. Status & protocols\n4. Test all routes now\n5. Automatic selection policy\n6. Open panel / admin key\n7. Show pairing link\n8. View service logs\n9. Restart service\n0. Exit')
                choice=ask('Select','3')
                if choice=='0':return
                commands={'1':'setup-iran','2':'setup-abroad','3':'status','4':'test','5':'policy','6':'panel','7':'pair-link','8':'logs','9':'restart'}
                if choice not in commands:continue
                extra=[]
                if choice=='5':extra=['--interval',ask('Compare every 1, 3, 6 or 12 hours','3'),'--auto',ask('Automatic switching: on/off','on')]
                subprocess.run([sys.executable,'-m','batmantunnel.cli',commands[choice],'--directory',str(directory)]+extra)
        elif cmd.startswith('setup-'):setup(cmd[6:],directory)
        elif cmd=='run':
            from .runtime import Agent
            a=Agent(directory)
            signal.signal(signal.SIGTERM,lambda *_:a.stop.set());signal.signal(signal.SIGINT,lambda *_:a.stop.set());a.run()
        elif cmd=='status':status(directory)
        elif cmd=='test':request(directory,'test',{});print('Native route comparison requested. Use status to follow progress.')
        elif cmd=='policy':
            data={}
            if args.interval:data['test_interval']=args.interval*3600
            if args.auto:data['auto']=args.auto=='on'
            print(json.dumps(request(directory,'policy',data)['policy'],indent=2))
        elif cmd=='panel':panel_info(directory)
        elif cmd=='pair-link':print(encode_link(load(directory)['plan']))
        elif cmd=='logs':subprocess.run(['journalctl','-u','batmantunnel','-n','80','--no-pager'])
        elif cmd in ('restart','stop'):subprocess.run(['systemctl',cmd,'batmantunnel'],check=True)
    except (ValueError,OSError,KeyError,subprocess.SubprocessError) as e:
        print('Error: '+str(e),file=sys.stderr);sys.exit(1)
    except KeyboardInterrupt:print('\nCancelled.')

if __name__=='__main__':main()
