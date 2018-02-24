"""Classes for handling files processed by ssm-ctl

Copyright 2018 iRobot Corporation

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

from __future__ import absolute_import, print_function

import six
from six.moves import input

import re
import argparse
import collections
import sys
import getpass

import yaml

from .parameters import SSMParameter, SSMClient

class InputError(Exception):
    pass

class Input(object):
    PROMPT = True
    
    @classmethod
    def load(cls, obj):
        inputs = {}
        for name, data in six.iteritems(obj):
            if isinstance(data, six.string_types):
                data = {'Type': data}
            if data.get('Type') == 'SecureString' and 'Default' in data:
                raise ValueError("Defaults are not allowed for SecureString")
            inputs[name] = cls(
                name,
                data.get('Type'),
                pattern=data.get('Pattern'),
                description=data.get('Description'),
                default=data.get('Default'),
            )
        return inputs
    
    @classmethod
    def get_resolver(cls, inputs, prompt=True, echo=None):
        class RegionResolver(object):
            @classmethod
            def get_value(cls, *args, **kwargs):
                return SSMClient.get_region()
            
        class AccountResolver(object):
            @classmethod
            def get_value(cls, *args, **kwargs):
                return SSMClient.get_account()
        
        def resolver(name):
            if name not in inputs:
                if name == 'Region':
                    return RegionResolver
                elif name == 'Account':
                    return AccountResolver
                if prompt:
                    inputs[name] = cls(name)
                else:
                    raise InputError("Input {} not given".format(name))
            input = inputs[name]
            if not input.value_is_set():
                if prompt:
                    input.set_value_from_prompt(echo=echo)
                else:
                    raise InputError("Input {} not given".format(name))
            return input
        return resolver
    
    @classmethod
    def merge_inputs(cls, inputs, inputs_to_merge):
        """Merge second argument into the first"""
        for name, input_to_merge in six.iteritems(inputs_to_merge):
            if name not in inputs:
                inputs[name] = input_to_merge
            else:
                input = inputs[name]
                if input.type != input_to_merge.type:
                    raise TypeError("Conflicting input types for {}".format(name))
                pattern = input.pattern
                pattern_to_merge = input_to_merge.pattern
                if pattern and pattern_to_merge and pattern != pattern_to_merge:
                    raise ValueError("Conflicting input patterns for {}".format(name))
                if pattern_to_merge and not pattern:
                    input.pattern = pattern_to_merge
                if input_to_merge.description and not input.description:
                    input.description = input_to_merge.description
    
    def __init__(self, name, type=None, pattern=None, description=None, default=None):
        self.name = name
        self.type = type or 'String'
        self.pattern = pattern
        self.description = description
        self.default = default
        
        self._value = None
        
        self._encrypted_value = None
        self._decrypted_value = None
    
    def value_is_set(self):
        return self._value or self._encrypted_value
    
    def get_value(self, key_id=None):
        if key_id:
            if not self._decrypted_value:
                if self._encrypted_value:
                    self._decrypted_value = SSMClient.decrypt(self._encrypted_value, key_id)
                else:
                    self._decrypted_value = SSMClient.decrypt(self._value, key_id)
            return self._decrypted_value
        else:
            return self._value
    
    def set_value(self, value, encrypted=None):
        if self.type == 'SecureString' and encrypted is not False:
            self._encrypted_value = value
        else:
            self._value = value
    
    def set_value_from_prompt(self, echo=None):
        _echo = lambda default: default if echo is None else echo
        
        if self.type == 'String':
            self._value = self._prompt_for_string(_echo(True))
        elif self.type == 'SecureString':
            self._decrypted_value = self._prompt_for_string(_echo(False))
        elif self.type == 'StringList':
            self._value = self._prompt_for_stringlist(_echo(True))
    
    def _prompt_for_string(self, echo):
        input_fn = getpass.getpass if not echo else input
        
        type_str = ' [{}]'.format(self.type) if self.type else ''
        desc_str = ' ({})'.format(self.description) if self.description else ''
        value = input_fn('Enter {}{}{}: '.format(self.name, type_str, desc_str))
        if not value and self.default:
            return self.default
        if self.pattern and not re.search(self.pattern, value):
            raise InputError("Invalid input")
        return value
    
    def _prompt_for_stringlist(self, echo):
        input_fn = getpass.getpass if not echo else input
        print('Enter StringList {} values (blank line when done):')
        entry = input_fn('\n')
        if not entry and self.default:
            return self.default
        if ',' in entry:
            return entry
        value = [entry]
        while entry:
            entry = input_fn('\n')
            value.append(entry)
        if self.pattern and not re.search(self.pattern, ','.join(value)):
            raise InputError("Invalid input")
        return value
    
    def __str__(self):
        return repr(self)
    
    def __repr__(self):
        kwargs=[
            'name={!r}'.format(self.name),
            'type={!r}'.format(self.type),
        ]
        for name in ['pattern', 'description']:
            if getattr(self, name):
                kwargs.append('{}={!r}'.format(name, getattr(self, name)))
        return 'Input({})'.format(
            ','.join(kwargs))

ParameterFileData = collections.namedtuple('ParameterFileData', ['inputs', 'parameters', 'flush'])

INPUT_KEY = '.INPUT'
COMMON_KEY = '.COMMON'
FLUSH_KEY = '.FLUSH'

def parse_parameter_file(obj, vars_in_name_only=False):
    inputs = Input.load(obj.get(INPUT_KEY, {}))
    
    common_data = obj.get(COMMON_KEY, {})
    
    flush = obj.get(FLUSH_KEY, [])
    if isinstance(flush, six.string_types):
        flush = [flush]
    
    parameters = {}
    for name, data in obj.items():
        if name.startswith('.'):
            continue
        
        param_data = {
            'Name': name,
        }
        
        if isinstance(data, six.string_types):
            data = {
                'Type': 'String',
                'Value': data,
            }
        elif isinstance(data, list):
            data = {
                'Type': 'StringList',
                'Value': data,
            }
        
        param_data.update(common_data)
        param_data.update(data)
        
        parameters[name] = SSMParameter.load(param_data, vars_in_name_only=vars_in_name_only)
    
    return ParameterFileData(inputs, parameters, flush)

def compile_parameter_file(parameters, flush=None, ignore_disabled=False):
    ssm_param_file_data = {}
    
    if flush:
        ssm_param_file_data[FLUSH_KEY] = flush
    
    for parameter in parameters:
        if parameter.disable and ignore_disabled:
            continue
        data = parameter.dump()
        name = data.pop('Name')
        ssm_param_file_data[name] = data
    
    return ssm_param_file_data
