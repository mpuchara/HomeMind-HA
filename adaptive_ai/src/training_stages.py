"""Stable display stages shared by parent and isolated worker observations."""

def stage_for(phase='', message='', detail='', preparation=None, process=None):
    if (process or {}).get('state')=='waiting_wal':
        return 'Waiting for WAL'
    if preparation:
        return preparation.get('stage','Preparing generation')
    text=(str(message)+' '+str(detail)).lower()
    if 'recorder' in text and 'complete' not in text:
        return 'Recorder import'
    if 'benchmark' in text or 'qualification' in text:
        return 'Benchmark'
    if any(word in text for word in ('serializing','model checkpoint','publishing model','generation committed')):
        return 'Publishing model'
    if any(word in text for word in ('tiny mlp','supervised','classifier','neural training')):
        return 'Learning'
    if 'replay' in text or 'historical training' in text:
        return 'Replay'
    if phase=='worker_bootstrap':
        return 'Preparing generation'
    return str(phase or 'idle')
