"""Opt-in listening experiment configuration; no model or network imports."""
from copy import deepcopy
import os


BASELINE = 'baseline'
EXPERIMENT = 'nemotron-smart-turn'
PORT = 18095


def listening_mode(value=None):
    value = os.environ.get('LIVE_LISTENING_MODE',BASELINE) if value is None else value
    if value not in (BASELINE,EXPERIMENT):
        raise ValueError('Choose listening mode baseline or nemotron-smart-turn.')
    return value


def listening_profile(config, mode=None):
    """Leave room beside the director only in the three-GPU experiment."""
    mode = listening_mode(mode)
    if mode == BASELINE:
        return config
    if config['gpus'] != 3 or not config.get('director'):
        raise ValueError('The listening experiment requires the quality-3gpu profile and three allocated H200s.')
    result = deepcopy(config)
    result['director']['memory'] = .75
    return result
