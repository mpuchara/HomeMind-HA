def validate(evidence, parent, *, sensor_universe=None,topology_information_ts=None,
             universe_revision=None,universe_count=None):
    if evidence is None: raise ValueError(parent.get('affected_sensor_status') or 'affected_declaration_missing')
    if type(evidence['contract_version']) is not int or evidence['contract_version']!=VERSION: raise ValueError('affected_unsupported_contract')
    doc=validate_declaration((evidence['declaration_revision'],evidence['declaration_json'],evidence['declaration_checksum']))
    if not doc['complete'] or evidence['complete'] is not True:
        raise ValueError('affected_declaration_incomplete')
    if evidence['affected_sensor_ids']!=doc['affected_sensor_ids']:
        raise ValueError('affected_sensor_set_mismatch')
    if (evidence['agent_id']!=parent['agent']['id'] or doc['agent_id']!=evidence['agent_id']
            or evidence['generation_id']!=parent['generation_id'] or doc['generation_id']!=parent['generation_id']
            or evidence['decision_id']!=parent['decision_id'] or evidence['decision_ts']!=parent['decision_ts']
            or evidence['registry_revision']!=parent['registry_revision'] or evidence['collector_epoch']!=parent['epoch']
            or doc['topology_revision']!=parent['topology'] or doc['config_revision']!=parent['config_revision']
            or doc['logical_device_id']!=parent['descriptor']['logical_device_id']
            or doc['target_entity']!=parent['agent']['target_entity'] or doc['target_property']!=parent['agent']['target_property']):
        raise ValueError('affected_decision_binding_mismatch')
    at=parent['inference_started_ts']
    available=evidence['information_available_ts']
    if (isinstance(available,bool) or not isinstance(available,(int,float)) or not math.isfinite(available)
            or not parent['created_ts']<=available<=parent['available_ts']<=at
            or any(doc[k]>available for k in ('recorded_ts','approved_ts','configuration_information_ts','topology_information_ts'))):
        raise ValueError('affected_future_information')
    if sensor_universe is not None:
        universe=sorted(set(sensor_universe))
        if doc['sensor_universe_count']!=len(universe) or doc['sensor_universe_revision']!=digest(encoded(universe)):
            raise ValueError('affected_historical_sensor_universe_mismatch')
        if not set(doc['affected_sensor_ids'])<=set(universe):
            raise ValueError('affected_unknown_sensors')
    if topology_information_ts is not None and doc['topology_information_ts']!=topology_information_ts:
        raise ValueError('affected_topology_information_mismatch')
    if universe_revision is not None and (doc['sensor_universe_revision']!=universe_revision or doc['sensor_universe_count']!=universe_count):
        raise ValueError('affected_historical_sensor_universe_mismatch')
    return tuple(doc['affected_sensor_ids'])
