from __future__ import division
from datetime import datetime, timedelta
import time
import importlib
import json
from uuid import uuid4
from copy import copy

from pyramid.view import view_config

from nupic.frameworks.opf.metrics import MetricSpec
from nupic.frameworks.opf.modelfactory import ModelFactory
from nupic.frameworks.opf.predictionmetricsmanager import MetricsManager
from nupic.data.inference_shifter import InferenceShifter

from nupic.algorithms import anomaly_likelihood

models = {}

@view_config(route_name='reset', renderer='json')
def reset(request):
    guid = request.matchdict['guid']
    has_model = guid in models
    if has_model:
        print "resetting model", guid
        models[guid]['model'].resetSequenceStates()
        models[guid]['seen'] = 0
        models[guid]['last'] = None
        models[guid]['alh'] = anomaly_likelihood.AnomalyLikelihood()
    else:
        request.response.status = 404
        return no_model_error()
    return {'success': has_model, 'guid': guid}


def du(datestring):
    DATE_FORMATS = ['%Y-%m-%d %H:%M:%S.%f',
                    '%Y-%m-%d %H:%M:%S:%f',
                    '%Y-%m-%d %H:%M:%S',
                    '%Y-%m-%d %H:%M',
                    '%Y-%m-%d',
                    '%m/%d/%Y %H:%M',
                    '%m/%d/%y %H:%M',
                    '%Y-%m-%dT%H:%M:%S.%fZ',
                    '%Y-%m-%dT%H:%M:%SZ',
                    '%Y-%m-%dT%H:%M:%S']
    if datestring.isdigit():
        dt = datetime.utcfromtimestamp(float(unix))
    else:
        for date_format in DATE_FORMATS:
            try:
                dt = datetime.strptime(datestring, date_format)
            except ValueError:
                pass
            else:
              break
        else:
            dt = None
    return dt;    

#def dt_to_unix(dt, epoch=datetime(1970,1,1)):
#    return int((dt - datetime(1970, 1, 1)).total_seconds())

def no_model_error():
    return {'error': 'No such model'}

def serialize_result(model, result):
    temporal_field = model["tfield"];
    #if temporal_field is not None:
    #    result.rawInput[temporal_field] = dt_to_unix(result.rawInput[temporal_field])
    out = dict(
        predictionNumber=result.predictionNumber,
        rawInput=result.rawInput,
        sensorInput=dict(dataRow=result.sensorInput.dataRow,
            dataDict=result.rawInput,
            #dataEncodings=[map(int, list(l)) for l in
            #result.sensorInput.dataEncodings],
            sequenceReset=int(result.sensorInput.sequenceReset),
            category=result.sensorInput.category),
        inferences=result.inferences,
        predictedFieldIdx=result.predictedFieldIdx,
        predictedFieldName=result.predictedFieldName,
        #classifierInput=result.classifierInput,
        metrics=result.metrics)
        #bucketIndex=result.classifierInput.bucketIndex)
    anomaly_score = result.inferences["anomalyScore"]
    
    if temporal_field is not None and anomaly_score is not None:
        pfield = result.predictedFieldName
        out['anomalyLikelihood'] = model['alh'].anomalyProbability(result.rawInput[pfield], anomaly_score, result.rawInput[temporal_field])
    else:
        out['anomalyLikelihood'] = None
    return out


def find_temporal_field(model_params):
    encoders = model_params['modelParams']['sensorParams']['encoders']
    for name, encoder in encoders.iteritems():
        if encoder is not None and encoder['type'] == 'DateEncoder':
            return encoder['fieldname']


@view_config(route_name='models', renderer='json', request_method='PUT')
def run(request):
    guid = request.matchdict['guid']
    has_model = guid in models
    if not has_model:
        request.response.status = 404
        return no_model_error()
    print 'run', guid
    #print 'run', guid, 'request', request.json_body
    responseList = []
    if type(request.json_body) is list:
        rows = request.json_body
    else:
        rows = [request.json_body]
    for row in rows:
        data = row #{k: float(v) for k, v in row.items()}
        model = models[guid]
        temporal_field = model['tfield']
        if temporal_field is not None and model['last'] and (data[temporal_field] < model['last'][temporal_field]):
            request.response.status = 400
            return {'error': 'Cannot run old data'}
        model['last'] = copy(data)
        model['seen'] += 1
        # turn the timestamp field into a datetime obj
        if temporal_field is not None:
            data[temporal_field] = du(data[temporal_field])
        
        result = model['model'].run(data)
        #print 'run', guid, 'result', result

        metrics = None
        metricsManager = model['metricsManager']
        if metricsManager is not None:
            metrics = metricsManager.update(result)
            result.metrics = metrics

        inferenceShifter = model["inferenceShifter"]
        if inferenceShifter is not None:
            result = inferenceShifter.shift(result)
            result.metrics = metrics
            #print 'run', guid, 'shifted', resultObject

        responseObject = serialize_result(model, result)
        responseList.append(responseObject)

    #print 'run', guid, 'response', responseList
    return responseList


@view_config(route_name='models', renderer='json', request_method='DELETE')
def model_delete(request):
    guid = request.matchdict['guid']
    has_model = guid in models
    if not has_model:
        request.response.status = 404
        return no_model_error()
    else:
        print "Deleting model", guid
        del models[guid]
    return {'success': has_model, 'guid': guid}


@view_config(route_name='models', renderer='json', request_method='GET')
def model_get(request):
    guid = request.matchdict['guid']
    has_model = guid in models
    if not has_model:
        request.response.status = 404
        return no_model_error()
    else:
        return serialize_model(guid)


def serialize_model(guid):
    data = models[guid]
    return {
        'guid': guid,
        'metrics': data['metrics'],
        'inferenceArgs': data['inferenceArgs'],
        'params': data['params'],
        'tfield': data['tfield'],
        'last': data['last'],
        'seen': data['seen']
    }

@view_config(route_name='model_create', renderer='json', request_method='GET')
def model_list(request):
    return [serialize_model(guid) for guid in models]


@view_config(route_name='model_create', renderer='json', request_method='POST')
def model_create(request):
    guid = str(uuid4())
    metrics = None
    inferenceArgs = None
    try:
        params = request.json_body
        if 'params' in request.json_body:
            params = request.json_body['params']
        if 'metrics' in request.json_body:
            metrics = request.json_body['metrics']
        if 'inferenceArgs' in request.json_body:
            inferenceArgs = request.json_body['inferenceArgs']
    except ValueError:
        params = None

    if params:
        if 'guid' in params:
            guid = params['guid']
            if guid in models.keys():
                request.response.status = 409
                return {'error': 'The guid "' + guid + '" is not unique.'}
        if 'modelParams' not in params:
            request.response.status = 400
            return {'error': 'POST body must include JSON with a modelParams value.'}
        msg = 'Used provided model parameters'
    else:
        params = importlib.import_module('model_params.model_params').MODEL_PARAMS['modelConfig']
        print 'Using default model, timestamp is field c0'

    model = ModelFactory.create(params)

    if inferenceArgs is None:
        inferenceArgs = dict(predictedField='c1')
        print 'Using default predictedField c1'

    if inferenceArgs["predictedField"] is None:
        print 'No prediciton field'
        inferenceShifter = None
    else:
        model.enableInference(inferenceArgs)
        print model_create, guid, 'inferenceType', model.getInferenceType()
        print model_create, guid, 'inferenceArgs', model.getInferenceArgs()
        inferenceShifter = InferenceShifter();

    if metrics:
        _METRIC_SPECS = (MetricSpec(field=metric['field'], metric=metric['metric'],
                inferenceElement=metric['inferenceElement'],
                params=metric['params'])  
            for metric in metrics)
        metricsManager = MetricsManager(_METRIC_SPECS, model.getFieldInfo(), model.getInferenceType())
    else:
        metricsManager = None

    models[guid] = {
        'model': model,
        'inferenceArgs': inferenceArgs,
        'inferenceShifter': inferenceShifter,
        'params': params,
        'seen': 0,
        'last': None,
        'alh': anomaly_likelihood.AnomalyLikelihood(),
        'tfield': find_temporal_field(params),
        'metrics': metrics,
        'metricsManager': metricsManager}
    


    print "Made model", guid
    return serialize_model(guid);


