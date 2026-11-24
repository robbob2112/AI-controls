"""Authenticated lab gateway: trusted host HTML and a fixed JSON-only guest API."""
import json,pathlib,urllib.request,urllib.parse,urllib.error
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*args,**kwargs):return None
OPENER=urllib.request.build_opener(NoRedirect())
def reply(h,value,status=200,typ='application/json'):
 body=value if isinstance(value,bytes) else json.dumps(value).encode()
 h.send_response(status);h.send_header('Content-Type',typ);h.send_header('Content-Length',str(len(body)));h.send_header('Cache-Control','no-store');h.send_header('X-Content-Type-Options','nosniff');h.send_header('X-Frame-Options','DENY');h.end_headers();h.wfile.write(body)
def api(h):
 path=urllib.parse.urlsplit(h.path).path
 if not path.startswith('/llama/lab'):return False
 if path=='/llama/lab':
  h.send_response(302);h.send_header('Location','/llama/lab/');h.send_header('Content-Length','0');h.end_headers();return True
 if path=='/llama/lab/' and h.command=='GET':reply(h,pathlib.Path('/usr/local/share/hal-lab.html').read_bytes(),typ='text/html; charset=utf-8');return True
 allowed={('GET','/llama/lab/api/state'),('GET','/llama/lab/api/status'),('POST','/llama/lab/api/control'),('POST','/llama/lab/api/exec')}
 if (h.command,path) not in allowed:reply(h,{'error':'Unknown lab API'},404);return True
 try:
  body=None
  if h.command=='POST':
   if h.headers.get_content_type()!='application/json':raise ValueError('JSON required')
   length=int(h.headers.get('Content-Length','0'))
   if not 0<length<=32768:raise ValueError('Invalid request size')
   body=h.rfile.read(length);json.loads(body)
  request=urllib.request.Request('http://127.0.0.1:18111/api/'+path.rsplit('/',1)[-1],body,{'Content-Type':'application/json'})
  with OPENER.open(request,timeout=75) as response:
   raw=response.read(262145)
   if len(raw)>262144:raise ValueError('VM response too large')
   reply(h,json.loads(raw))
 except urllib.error.HTTPError as error:
  reply(h,{'error':'VM request failed: '+str(error.code)},502)
 except (OSError,ValueError,urllib.error.URLError) as error:reply(h,{'error':'Lab unavailable: '+str(error)},503)
 return True
