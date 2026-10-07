"""Local two-node acceptance test; not an Internet performance benchmark."""
import contextlib, json, pathlib, secrets, threading, time, unittest
from batmantunnel.common import *
from batmantunnel.runtime import Agent
from test_native import echo, free_port, wait_probe

class PairedAcceptanceTest(unittest.TestCase):
    def test_two_nodes_select_and_switch_native_routes(self):
        nodes=[];threads=[]
        with tempfile.TemporaryDirectory() as temp,echo(b'backend-service') as backend:
            entry=free_port();control=free_port();echo_port=free_port()
            p=create_plan('127.0.0.1','127.0.0.2',[{'listen':f'127.0.0.1:{entry}','target':f'127.0.0.1:{backend}','protocol':'both'}],control_port=control,echo_port=echo_port)
            for role,bind in [('iran','127.0.0.1'),('abroad','127.0.0.2')]:
                d=pathlib.Path(temp)/role;d.mkdir();atomic_json(d/'node.json',{'plan':p,'role':role,'policy':policy_defaults(),'admin_key':secrets.token_hex(24),'peer_bind':bind,'panel_port':free_port()})
                a=Agent(d)
                for rid,r in a.routes.items():
                    if rid not in ('reverse-tcp','reverse-quic'):r['skip']='Excluded from this focused integration test'
                a.desired=['reverse-tcp','reverse-quic'];a.policy['soak_seconds']=2
                nodes.append(a);t=threading.Thread(target=a.run,daemon=True);threads.append(t);t.start()
            try:
                iran,abroad=nodes
                deadline=time.monotonic()+30
                while time.monotonic()<deadline and not iran.active:time.sleep(.3)
                self.assertIsNotNone(iran.active,'No route selected by paired native controllers')
                wait_probe(entry,b'backend-service');wait_probe(entry,b'backend-service','udp')
                old=iran.active;other='reverse-quic' if old=='reverse-tcp' else 'reverse-tcp'
                iran.sync_once();self.assertTrue(iran.switch(other,True))
                self.assertNotEqual(iran.active,old)
                wait_probe(entry,b'backend-service');wait_probe(entry,b'backend-service','udp')
                iran.sync_once();self.assertEqual(abroad.active,iran.active)
                print('Paired selection and switch passed:',old,'->',other,flush=True)
            finally:
                for a in nodes:a.stop.set()
                for t in threads:t.join(timeout=15)
                self.assertTrue(all(not t.is_alive() for t in threads))

if __name__=='__main__':unittest.main(verbosity=2)
