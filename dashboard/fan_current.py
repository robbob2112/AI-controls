import sqlite3,time
DB='/var/lib/hal-monitor/history.sqlite3'
HEADERS=('CPU_FAN','CPU_OPT','CHA_FAN1','CHA_FAN2','CHA_FAN3','CHA_FAN4','CHA_FAN5','W_PUMP+')
def get(handler,sensor_name):
 try:
  with sqlite3.connect('file:'+DB+'?mode=ro',uri=True,timeout=5) as db:
   ts=db.execute('SELECT max(ts) FROM samples').fetchone()[0]
   rows=db.execute("SELECT sensor,value FROM readings WHERE ts=? AND kind='fan'",(ts,)).fetchall()
  fans={}
  for sensor,value in rows:
   name=sensor_name(sensor)
   if name.startswith('Motherboard BMC · '):
    header=name.split(' · ')[-1]
    if header in HEADERS:fans[header]=value
  age=time.time()-ts if ts else None
  handler.send_json(200,{'time':ts,'age_seconds':age,'stale':age is None or age>180,'fans':fans})
 except Exception:handler.send_json(503,{'error':'Fresh fan readings unavailable'})
