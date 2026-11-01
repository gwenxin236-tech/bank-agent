import tempfile,unittest,json,threading,urllib.request,urllib.error
from pathlib import Path
from unittest.mock import patch
import banking_agent as ba
class ServiceHttpTests(unittest.TestCase):
 def test_session_scope_fields_and_official_case_cannot_be_forged(self):
  with tempfile.TemporaryDirectory() as tmp:
   rt=ba.AgentRuntime(db_path=Path(tmp)/'bank.sqlite3',offline=True)
   try:
    with patch.object(ba,'runtime',rt),patch.object(ba,'gateway',ba.AgentGateway(rt)),ba.ThreadingHTTPServer(('127.0.0.1',0),ba.Handler) as server:
     thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
     try:
      base=f'http://127.0.0.1:{server.server_port}';tid=rt.bank.recent_transactions().data['items'][0]['id']
      for token,extra,status in [('',{},403),('demo-user-session',{'user_id':'other'},403),('demo-user-session',{'official_case_id':'bank_approved'},400),('demo-user-session',{},200)]:
       body={'transaction_id':tid,'reason':'not_authorized','request_id':'http-case',**extra};request=urllib.request.Request(base+'/api/v1/service-tasks/dispute',data=json.dumps(body).encode(),headers={'Content-Type':'application/json',**({'Authorization':'Bearer '+token} if token else {})})
       try:
        with urllib.request.urlopen(request,timeout=10) as response:actual=response.status;data=json.load(response)
       except urllib.error.HTTPError as error:actual=error.code;data=json.loads(error.read());error.close()
       self.assertEqual(actual,status,data)
       if actual==200:self.assertIsNone(data['service_task']['official_case_id']);self.assertFalse(data['business_executed'])
     finally:server.shutdown();thread.join(timeout=5)
   finally:rt.recovery_scheduler.stop();rt.bank.close();rt.audit.db.close()
if __name__=='__main__':unittest.main()
