import base64, copy, hashlib, hmac, ipaddress, json, os, pathlib, secrets
import socket, ssl, subprocess, tempfile, time, zlib

REVERSE = ('tcp','tcpmux','stealth','pck','udp','kcp','quic','ws','wsmux','wss','wssmux','xdi')
DIRECT = ('udp','quic','pck','xdi','sni','spoof')
ROOT = pathlib.Path(__file__).resolve().parent.parent
ENGINE = ROOT / 'bin' / 'batmantunnel-engine'
DEFAULT_DIR = pathlib.Path('/etc/batmantunnel')
MAX_LINK = 48000

def atomic_json(path, value):
    path=pathlib.Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd,tmp=tempfile.mkstemp(prefix='.'+path.name+'-',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as f:
            os.fchmod(f.fileno(),0o600);json.dump(value,f);f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)

def recv_exact(s,n):
    b=bytearray()
    while len(b)<n:
        chunk=s.recv(n-len(b))
        if not chunk:raise ConnectionError('short response')
        b.extend(chunk)
    return bytes(b)

def endpoint(text):
    host,port=str(text).rsplit(':',1)
    ipaddress.IPv4Address(host);port=int(port)
    if not 1<=port<=65535:raise ValueError('Port must be between 1 and 65535')
    return host,port

def ipv4(value):
    return str(ipaddress.IPv4Address(value))

def derive(plan, label):
    return hmac.new(plan['secret'].encode(),('batmantunnel/v2/'+label).encode(),hashlib.sha256).hexdigest()

def validate_plan(p):
    if not isinstance(p,dict):raise ValueError('Invalid pairing object')
    if not isinstance(p.get('id'),str) or len(p['id'])!=8 or any(c not in '0123456789abcdef' for c in p['id']):raise ValueError('Invalid pair ID')
    if p.get('version')!=2:raise ValueError('Unsupported pairing format')
    if not isinstance(p.get('secret'),str) or len(p['secret'])!=64:raise ValueError('Invalid pair secret')
    int(p['secret'],16)
    for key in ('iran','abroad'):ipv4(p[key])
    for key in ('control_port','echo_port','direct_service_base','carrier_base','local_base'):
        if type(p.get(key)) is not int or not 1024<=p[key]<=60000:raise ValueError('Invalid '+key)
    if p['carrier_base']+len(REVERSE)+len(DIRECT)>65535 or p['local_base']+18*16>65535:raise ValueError('Port range exceeds 65535')
    ranges=[set(range(p['carrier_base'],p['carrier_base']+18)),set(range(p['local_base'],p['local_base']+18*16)),{p['control_port']},{p['echo_port']},set(range(p['direct_service_base'],p['direct_service_base']+8)),{8787}]
    for i,ports in enumerate(ranges):
        if any(ports & other for other in ranges[i+1:]):raise ValueError('Management and tunnel port ranges overlap')
    net=ipaddress.IPv4Network(p['subnet'],strict=True)
    if net.prefixlen!=24 or not net.is_private:raise ValueError('A private /24 is required')
    if not isinstance(p.get('mappings'),list) or not 1<=len(p['mappings'])<=8:raise ValueError('Use one to eight mappings')
    seen=set()
    for m in p['mappings']:
        endpoint(m['listen']);endpoint(m['target'])
        if m['protocol'] not in ('tcp','udp','both'):raise ValueError('Invalid mapping protocol')
        if m['listen'] in seen:raise ValueError('Duplicate public listener')
        seen.add(m['listen'])
        port=endpoint(m['listen'])[1]
        if port in range(p['carrier_base'],p['carrier_base']+18) or port in range(p['local_base'],p['local_base']+18*16) or port in (p['control_port'],8787):raise ValueError('Public listener overlaps a management or tunnel port')
    if not isinstance(p.get('sni'),str) or len(p['sni'])>253 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-' for c in p['sni']):raise ValueError('Invalid SNI name')
    for role in ('iran','abroad'):
        source=p.get('spoof_'+role,'')
        if source:ipv4(source)
    for key in ('cert','key'):
        if not isinstance(p.get(key),str) or len(p[key])>12000:raise ValueError('Invalid certificate material')
    if 'BEGIN CERTIFICATE' not in p['cert'] or 'PRIVATE KEY' not in p['key']:raise ValueError('Missing certificate material')
    return p

def create_plan(iran, abroad, mappings, **options):
    secret=secrets.token_hex(32)
    p={'version':2,'secret':secret,'id':secrets.token_hex(4),'iran':ipv4(iran),'abroad':ipv4(abroad),'control_port':9443,'echo_port':19001,
       'direct_service_base':19100,'carrier_base':23000,'local_base':32000,'subnet':f'10.{160+int(secret[:2],16)%60}.{int(secret[2:4],16)}.0/24',
       'sni':'www.speedtest.net','spoof_iran':'','spoof_abroad':'','mappings':mappings}
    p.update(options)
    with tempfile.TemporaryDirectory() as temp:
        cert=pathlib.Path(temp)/'pair.crt';key=pathlib.Path(temp)/'pair.key'
        subprocess.run(['openssl','req','-x509','-newkey','ec','-pkeyopt','ec_paramgen_curve:prime256v1','-nodes','-keyout',str(key),'-out',str(cert),'-days','3650','-subj','/CN=BatmanTunnel pair'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=30)
        p['cert']=cert.read_text();p['key']=key.read_text()
    return validate_plan(p)

def encode_link(plan):
    raw=json.dumps(plan,separators=(',',':')).encode()
    return 'batman://2.'+base64.urlsafe_b64encode(zlib.compress(raw,9)).decode().rstrip('=')

def decode_link(link):
    if len(link)>MAX_LINK or not link.strip().startswith('batman://2.'):raise ValueError('Invalid BatmanTunnel pairing link')
    raw=link.strip().split('.',1)[1]
    try:
        b=base64.b64decode(raw+'='*((-len(raw))%4),altchars=b'-_',validate=True)
        d=zlib.decompressobj();body=d.decompress(b,MAX_LINK+1)
        if len(body)>MAX_LINK or not d.eof or d.unused_data:raise ValueError('Invalid compressed payload')
        p=json.loads(body)
    except (ValueError,zlib.error) as e:raise ValueError('Damaged pairing link') from e
    return validate_plan(p)

def routes(plan):
    result=[];net=ipaddress.IPv4Network(plan['subnet'])
    for i,(kind,tr) in enumerate([('reverse',x) for x in REVERSE]+[('direct',x) for x in DIRECT]):
        d=i-len(REVERSE)
        r={'id':kind+'-'+tr,'kind':kind,'transport':tr,'name':kind.title()+' '+tr.upper(),'index':i,'port':plan['carrier_base']+i,
           'probe':plan['local_base']+i*16,'control':plan['local_base']+i*16+1,'service':[plan['local_base']+i*16+2+j for j in range(len(plan['mappings']))],
           'tcp':not(kind=='reverse' and tr=='udp'),'udp':True}
        if kind=='direct':
            r.update(iran_ip=str(net.network_address+4*d+1),abroad_ip=str(net.network_address+4*d+2),interface='bt'+plan['id'][:4]+str(d))
        r['skip']=''
        if tr=='spoof' and not (plan.get('spoof_iran') and plan.get('spoof_abroad')):r['skip']='IP spoofing source addresses are not configured'
        result.append(r)
    return result

def compatible(plan,r):
    return not any(m['protocol'] in ('tcp','both') and not r['tcp'] for m in plan['mappings'])

def render_request(plan,r,role):
    ports=[];remote='abroad' if role=='iran' else 'iran'
    if role=='iran':
        target='127.0.0.1' if r['kind']=='reverse' else r['abroad_ip']
        ports.append(f"127.0.0.1:{r['probe']}={target}:{plan['echo_port']}")
        if r['tcp']:ports.append(f"127.0.0.1:{r['control']}={target}:{plan['control_port']}")
        for i,m in enumerate(plan['mappings']):
            backend=m['target'] if r['kind']=='reverse' else f"{r['abroad_ip']}:{plan['direct_service_base']+i}"
            ports.append(f"127.0.0.1:{r['service'][i]}={backend}")
    q={'kind':r['kind'],'transport':r['transport'],'role':role,'name':'bt-'+role+'-'+r['id'],'token':derive(plan,'route/'+r['id']),
       'ports':ports,'accept_udp':True,'sni':plan['sni']}
    if r['kind']=='reverse':q['addr']=f"0.0.0.0:{r['port']}" if role=='iran' else f"{plan['iran']}:{r['port']}"
    else:
        q.update(addr=f"{plan['abroad']}:{r['port']}" if role=='iran' else f"0.0.0.0:{r['port']}",local_ip=r[role+'_ip']+'/30',peer_ip=r[remote+'_ip'],interface=r['interface'],
                 spoof_source=plan.get('spoof_'+role,''),spoof_peer=plan[remote])
    return q

def provision(plan,role,directory,engine=ENGINE):
    directory=pathlib.Path(directory);directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    result={}
    for r in routes(plan):
        q=render_request(plan,r,role)
        if r['skip']:result[r['id']]=r;continue
        proc=subprocess.run([str(engine),'batman-render'],input=json.dumps(q),text=True,capture_output=True,timeout=15)
        if proc.returncode:raise RuntimeError('Could not render '+r['id']+': '+proc.stderr[-300:])
        path=directory/(role+'-'+r['id']+'.toml');path.write_text(proc.stdout);path.chmod(0o600)
        check=subprocess.run([str(engine),'check','-c',str(path)],capture_output=True,text=True,timeout=15)
        if check.returncode:raise RuntimeError('Invalid native config for '+r['id']+': '+check.stderr[-300:])
        r['config']=str(path);result[r['id']]=r
    return result

def policy_defaults():
    return {'auto':True,'test_interval':10800,'failure_threshold':3,'switch_margin':10,'soak_seconds':180,'health_interval':60,'cooldown':1800}

def validate_policy(p):
    if type(p.get('auto')) is not bool:raise ValueError('auto must be boolean')
    for k,lo,hi in [('test_interval',3600,43200),('failure_threshold',2,10),('switch_margin',5,30),('soak_seconds',30,300),('health_interval',10,120),('cooldown',60,7200)]:
        if type(p.get(k)) is not int or not lo<=p[k]<=hi:raise ValueError('Invalid '+k)
    return p
