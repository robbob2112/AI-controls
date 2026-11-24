"""Private native BMC client. Credentials must remain root-only."""
import http.cookiejar
import json
import pathlib
import ssl
import urllib.parse
import urllib.request

class BMC:
    def __init__(self, timeout=12):
        self.timeout = timeout
        self.credentials = json.loads(pathlib.Path('/etc/hal-case-fans/bmc.json').read_text())
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
            urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))
        self.token = None

    def request(self, path, method='GET', data=None, form=False):
        headers = {}
        if self.token:
            headers['X-CSRFTOKEN'] = self.token
        if data is not None:
            data = urllib.parse.urlencode(data).encode() if form else json.dumps(data).encode()
            headers['Content-Type'] = 'application/x-www-form-urlencoded' if form else 'application/json'
        request = urllib.request.Request('https://192.0.2.11'+path, data=data, headers=headers, method=method)
        with self.opener.open(request, timeout=self.timeout) as response:
            raw = response.read()
            return json.loads(raw) if raw else None

    def __enter__(self):
        result = self.request('/api/session', 'POST', self.credentials, form=True)
        if result.get('ok') != 0 or result.get('passwordStatus',0) != 0:
            raise RuntimeError('BMC login not fully authorized')
        self.token = result['CSRFToken']
        return self

    def __exit__(self, *_):
        try:
            self.request('/api/session','DELETE')
        except Exception:
            pass

    def curves(self):
        rows = self.request('/api/fanctrl/PWM')
        expected = {0:'Zone1(CPU_FAN & CPU_OPT)',1:'Zone2(CHA_FAN1)',3:'Zone4(CHA_FAN3)',
                    4:'Zone5(CHA_FAN4)',5:'Zone6(CHA_FAN5)',6:'Zone7(W_PUMP+)'}
        for index, name in expected.items():
            if rows[index]['PWMNum'] != index or rows[index]['PWMName'] != name:
                raise RuntimeError('Unexpected BMC fan-zone mapping')
        return rows

    def put_curve(self, index, points):
        if index not in (1,3,4,5):
            raise ValueError('Only connected chassis fan zones are writable')
        if len(points)!=5 or any(not 0<=p['Temp']<=100 or not 10<=p['Duty']<=100 for p in points):
            raise ValueError('Invalid thermal curve')
        flat = [value for p in points for value in (p['Temp'],p['Duty'])]
        return self.request('/api/fanctrl/PWM','PUT',{'PWMIndex':index,'PWMSrc':0,'PWMNum':index,'CurrentPWMdata':flat})
