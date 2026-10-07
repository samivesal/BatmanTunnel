"""Paired native-engine orchestration; secrets never leave the pinned TLS channel."""
import concurrent.futures, copy, hashlib, hmac, http.client, http.server, json, os
import pathlib, queue, secrets, signal, socket, socketserver, ssl, statistics
import struct, subprocess, threading, time, urllib.parse, urllib.request
from .common import *

class EnginePool:
    def __init__(self,routes_,directory,engine=ENGINE):
        self.routes=routes_;self.directory=pathlib.Path(directory);self.engine=engine
        self.lock=threading.RLock();self.processes={};self.errors={};self.retries={}
    def ensure(self,wanted):
        with self.lock:
            for rid,p in list(self.processes.items()):
                if p.poll() is not None:
                    self.errors[rid]='Native engine exited; see its log';self.processes.pop(rid)
                    self.retries[rid]=time.monotonic()+15
            for rid in wanted:
                r=self.routes[rid]
                if r['skip'] or rid in self.processes or time.monotonic()<self.retries.get(rid,0):continue
                try:
                    with open(self.directory/(rid+'.log'),'ab',buffering=0) as log:
                        self.processes[rid]=subprocess.Popen([str(self.engine),'-c',r['config']],stdout=log,stderr=log,start_new_session=True)
                    self.errors.pop(rid,None)
                except OSError as e:self.errors[rid]=str(e);self.retries[rid]=time.monotonic()+30
            for rid in set(self.processes)-set(wanted):self.stop_one(rid)
            for file in self.directory.glob('*.log'):
                if file.stat().st_size>2*1024*1024:
                    with open(file,'wb'):pass
    def stop_one(self,rid):
        p=self.processes.pop(rid,None)
        if p is None:return
        if p.poll() is None:
            try:os.killpg(p.pid,signal.SIGTERM);p.wait(timeout=4)
            except subprocess.TimeoutExpired:os.killpg(p.pid,signal.SIGKILL);p.wait(timeout=2)
            except ProcessLookupError:pass
    def running(self):
        with self.lock:return [k for k,p in self.processes.items() if p.poll() is None]
    def close(self):
        with self.lock:
            for rid in list(self.processes):self.stop_one(rid)

class EchoTCP(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(5)
        try:
            for _ in range(16):
                n=struct.unpack('!I',recv_exact(self.request,4))[0]
                if not 1<=n<=65536:return
                signature=recv_exact(self.request,32);payload=recv_exact(self.request,n)
                want=hmac.digest(self.server.secret,b'probe/'+payload,'sha256')
                if not hmac.compare_digest(signature,want):return
                self.request.sendall(hmac.digest(self.server.secret,b'reply/'+payload,'sha256')+payload)
        except (OSError,ConnectionError):pass

class EchoUDP(socketserver.BaseRequestHandler):
    def handle(self):
        raw,s=self.request
        if not 36<=len(raw)<=65500:return
        n=struct.unpack('!I',raw[:4])[0];payload=raw[36:]
        if n!=len(payload) or not hmac.compare_digest(raw[4:36],hmac.digest(self.server.secret,b'probe/'+payload,'sha256')):return
        s.sendto(hmac.digest(self.server.secret,b'reply/'+payload,'sha256')+payload,self.client_address)

class TCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address=True;daemon_threads=True
class UDPServer(socketserver.ThreadingUDPServer):
    allow_reuse_address=True;daemon_threads=True

class PinnedServer(http.server.ThreadingHTTPServer):
    daemon_threads=True;request_queue_size=64
    def handle_error(self,request,client_address):
        # Untrusted clients can abandon a TLS handshake; do not flood the journal.
        import sys
        if isinstance(sys.exc_info()[1],(ssl.SSLError,ConnectionError,TimeoutError)):return
        super().handle_error(request,client_address)
    def get_request(self):
        s,addr=self.socket.accept();s.settimeout(8)
        return self.context.wrap_socket(s,server_side=True,do_handshake_on_connect=False),addr

class PeerAPI(http.server.BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_POST(self):
        a=self.server.agent
        if self.path!='/peer/sync' or not hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+derive(a.plan,'control')):
            self.send_error(403);return
        try:
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<32000:raise ValueError('invalid body')
            body=json.loads(self.rfile.read(size))
            if body.get('plan_id')!=a.plan['id'] or body.get('role')!=('abroad' if a.role=='iran' else 'iran'):raise ValueError('wrong peer')
            with a.lock:
                a.peer_seen=time.time();a.peer_running=[r for r in body.get('running',[]) if r in a.routes]
                if a.role=='abroad':a.accept_desired(body)
                response=a.peer_state()
            raw=json.dumps(response).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        except (ValueError,TypeError,KeyError):self.send_error(400)

class Agent:
    def __init__(self,directory,engine=ENGINE):
        self.directory=pathlib.Path(directory);self.cfg=json.loads((self.directory/'node.json').read_text())
        self.plan=validate_plan(self.cfg['plan']);self.role=self.cfg['role']
        if self.role not in ('iran','abroad'):raise ValueError('Invalid role')
        self.engine=pathlib.Path(engine);self.lock=threading.RLock();self.eval_lock=threading.Lock();self.switch_lock=threading.Lock();self.stop=threading.Event()
        self.policy=policy_defaults();self.policy.update(self.cfg.get('policy',{}));validate_policy(self.policy)
        state_path=self.directory/'runtime.json'
        try:saved=json.loads(state_path.read_text())
        except (OSError,ValueError):saved={}
        self.history=saved.get('history',{});self.events=saved.get('events',[])[-100:]
        self.revision=int(saved.get('revision',0))+1;self.last_accepted=int(saved.get('last_accepted',0))
        self.active=None;self.previous=None;self.draining={};self.last_switch=0;self.last_test=0;self.testing=False;self.test_progress=0
        self.peer_seen=0;self.peer_running=[];self.peer_endpoint=None;self.failure_streak=0;self.started=time.time();self.alerts=queue.Queue(maxsize=100)
        self.routes=provision(self.plan,self.role,self.directory/'engines',self.engine)
        if os.geteuid()!=0:
            for r in self.routes.values():
                if r['kind']=='direct' or r['transport'] in ('pck','xdi'):r['skip']='Requires Linux root privileges'
        if not pathlib.Path('/dev/net/tun').exists():
            for r in self.routes.values():
                if r['kind']=='direct':r['skip']='TUN device is unavailable on this server'
        for r in self.routes.values():
            r['history']=self.history.get(r['id'],[])[-720:];r.update(score=0,last=None,success_rate=None)
        self.desired=[k for k,r in self.routes.items() if not r['skip']]
        self.pool=EnginePool(self.routes,self.directory/'engines',self.engine)
        self.echoes={};self.relay=None;self.servers=[]
        self.secret=derive(self.plan,'probe').encode()
        self.admin_key=self.cfg['admin_key']
        if len(self.admin_key)<32:raise ValueError('Admin key is too short')
        self.context=ssl.create_default_context(cadata=self.plan['cert']);self.context.check_hostname=False
        self.event('Native controller started. Routes require fresh verification.')

    def event(self,message):
        with self.lock:
            self.events.append({'time':time.time(),'message':message});self.events=self.events[-100:]
        try:self.alerts.put_nowait(message)
        except queue.Full:pass

    def persist(self):
        with self.lock:
            atomic_json(self.directory/'runtime.json',{'history':{k:r['history'][-720:] for k,r in self.routes.items()},'events':self.events,'revision':self.revision,'last_accepted':self.last_accepted})

    def set_desired(self,ids):
        with self.lock:
            ids=sorted(set(ids));allowed=set(self.routes)
            if not set(ids)<=allowed:raise ValueError('Unknown route')
            if ids!=sorted(self.desired):self.desired=ids;self.revision+=1;self.persist()

    def accept_desired(self,body):
        revision=body.get('revision');wanted=body.get('desired')
        if type(revision) is not int or revision<self.last_accepted:return
        if not isinstance(wanted,list) or any(x not in self.routes for x in wanted):raise ValueError('Invalid desired routes')
        self.desired=sorted(set(wanted));self.last_accepted=revision
        self.active=body.get('active') if body.get('active') in self.routes else None

    def peer_state(self):
        return {'plan_id':self.plan['id'],'role':self.role,'running':self.pool.running(),'desired':list(self.desired),'revision':self.revision,
                'ack_revision':self.last_accepted,'active':self.active,'unavailable':{k:r['skip'] for k,r in self.routes.items() if r['skip']}}

    def peer_addresses(self):
        addresses=[]
        if self.peer_endpoint:addresses.append(self.peer_endpoint)
        remote='abroad' if self.role=='iran' else 'iran'
        addresses.append((self.plan[remote],self.plan['control_port']))
        running=self.pool.running()
        preferred=sorted((r for r in self.routes.values() if r['id'] in running),key=lambda r:-(r['score'] or 0))
        for r in preferred:
            if self.role=='iran' and r['tcp']:addresses.append(('127.0.0.1',r['control']))
            if r['kind']=='direct':addresses.append((r[remote+'_ip'],self.plan['control_port']))
        return list(dict.fromkeys(addresses))

    def peer_call(self,address,body):
        c=http.client.HTTPSConnection(address[0],address[1],timeout=2.5,context=self.context)
        try:
            c.request('POST','/peer/sync',json.dumps(body),{'Authorization':'Bearer '+derive(self.plan,'control'),'Content-Type':'application/json'})
            r=c.getresponse()
            if r.status!=200:raise ConnectionError('Peer refused request')
            data=json.loads(r.read(32000))
            if data.get('plan_id')!=self.plan['id'] or data.get('role')!=('abroad' if self.role=='iran' else 'iran'):raise ConnectionError('Wrong peer')
            return address,data
        finally:c.close()

    def sync_once(self):
        with self.lock:body=self.peer_state()
        addresses=self.peer_addresses();answer=None
        # Prefer the already working route, then race alternatives with bounded timeouts.
        if self.peer_endpoint:
            try:answer=self.peer_call(self.peer_endpoint,body)
            except (OSError,ValueError,http.client.HTTPException):pass
        if answer is None:
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                jobs=[pool.submit(self.peer_call,a,body) for a in addresses]
                for job in concurrent.futures.as_completed(jobs):
                    try:answer=job.result();break
                    except (OSError,ValueError,http.client.HTTPException):pass
                for job in jobs:job.cancel()
        if answer:
            addr,data=answer
            with self.lock:
                self.peer_endpoint=addr;self.peer_seen=time.time();self.peer_running=[r for r in data.get('running',[]) if r in self.routes]
                if self.role=='abroad':self.accept_desired(data)
            return True
        return False

    def peer_loop(self):
        while not self.stop.is_set():
            try:self.sync_once()
            except Exception as e:self.event('Peer sync error: '+type(e).__name__)
            self.stop.wait(3)

    def make_relay(self):
        listeners=[];targets={}
        if self.role=='iran':
            for j,m in enumerate(self.plan['mappings']):listeners.append({'bind':m['listen'],'key':str(j),'protocol':m['protocol']})
        else:
            for r in self.routes.values():
                if r['kind']!='direct' or r['skip']:continue
                for j,m in enumerate(self.plan['mappings']):
                    key=r['id']+'/'+str(j);listeners.append({'bind':f"{r['abroad_ip']}:{self.plan['direct_service_base']+j}",'key':key,'protocol':m['protocol']});targets[key]=m['target']
        self.relay_data={'listeners':listeners,'targets':targets,'generation':'initial'}
        atomic_json(self.directory/'relay.json',self.relay_data)
        with open(self.directory/'relay.log','ab',buffering=0) as log:
            self.relay=subprocess.Popen([str(self.engine),'batman-relay',str(self.directory/'relay.json')],stdout=log,stderr=log,start_new_session=True)

    def activate(self,rid):
        with self.lock:
            self.relay_data['targets']={} if rid is None else {str(j):f"127.0.0.1:{port}" for j,port in enumerate(self.routes[rid]['service'])}
            self.relay_data['generation']=secrets.token_hex(8)
            atomic_json(self.directory/'relay.json',self.relay_data);self.active=rid

    def ensure_echo(self):
        if self.role!='abroad':return
        ips=['127.0.0.1']+[r['abroad_ip'] for r in self.routes.values() if r['kind']=='direct' and r['id'] in self.pool.running()]
        for ip in ips:
            for proto,server,handler in [('tcp',TCPServer,EchoTCP),('udp',UDPServer,EchoUDP)]:
                key=(ip,proto)
                if key in self.echoes:continue
                try:
                    s=server((ip,self.plan['echo_port']),handler);s.secret=self.secret
                    self.echoes[key]=s;threading.Thread(target=s.serve_forever,daemon=True).start()
                except OSError:pass
        for key,s in list(self.echoes.items()):
            if key[0] not in ips:s.shutdown();s.server_close();self.echoes.pop(key,None)

    def pool_loop(self):
        while not self.stop.is_set():
            with self.lock:wanted=list(self.desired)
            self.pool.ensure(wanted);self.ensure_echo()
            if self.relay and self.relay.poll() is not None:
                # A relay crash is recovered without changing the selected route file.
                self.event('Entrypoint process restarted.')
                with open(self.directory/'relay.log','ab',buffering=0) as log:self.relay=subprocess.Popen([str(self.engine),'batman-relay',str(self.directory/'relay.json')],stdout=log,stderr=log,start_new_session=True)
            self.stop.wait(2)

    def probe_once(self,r,proto='tcp',bulk=False):
        payload=secrets.token_bytes(16384 if bulk and proto=='tcp' else 512 if proto=='udp' else 64)
        packet=struct.pack('!I',len(payload))+hmac.digest(self.secret,b'probe/'+payload,'sha256')+payload
        addr=('127.0.0.1',r['probe']);start=time.monotonic()
        try:
            if proto=='tcp':
                with socket.create_connection(addr,timeout=2) as s:
                    s.settimeout(2);s.sendall(packet);reply=recv_exact(s,len(payload)+32)
            else:
                with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as s:
                    s.settimeout(2);s.connect(addr);s.send(packet);reply=s.recv(65535)
            if len(reply)!=len(payload)+32 or not hmac.compare_digest(reply[32:],payload) or not hmac.compare_digest(reply[:32],hmac.digest(self.secret,b'reply/'+payload,'sha256')):raise ValueError('Bad authenticated echo')
            duration=max(time.monotonic()-start,.00001)
            return {'ok':True,'latency_ms':duration*1000,'sample_mbps':len(payload)*16/duration/1e6,'error':None}
        except (OSError,ValueError,ConnectionError) as e:return {'ok':False,'latency_ms':None,'sample_mbps':0,'error':type(e).__name__}

    def protocols_to_probe(self,r):
        # UDP-only native transport is tested as UDP, and remains ineligible for TCP services.
        if not r['tcp']:return ['udp']
        need_tcp=any(m['protocol'] in ('tcp','both') for m in self.plan['mappings'])
        need_udp=any(m['protocol'] in ('udp','both') for m in self.plan['mappings'])
        return (['tcp'] if need_tcp else [])+(['udp'] if need_udp else [])

    def check_route(self,r,trials=1):
        results=[]
        for _ in range(trials):
            for proto in self.protocols_to_probe(r):results.append(self.probe_once(r,proto))
        oks=[x for x in results if x['ok']];lat=[x['latency_ms'] for x in oks]
        return {'time':time.time(),'ok':len(oks)==len(results),'success':len(oks)/max(1,len(results)),
                'latency_ms':statistics.mean(lat) if lat else None,'jitter_ms':statistics.pstdev(lat) if len(lat)>1 else 0,
                'sample_mbps':0,'error':next((x['error'] for x in results if not x['ok']),None)}

    def record(self,rid,result):
        with self.lock:
            r=self.routes[rid];r['last']=result
            r['history']=[x for x in r['history'] if time.time()-x['time']<7*86400][-719:]+[result]
            history_rate=statistics.mean(x['success'] for x in r['history'])
            recent=statistics.mean(x['success'] for x in r['history'][-20:]);r['success_rate']=history_rate
            latency=max(0,1-(result['latency_ms'] or 1000)/1000)
            jitter=max(0,1-result.get('jitter_ms',0)/200)
            r['score']=round(55*result['success']+25*recent+10*history_rate+7*latency+3*jitter,2) if result['ok'] else 0

    def switch(self,rid,emergency=False):
        with self.switch_lock:return self._switch(rid,emergency)

    def _switch(self,rid,emergency=False):
        r=self.routes[rid]
        with self.lock:
            old=self.active
            if rid==old:return True
            if old and not self.policy['auto']:return False
            if not compatible(self.plan,r) or r['skip']:return False
            if old and not emergency:
                if time.time()-self.last_switch<self.policy['cooldown'] or r['score']<self.routes[old]['score']+self.policy['switch_margin']:return False
            if rid not in self.peer_running or time.time()-self.peer_seen>20:return False
        check=self.check_route(r,3);self.record(rid,check)
        if not check['ok']:return False
        # Native route has carried authenticated probes; both endpoints are prepared.
        self.activate(rid);self.stop.wait(.4)
        after=self.check_route(r,3);self.record(rid,after)
        if not after['ok']:
            rollback=old if old and self.check_route(self.routes[old],3)['ok'] else None
            self.activate(rollback);self.event('Post-switch check failed. '+('Previous route restored.' if rollback else 'No verified fallback is available.'));return False
        with self.lock:
            self.previous=old;self.last_switch=time.time();self.failure_streak=0
            if old:self.draining[old]=time.time()+600
        self.event('Active route: '+r['name']+'.');self.persist();return True

    def soak(self,r,seconds):
        if r['skip']:return None
        # Wait separately for startup so handshake time is not scored as sustained loss.
        deadline=time.monotonic()+45
        first=None
        while not self.stop.is_set() and time.monotonic()<deadline:
            first=self.check_route(r)
            if first['ok']:break
            self.stop.wait(1)
        if not first or not first['ok']:return first or self.check_route(r)
        values=[];end=time.monotonic()+seconds
        while not self.stop.is_set() and time.monotonic()<end:
            values.append(self.check_route(r));self.stop.wait(1)
        if not values:return None
        good=[x for x in values if x['ok']];rate=len(good)/len(values)
        lat=[x['latency_ms'] for x in good];last_ok=all(x['ok'] for x in values[-3:])
        # Serial short bulk probes are run after the concurrent stability window.
        return {'time':time.time(),'ok':rate>=.98 and last_ok,'success':rate,'latency_ms':statistics.mean(lat) if lat else None,
                'jitter_ms':statistics.pstdev(lat) if len(lat)>1 else 0,'sample_mbps':0,'samples':len(values),'error':None if rate>=.98 else 'Unstable delivery'}

    def trim_candidates(self):
        with self.lock:
            if self.testing:return
            eligible=sorted((r for r in self.routes.values() if r['last'] and r['last']['ok'] and compatible(self.plan,r)),key=lambda r:r['score'],reverse=True)
            keep=[r['id'] for r in eligible if r['id']!=self.active][:1]
            if self.active:keep.append(self.active)
            self.draining={k:v for k,v in self.draining.items() if v>time.time()}
            keep.extend(self.draining)
            # With no healthy route, keep candidates running so peer control can recover.
            if self.active:self.set_desired(keep)

    def evaluate(self,emergency=False):
        if self.role!='iran' or not self.eval_lock.acquire(False):return False
        with self.lock:self.testing=True;self.test_progress=0
        try:
            self.event('Testing native transports on both servers.')
            self.set_desired([k for k,r in self.routes.items() if not r['skip']])
            self.pool.ensure(list(self.desired));self.sync_once()
            candidates=[r for r in self.routes.values() if not r['skip']]
            seconds=min(30,self.policy['soak_seconds']) if emergency else self.policy['soak_seconds']
            start=time.monotonic()
            with concurrent.futures.ThreadPoolExecutor(max_workers=18) as pool:
                jobs={pool.submit(self.soak,r,seconds):r['id'] for r in candidates}
                for job in concurrent.futures.as_completed(jobs):
                    result=job.result()
                    if result:self.record(jobs[job],result)
                    self.test_progress=round(100*sum(j.done() for j in jobs)/max(1,len(jobs)))
            # Short transfer samples never compete with each other or determine stability.
            for r in candidates:
                if r['last'] and r['last']['ok']:
                    b=self.probe_once(r,self.protocols_to_probe(r)[0],True)
                    with self.lock:r['last']['sample_mbps']=b['sample_mbps']
            self.sync_once()
            ranked=sorted((r for r in candidates if r['last'] and r['last']['ok'] and compatible(self.plan,r)),key=lambda r:r['score'],reverse=True)
            switched=False
            for r in ranked:
                if self.switch(r['id'],emergency):switched=True;break
            with self.lock:self.last_test=time.time()
            if not ranked:self.event('No compatible route passed the stability window. The current route was not replaced.')
            else:self.event('Route comparison completed in '+str(round(time.monotonic()-start))+' seconds.')
            self.persist();return True
        except Exception as e:self.event('Route comparison failed: '+type(e).__name__);return False
        finally:
            with self.lock:self.testing=False
            self.eval_lock.release();self.trim_candidates()

    def health_loop(self):
        # Both native engines start while pairing; measure only after the peer joins.
        while not self.stop.is_set() and not self.peer_seen:self.stop.wait(2)
        if self.stop.is_set():return
        threading.Thread(target=self.evaluate,daemon=True).start()
        last_attempt=time.time();next_health=time.monotonic()+self.policy['health_interval']
        while not self.stop.wait(1):
            if time.time()-last_attempt>=self.policy['test_interval']:
                last_attempt=time.time();threading.Thread(target=self.evaluate,daemon=True).start()
            if time.monotonic()<next_health:continue
            next_health=time.monotonic()+self.policy['health_interval']
            with self.lock:active=self.active
            if active:
                result=self.check_route(self.routes[active]);self.record(active,result)
                self.failure_streak=0 if result['ok'] else self.failure_streak+1
                if not result['ok']:self.event('Active route health check failed.')
                if self.policy['auto'] and self.failure_streak>=self.policy['failure_threshold']:
                    standby=[r for r in self.routes.values() if r['id']!=active and r['id'] in self.pool.running() and compatible(self.plan,r)]
                    for r in sorted(standby,key=lambda r:r['score'],reverse=True):
                        if self.switch(r['id'],True):break
                    else:threading.Thread(target=self.evaluate,kwargs={'emergency':True},daemon=True).start()
            elif not self.testing:threading.Thread(target=self.evaluate,kwargs={'emergency':True},daemon=True).start()
            self.trim_candidates();self.persist()

    def status(self):
        with self.lock:
            return copy.deepcopy({'role':self.role,'active':self.active,'policy':self.policy,'testing':self.testing,'test_progress':self.test_progress,'last_test':self.last_test,
                'peer_connected':time.time()-self.peer_seen<20,'peer_age':round(time.time()-self.peer_seen) if self.peer_seen else None,'events':self.events,
                'profiles':[{k:r.get(k) for k in ('id','name','kind','transport','skip','score','last','success_rate')}|{'compatible':compatible(self.plan,r),'running':r['id'] in self.pool.running()} for r in self.routes.values()]})

    def notify_loop(self):
        token=os.environ.get('BATMAN_TELEGRAM_TOKEN');chat=os.environ.get('BATMAN_TELEGRAM_CHAT_ID')
        if not token or not chat:return
        while not self.stop.is_set():
            try:message=self.alerts.get(timeout=1)
            except queue.Empty:continue
            try:
                data=urllib.parse.urlencode({'chat_id':chat,'text':'BatmanTunnel | '+message}).encode()
                with urllib.request.urlopen('https://api.telegram.org/bot'+token+'/sendMessage',data=data,timeout=8) as r:r.read(1024)
            except Exception:pass

    def run(self):
        cert=self.directory/'pair.crt';key=self.directory/'pair.key'
        cert.write_text(self.plan['cert']);key.write_text(self.plan['key']);cert.chmod(0o600);key.chmod(0o600)
        tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);tls.minimum_version=ssl.TLSVersion.TLSv1_2;tls.load_cert_chain(cert,key)
        peer=PinnedServer((self.cfg.get('peer_bind','0.0.0.0'),self.plan['control_port']),PeerAPI);peer.context=tls;peer.agent=self
        from .web import Panel
        panel=http.server.ThreadingHTTPServer(('127.0.0.1',self.cfg.get('panel_port',8787)),Panel);panel.agent=self
        self.servers=[peer,panel];self.make_relay()
        for task in (peer.serve_forever,panel.serve_forever,self.pool_loop,self.peer_loop,self.notify_loop):threading.Thread(target=task,daemon=True).start()
        if self.role=='iran':threading.Thread(target=self.health_loop,daemon=True).start()
        try:self.stop.wait()
        finally:
            for s in self.servers:s.shutdown();s.server_close()
            for s in self.echoes.values():s.shutdown();s.server_close()
            if self.relay and self.relay.poll() is None:
                self.relay.terminate()
                try:self.relay.wait(timeout=3)
                except subprocess.TimeoutExpired:self.relay.kill()
            self.pool.close();self.persist()
