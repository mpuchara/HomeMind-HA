import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'adaptive_ai/src'))
DATA = tempfile.TemporaryDirectory(prefix='homemind-tests-')
os.environ['ADAPTIVE_AI_DATA'] = DATA.name


def state(eid, value='off', **attributes):
    return {'entity_id': eid, 'state': str(value), 'attributes': attributes}


def agent(**overrides):
    return dict(id='test', name='Test', target_entity='light.kitchen', target_property='power',
                min_value=0, max_value=1, deadband=.5, input_entities=['*'], enabled=True,
                confidence_threshold=.78, action_interval=1, micro_exploration=False,
                exploration_step=1, mode='control', training_state='qualified', **{}) | overrides
