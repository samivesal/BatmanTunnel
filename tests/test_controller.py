import copy, http.client, json, pathlib, secrets, ssl, tempfile, threading, time, unittest
from unittest.mock import patch
from batmantunnel.common import *
from batmantunnel.runtime import Agent, PinnedServer, PeerAPI
from batmantunnel.web import Panel
import http.server

class Pool:
    def running(self):return ['reverse-tcp','reverse-quic']

class PolicyTests(unittest.TestCase):
    def agent(self,d):
        a=Agent.__new__(Agent);a.lock=threading.RLock();a.switch_lock=threading.Lock();a.stop=threading.Event()
        a.plan={'mappings':[{'protocol':'tcp'}]};a.role='iran';a.policy=policy_defaults();a.pool=Pool()
        a.routes={rid:{'id':rid,'name':rid,'score':score,'skip':'','tcp':True,'history':[],'last':None} for rid,score in [('reverse-tcp',80),('reverse-quic',99)]}
        a.active='reverse-tcp';a.previous=None;a.draining={};a.last_switch=time.time()-3600;a.peer_seen=time.time();a.peer_running=['reverse-quic'];a.failure_streak=0
        a.directory=pathlib.Path(d);a.relay_data={'targets':{},'generation':'x'};a.routes['reverse-quic']['service']=[33000];a.routes['reverse-tcp']['service']=[33001]
        a.event=lambda message:None;a.persist=lambda:None
        a.record=lambda rid,result:None
        return a
    def test_switch_requires_prepared_peer_and_margin(self):
        with tempfile.TemporaryDirectory() as d:
            a=self.agent(d);a.check_route=lambda *_:{'ok':True}
            a.peer_running=[];self.assertFalse(a.switch('reverse-quic'))
            a.peer_running=['reverse-quic'];a.routes['reverse-quic']['score']=85;self.assertFalse(a.switch('reverse-quic'))
            a.routes['reverse-quic']['score']=99;a.last_switch=time.time();self.assertFalse(a.switch('reverse-quic'))
            self.assertTrue(a.switch('reverse-quic',True));self.assertEqual(a.active,'reverse-quic');self.assertIn('reverse-tcp',a.draining)
    def test_post_switch_failure_rolls_back(self):
        with tempfile.TemporaryDirectory() as d:
            a=self.agent(d);answers=iter([{'ok':True},{'ok':False},{'ok':True}]);a.check_route=lambda *_:next(answers)
            self.assertFalse(a.switch('reverse-quic'));self.assertEqual(a.active,'reverse-tcp')
    def test_auto_off_retains_current_route(self):
        with tempfile.TemporaryDirectory() as d:
            a=self.agent(d);a.policy['auto']=False;a.check_route=lambda *_:self.fail('Should not probe for a disabled switch')
            self.assertFalse(a.switch('reverse-quic',True));self.assertEqual(a.active,'reverse-tcp')
    def test_reordered_peer_command_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            a=self.agent(d);a.last_accepted=12;a.desired=['reverse-tcp']
            a.accept_desired({'revision':11,'desired':['reverse-quic']});self.assertEqual(a.desired,['reverse-tcp'])
            a.accept_desired({'revision':13,'desired':['reverse-quic']});self.assertEqual(a.desired,['reverse-quic'])
            with self.assertRaises(ValueError):a.accept_desired({'revision':14,'desired':['unknown']})

class APIAuthenticationTests(unittest.TestCase):
    def test_pinned_https_peer_and_local_panel(self):
        with tempfile.TemporaryDirectory() as d:
            plan=create_plan('127.0.0.1','127.0.0.2',[{'listen':'0.0.0.0:8443','target':'127.0.0.1:8444','protocol':'tcp'}])
            class A:
                role='abroad';lock=threading.RLock();routes={'reverse-tcp':{}};admin_key='a'*48
                def accept_desired(self,body):self.received=body
                def peer_state(self):return {'plan_id':plan['id'],'role':'abroad','running':[]}
                def status(self):return {'role':'abroad'}
            a=A();a.plan=plan
            cert=pathlib.Path(d)/'cert';key=pathlib.Path(d)/'key';cert.write_text(plan['cert']);key.write_text(plan['key'])
            tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);tls.load_cert_chain(cert,key)
            server=PinnedServer(('127.0.0.1',0),PeerAPI);server.context=tls;server.agent=a
            panel=http.server.ThreadingHTTPServer(('127.0.0.1',0),Panel);panel.agent=a
            for s in (server,panel):threading.Thread(target=s.serve_forever,daemon=True).start()
            try:
                context=ssl.create_default_context(cadata=plan['cert']);context.check_hostname=False
                for token,expected in [('wrong',403),(derive(plan,'control'),200)]:
                    c=http.client.HTTPSConnection('127.0.0.1',server.server_address[1],context=context,timeout=2)
                    c.request('POST','/peer/sync',json.dumps({'plan_id':plan['id'],'role':'iran','running':['reverse-tcp']}),{'Authorization':'Bearer '+token})
                    self.assertEqual(c.getresponse().status,expected);c.close()
                with self.assertRaises(ssl.SSLCertVerificationError):
                    c=http.client.HTTPSConnection('127.0.0.1',server.server_address[1],timeout=2);c.request('POST','/peer/sync','{}')
                for token,expected in [('wrong',401),(a.admin_key,200)]:
                    c=http.client.HTTPConnection('127.0.0.1',panel.server_address[1],timeout=2);c.request('GET','/api/status',headers={'Authorization':'Bearer '+token})
                    r=c.getresponse();self.assertEqual(r.status,expected);r.read();c.close()
            finally:
                for s in (server,panel):s.shutdown();s.server_close()

if __name__=='__main__':unittest.main()
