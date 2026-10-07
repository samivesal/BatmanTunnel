"""Loopback-only administration. Pair coordination uses a separate pinned TLS API."""
import hmac, http.server, json, mimetypes, pathlib, threading, urllib.parse
from .common import ROOT, atomic_json, validate_policy

class Panel(http.server.BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def reply(self,status,body):
        raw=json.dumps(body).encode();self.send_response(status)
        self.send_header('Content-Type','application/json');self.send_header('Cache-Control','no-store')
        self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    def authorized(self):
        if not hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+self.server.agent.admin_key):
            self.reply(401,{'error':'Invalid admin key'});return False
        return True
    def do_GET(self):
        path=urllib.parse.urlsplit(self.path).path
        if path.startswith('/api/'):
            if not self.authorized():return
            if path=='/api/status':self.reply(200,self.server.agent.status())
            else:self.reply(404,{'error':'Unknown endpoint'})
            return
        relative='index.html' if path=='/' else urllib.parse.unquote(path).lstrip('/')
        root=(ROOT/'dist').resolve();file=(root/relative).resolve()
        if not file.is_relative_to(root) or not file.is_file():self.send_error(404);return
        self.send_response(200);self.send_header('Content-Type',mimetypes.guess_type(file.name)[0] or 'application/octet-stream')
        self.send_header('X-Content-Type-Options','nosniff');self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Length',str(file.stat().st_size));self.end_headers()
        with file.open('rb') as source:
            while chunk:=source.read(65536):self.wfile.write(chunk)
    def do_POST(self):
        if not self.authorized():return
        a=self.server.agent
        if a.role!='iran':self.reply(409,{'error':'Selection is controlled by the Iran node'});return
        try:
            n=int(self.headers.get('Content-Length','0'))
            if not 0<n<=4096:raise ValueError('Invalid request size')
            body=json.loads(self.rfile.read(n))
            if not isinstance(body,dict):raise ValueError('Expected an object')
            if self.path=='/api/policy':
                if set(body)-set(a.policy):raise ValueError('Unknown policy field')
                with a.lock:
                    policy=validate_policy(a.policy|body);a.cfg['policy']=policy
                    atomic_json(a.directory/'node.json',a.cfg);a.policy=policy
                a.event('Selection policy updated.');self.reply(200,{'ok':True,'policy':policy})
            elif self.path=='/api/test':
                if a.testing:self.reply(409,{'error':'A comparison is already running'});return
                threading.Thread(target=a.evaluate,daemon=True).start();self.reply(202,{'ok':True})
            else:self.reply(404,{'error':'Unknown endpoint'})
        except (ValueError,TypeError,KeyError):self.reply(400,{'error':'Invalid request'})
