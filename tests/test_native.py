import contextlib, hashlib, hmac, json, os, pathlib, secrets, socket, struct, subprocess, tempfile, threading, time, unittest
from batmantunnel.common import *
from batmantunnel.runtime import TCPServer, UDPServer, EchoTCP, EchoUDP, Agent

@contextlib.contextmanager
def echo(secret):
    t=TCPServer(('127.0.0.1',0),EchoTCP);port=t.server_address[1]
    u=UDPServer(('127.0.0.1',port),EchoUDP)
    for s in (t,u):s.secret=secret;threading.Thread(target=s.serve_forever,daemon=True).start()
    try:yield port
    finally:
        for s in (t,u):s.shutdown();s.server_close()

def probe(port,secret,proto='tcp',connection=None):
    payload=secrets.token_bytes(300);packet=struct.pack('!I',len(payload))+hmac.digest(secret,b'probe/'+payload,'sha256')+payload
    if proto=='tcp':
        if connection:
            connection.sendall(packet);response=recv_exact(connection,len(payload)+32)
        else:
            with socket.create_connection(('127.0.0.1',port),timeout=1) as s:s.sendall(packet);response=recv_exact(s,len(payload)+32)
    else:
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as s:
            s.settimeout(1);s.sendto(packet,('127.0.0.1',port));response=s.recv(65535)
    assert response[32:]==payload and hmac.compare_digest(response[:32],hmac.digest(secret,b'reply/'+payload,'sha256'))

def free_port():
    while True:
        with socket.socket() as s:
            s.bind(('127.0.0.1',0));p=s.getsockname()[1]
            if 33000<=p<=59999:return p

def wait_probe(port,secret,proto='tcp',seconds=10):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        try:probe(port,secret,proto);return
        except (OSError,AssertionError,ConnectionError):time.sleep(.2)
    probe(port,secret,proto)

class ConfigurationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.plan=create_plan('127.0.0.1','127.0.0.2',[{'listen':'0.0.0.0:8443','target':'127.0.0.1:8444','protocol':'both'}])
    def test_pair_roundtrip_and_bounded_decode(self):
        self.assertEqual(decode_link(encode_link(self.plan)),self.plan)
        with self.assertRaises(ValueError):decode_link('batman://2.'+'a'*50000)
        with self.assertRaises(ValueError):decode_link('batman://2.broken')
        p=copy.deepcopy(self.plan);p['local_base']=p['carrier_base']
        with self.assertRaises(ValueError):validate_plan(p)
    def test_all_configs_both_roles(self):
        p=copy.deepcopy(self.plan);p.update(spoof_iran='192.0.2.1',spoof_abroad='192.0.2.2')
        with tempfile.TemporaryDirectory() as d:
            for role in ('iran','abroad'):
                configs=provision(p,role,pathlib.Path(d)/role)
                self.assertEqual(len(configs),18)
                self.assertTrue(all(r.get('config') for r in configs.values()))
    def test_traffic_compatibility(self):
        rr=routes(self.plan)
        self.assertEqual([r['id'] for r in rr if not compatible(self.plan,r)],['reverse-udp'])
        self.assertTrue(next(r for r in rr if r['id']=='direct-spoof')['skip'])
    def test_echo_authentication(self):
        with echo(b'right-secret') as port:
            probe(port,b'right-secret');probe(port,b'right-secret','udp')
            with self.assertRaises((OSError,ConnectionError)):probe(port,b'wrong-secret')
            with self.assertRaises(OSError):probe(port,b'wrong-secret','udp')
    def test_stable_entrypoint_switch(self):
        with tempfile.TemporaryDirectory() as d,echo(b'first') as first,echo(b'second') as second:
            port=free_port();path=pathlib.Path(d)/'relay.json'
            c={'listeners':[{'bind':f'127.0.0.1:{port}','key':'0','protocol':'both'}],'targets':{'0':f'127.0.0.1:{first}'},'generation':'1'}
            atomic_json(path,c);p=subprocess.Popen([str(ENGINE),'batman-relay',str(path)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            try:
                wait_probe(port,b'first');probe(port,b'first','udp')
                old=socket.create_connection(('127.0.0.1',port),timeout=2);probe(port,b'first',connection=old)
                c['targets']['0']=f'127.0.0.1:{second}';c['generation']='2';atomic_json(path,c);time.sleep(.5)
                probe(port,b'second');probe(port,b'second','udp');probe(port,b'first',connection=old);old.close()
            finally:p.terminate();p.wait(timeout=5)

class NativeTransportTests(unittest.TestCase):
    def test_reverse_transfers(self):
        # Native processes on localhost exercise actual BackPack handshakes and forwarding.
        failures=[]
        with tempfile.TemporaryDirectory() as d,echo(b'native-transfer') as destination:
            for transport in ('tcp','tcpmux','stealth','kcp','quic','ws','wsmux','wss','wssmux','udp'):
                carrier=free_port();entry=free_port();procs=[];logs=[]
                try:
                    for role in ('iran','abroad'):
                        req={'kind':'reverse','transport':transport,'role':role,'name':role+'-'+transport,'addr':f'127.0.0.1:{carrier}','token':'a'*64,'ports':[f'127.0.0.1:{entry}=127.0.0.1:{destination}'],'accept_udp':True}
                        cfg=pathlib.Path(d)/(role+'-'+transport+'.toml')
                        cfg.write_text(subprocess.check_output([str(ENGINE),'batman-render'],input=json.dumps(req).encode()).decode())
                        log=open(pathlib.Path(d)/(role+'-'+transport+'.log'),'wb');logs.append(log)
                        procs.append(subprocess.Popen([str(ENGINE),'-c',str(cfg)],stdout=log,stderr=log))
                    if transport!='udp':wait_probe(entry,b'native-transfer')
                    wait_probe(entry,b'native-transfer','udp')
                    print('Native transfer passed:',transport,flush=True)
                except Exception as e:
                    failures.append(transport+': '+str(e))
                    for role in ('iran','abroad'):
                        print((pathlib.Path(d)/(role+'-'+transport+'.log')).read_text()[-2000:],flush=True)
                finally:
                    for p in procs:
                        if p.poll() is None:p.terminate()
                    for p in procs:
                        try:p.wait(timeout=3)
                        except subprocess.TimeoutExpired:p.kill();p.wait()
                    for f in logs:f.close()
        self.assertEqual(failures,[])

if __name__=='__main__':unittest.main(verbosity=2)
