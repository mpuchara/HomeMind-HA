"""Local UI fixture, no Home Assistant connection or runtime threads."""
import os
import sys
import tempfile
from pathlib import Path
from http.server import ThreadingHTTPServer
from unittest.mock import Mock

data=tempfile.TemporaryDirectory(prefix='homemind-ui-')
os.environ['ADAPTIVE_AI_DATA']=data.name
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'adaptive_ai/src'))
import main
from ha import HA
import engine
from simulate_anticipation import fixture
from telemetry import TELEMETRY

a,states,context,policy,example=fixture()
example('arriving',main.now_ts()-3)
main.ENGINE.state_map=states
main.ENGINE.context=context
main.ENGINE.last_state_count=len(states)
created=main.STORE.create_agent(a)
main.STORE.set_training_state(created['id'],'qualified',score=.94,samples=120)
policy.agent=main.STORE.get_agent(created['id'])
main.ENGINE.models[created['id']]=policy
main.ENGINE.process_agent(policy.agent,states,{'binary_sensor.living'})
second=main.STORE.create_agent(dict(a,name='Schody',target_entity='light.stairs'))
main.STORE.set_training_state(second['id'],'needs_retrain',detail={'reason':'0.8 model'})
main.STORE.create_agent(dict(a,name='Ogrzewanie',target_entity='climate.living',target_property='temperature',min_value=15,max_value=25))
main.HISTORY=main.HistoryManager(main.ENGINE)
main.ENGINE.history_manager=main.HISTORY
main.ENGINE.home_bootstrap=main.HomeBootstrap(context,main.HISTORY,main.STORE)
HA.service=Mock(side_effect=RuntimeError('Offline UI preview: HA services disabled'))
print('http://127.0.0.1:18099',flush=True)
ThreadingHTTPServer(('127.0.0.1',18099),main.Handler).serve_forever()
