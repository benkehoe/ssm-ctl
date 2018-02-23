from __future__ import absolute_import, print_function

import six
import re

class SSMClient(object):
    @classmethod
    def _default_session_factory(cls):
        """Default session factory that creates a boto3 session."""
        import boto3
        return boto3.session.Session()

    @classmethod
    def _default_client_factory(cls, session, name):
        """Default client factory that creates a client from the provided session."""
        return session.client(name)

    SESSION_FACTORY = _default_session_factory
    CLIENT_FACTORY = _default_client_factory

    _SESSION = None
    _CLIENT = None

    @classmethod
    def _session(cls):
        """Use the defined session factory to create an object that acts like a boto3 session.
        Defaults to boto3.session.Session(); set SESSION_FACTORY to inject a different session
        factory.
        You should not need to call this method yourself; it is meant for internal use."""
        if cls._SESSION is None:
            if cls.SESSION_FACTORY:
                cls._SESSION = cls.SESSION_FACTORY()
            else:
                cls._SESSION = cls._default_session_factory()
        return cls._SESSION

    @classmethod
    def _client(cls):
        """Use the defined client factory to create an object that acts like a boto3 client.
        Defaults to _session().client("ssm"); set CLIENT_FACTORY to inject a different client
        factory."""
        if cls._CLIENT is None:
            if cls.CLIENT_FACTORY:
                client = cls.CLIENT_FACTORY(cls._session(), "ssm")
            else:
                client = cls._default_client_factory(cls._session(), "ssm")
            cls._CLIENT = client
        return cls._CLIENT
    
    _REGION = None
    _ACCOUNT = None
    
    @classmethod
    def get_region(cls):
        if not cls._REGION:
            cls._REGION = cls._session().region_name
        return cls._REGION
    
    @classmethod
    def get_account(cls):
        if not cls._ACCOUNT:
            cls._ACCOUNT = cls._session().client('sts').get_caller_identity()['Account']
        return cls._ACCOUNT   
    
    @classmethod
    def batch_put(cls, *args):
        client = cls._client()
        responses = []
        for parameters in _batch(args, 10):
            for parameter in args:
                if parameter.disable:
                    continue
                kwargs = {
                    'Name': parameter.name,
                    'Type': parameter.type,
                    'Value': parameter.value,
                }
                
                if parameter.allowed_pattern:
                    kwargs['AllowedPattern'] = parameter.allowed_pattern
                
                if parameter.description:
                    kwargs['Description'] = parameter.description
                
                if parameter.key_id:
                    kwargs['KeyId'] = parameter.key_id
                
                if parameter.overwrite:
                    kwargs['Overwrite'] = parameter.overwrite
                
                response = client.put_parameter(
                    **kwargs
                    )
                responses.append(response)
        
    @classmethod
    def get(cls, *args, **kwargs):
        invalid_parameter_names = []
        parameters = []
        
        if kwargs.get('full', False):
            #TODO: catch exceptions, store as invalid
            for name in args:
                parameter_versions = cls.get_versions(name, with_decryption=False)
                parameters.append(parameter_versions[0])
        else:
            client = cls._client()
            for names in _batch(args, n=10):
                response = client.get_parameters(
                    names = names,
                    with_decryption=False)
                invalid_parameter_names.extend(response['InvalidParameters'])
                parameters.extend(SSMParameter.load(p) for p in response['Parameters'])
        
        if invalid_parameter_names:
            raise KeyError("Invalid parameter names {}".format(', '.join(invalid_parameter_names)))
        return parameters
    
    @classmethod
    def get_versions(cls, name):
        client = cls._client()
        paginator = client.get_paginator('get_parameter_history')
        parameter_versions = []
        
        for response in paginator.paginate(
                Name=name,
                WithDecryption=False):
            for item in response['Parameters']:
                parameter = SSMParameter.load(item)
                parameter_versions.append(parameter)
        
        parameter_versions.sort(key=lambda p: p.version, reverse=True)
        return parameter_versions
    
    @classmethod
    def get_path(cls, path, names_only=False, full=False, recursive=True, parameter_filters=[]):
        if names_only and full:
            raise ValueError("Can't specify both names_only and full")
        client = cls._client()
        paginator = client.get_paginator('get_parameters_by_path')
        names = []
        parameters = []
        
        for response in paginator.paginate(
                Path=path,
                Recursive=recursive,
                ParameterFilters=parameter_filters,
                WithDecryption=False):
            if names_only or full:
                names.extend(item['Name'] for item in response['Parameters'])
            else:
                parameters.extend(SSMParameter.load(item) for item in response['Parameters'])
        
        if names_only:
            return names
        elif full:
            return cls.get(*names, full=True)
        else:
            return parameters
    
    @classmethod
    def delete(cls, *args):
        client = cls._client()
        responses = []
        for names in _batch(args, 10):
            response = client.delete_parameters(Names=names)
            responses.append(response)
    
    @classmethod
    def delete_path(cls, path, recursive=True, parameter_filters=[]):
        names = cls.get_path(path, names_only=True, recursive=recursive, parameter_filters=parameter_filters)
        return cls.delete(*names)
    
    _MASTER_KEY_PROVIDER = None
    _MASTER_KEYS = set()
    
    @classmethod
    def _default_encrypter(cls, plaintext, key_id):
        import aws_encryption_sdk
        return aws_encryption_sdk.encrypt(
                source=plaintext,
                key_provider=cls.get_master_key_provider(key_id))
    
    @classmethod
    def _default_decrypter(cls, ciphertext, key_id):
        import aws_encryption_sdk
        return aws_encryption_sdk.decrypt(
                source=ciphertext,
                key_provider=cls.get_master_key_provider(key_id))
    
    _ENCRYPTER = _default_encrypter
    _DECRYPTER = _default_decrypter
    
    @classmethod
    def get_master_key_provider(cls, key_id):
        if not cls._MASTER_KEY_PROVIDER:
            import aws_encryption_sdk
            cls._MASTER_KEY_PROVIDER = aws_encryption_sdk.KMSMasterKeyProvider()
        if key_id not in cls._MASTER_KEYS:
            cls._MASTER_KEY_PROVIDER.add_master_key(key_id)
            cls._MASTER_KEYS.add(key_id)
        return cls._MASTER_KEY_PROVIDER
    
    @classmethod
    def encrypt(cls, plaintext, key_id):
        return cls._ENCRYPTER(plaintext, key_id) 
    
    @classmethod
    def decrypt(cls, ciphertext, key_id):
        return cls._DECRYPTER(ciphertext, key_id) 

class VarString(object):
    _VAR_NAME_PATTERN_STR = r'\w+'
    _VAR_NAME_PATTERN = re.compile(_VAR_NAME_PATTERN_STR)
    _REFERENCE_PATTERN_STR = r'\$\(({})\)'.format(_VAR_NAME_PATTERN_STR)
    _REFERENCE_PATTERN = re.compile(_REFERENCE_PATTERN_STR)
    
    NAMES = set()
    _VAR_VALUES = {}
    
    @classmethod
    def get_reference_pattern(cls, name=None):
        if name is None:
            return cls._REFERENCE_PATTERN
        pattern = re.escape('$({})'.format(name))
        return re.compile(pattern)
    
    @classmethod
    def single_reference(cls, s):
        match = cls._REFERENCE_PATTERN.fullmatch(s)
        if match:
            return s
        if cls._VAR_NAME_PATTERN.fullmatch(s):
            return '$({})'.format(s)
        raise ValueError("{} is not a valid single reference".format(s))
    
    @classmethod
    def resolve(cls, resolver):
        for name in sorted(cls.NAMES):
            cls._VAR_VALUES[name] = resolver(name)
    
    @classmethod
    def load(cls, obj, key_id=None):
        if not isinstance(obj, six.string_types):
            return obj
        return cls(obj, key_id)
    
    @classmethod
    def dump(cls, obj):
        if isinstance(obj, cls):
            return obj.value
        else:
            return obj
    
    def __init__(self, s, key_id):
        self.string = s
        
        self.names = self.get_reference_pattern().findall(s)
        self.NAMES.update(self.names)
        
        self._key_id = key_id
        
        self._value = None if self.names else self.string
    
    @property
    def value(self):
        if not self._value:
            value = self.string
            for name in self.names:
                pattern = self.get_reference_pattern(name)
                key_id = self.dump(self._key_id)
                var_value = self._VAR_VALUES[name].get_value(key_id=key_id)
                value = pattern.sub(var_value, value)
            self._value = value
        return self._value
    
    def __eq__(self, other):
        if isinstance(other, six.string_types):
            return self.string == other
        else:
            return self.string == other.string
    
    def __hash__(self):
        return hash(self.string)
    
    def __str__(self):
        if self._value:
            return self._value
        else:
            return self.string
    
    def __repr__(self):
        value_str = ',value={!r}'.format(self._value) if self._value else ''
        return 'load_varstring({}{})'.format(self.string, value_str)

class SSMParameter(object):
    NAME_PATTERN = r'^(/[a-zA-Z0-9.-_]+)+$'
    
    OVERWRITE_DEFAULT = False
    
    @classmethod
    def load(cls, obj, vars_in_name_only=False):
        load_varstring = (lambda o, k=None: o) if vars_in_name_only else VarString.load
        
        name = VarString.load(obj['Name'])
        
        type = obj.get('Type')
        if not type:
            if 'KeyId' in obj:
                type = 'SecureString'
            elif not isinstance(obj['Value'], six.string_types):
                type = 'StringList'
            else:
                type = 'String'
            type = 'SecureString' if 'KeyId' in obj else 'String'
        
        key_id = load_varstring(obj.get('KeyId'))
        
        value = None
        if type == 'SecureString':
            if not key_id:
                raise ValueError("SecureString requires KeyId")
            if 'EncryptedValue' in obj:
                value = load_varstring(obj['EncryptedValue'], key_id=key_id)
            elif 'Input' in obj:
                value = load_varstring(VarString.single_reference(obj['Input']), key_id=key_id)
            elif 'Value' in obj:
                raise ValueError("Value cannot be used with SecureString")
        elif isinstance(obj.get('Value'), list):
            value = [load_varstring(s) for s in obj['Value']]
        else:
            value = load_varstring(obj.get('Value'))
        
        allowed_pattern = load_varstring(obj.get('AllowedPattern'))
        
        description = obj.get('Description')
        
        overwrite = obj.get('Overwrite')
        
        disable = load_varstring(obj.get('Disable', obj.get('Disabled')))
        
        parameter = cls(name, type, value,
            allowed_pattern=allowed_pattern,
            description=description,
            key_id=key_id,
            overwrite=overwrite,
            disable=disable)
        
        parameter.version = obj.get('Version')
        parameter.last_modified_date = obj.get('LastModifiedDate')
        parameter.last_modified_user = obj.get('LastModifiedUser')
        
        return parameter
    
    def dump(self):
        data = {
            'Name': self.name,
            'Type': self.type,
            'Value': self.value,
        }
        
        if self.allowed_pattern:
            data['AllowedPattern'] = self.allowed_pattern
        
        if self.description:
            data['Description'] = self.description
        
        if self.key_id:
            data['KeyId'] = self.key_id
        
        return data
        
    def __init__(self, name, type, value,
                 allowed_pattern=None,
                 description=None,
                 key_id=None,
                 overwrite=None,
                 disable=None):
        
        self._name = name
        self._type = type
        self._value = value
        
        self._allowed_pattern = allowed_pattern
        self._description = description
        self._key_id = key_id
        self._overwrite = overwrite
        
        self._disable = disable
        
        self.version = None
        self.last_modified_date = None
        self.last_modified_user = None
        
        if (self._key_id and not self._type == 'SecureString') or (self._type == 'SecureString' and not self._key_id):
            raise ValueError('Mismatched secure inputs on parameter {}'.format(name))
    
    def __str__(self):
        return repr(self)
    
    def __repr__(self):
        kwargs=[
            'name={!r}'.format(self.name),
            'type={!r}'.format(self.type),
            'value={!r}'.format(self.value),
        ]
        for name in ['allowed_pattern', 'description', 'key_id', 'overwrite', 'disable']:
            if getattr(self, name):
                kwargs.append('{}={!r}'.format(name, getattr(self, name)))
        return 'SSMParameter({})'.format(
            ','.join(kwargs))
    
    @property
    def name(self):
        name = VarString.dump(self._name)
#         if not re.match(self.NAME_PATTERN, name):
#             raise ValueError("Invalid name: {}".format(name))
        return name
    
    @property
    def type(self):
        return self._type
    
    @property
    def value(self):
        if self._value is None:
            if self.disable:
                return self._value
            raise ValueError("Value missing for parameter {}".format(self.name))
        value = VarString.dump(self._value)
        if not isinstance(value, six.string_types):
            value = ','.join(VarString.dump(v) for v in self._value)
        return value
    
    @property
    def allowed_pattern(self):
        return VarString.dump(self._allowed_pattern)
    
    @property
    def description(self):
        return self._description
    
    @property
    def key_id(self):
        return VarString.dump(self._key_id)
    
    @property
    def secure(self):
        return self.type == 'SecureString'
    
    @property
    def overwrite(self):
        if self._overwrite is not None:
            return bool(self._overwrite)
        else:
            return self.OVERWRITE_DEFAULT
    
    @property
    def disable(self):
        if self._disable is None:
            return False
        else:
            return bool(VarString.dump(self._disable))
    
    def put(self):
        SSMClient.batch_put(self)

class SSMParameterRequirement(object):
    def __init__(self):
        self.name = None #regex
        self.allowed = None #bool
        self.type = None #string
        self.value = None #regex
        self.secure = None #bool
        self.key_id = None #regex
        self.allowed_pattern = None #string
        self.description = None
    
    def validate(self, parameter):
        if self.name and not re.search(self.name, parameter.name):
            return True, []
        errors = []
        if self.type and parameter.type != self.type:
            errors.append("type is {} not {}".format(parameter.type, self.type))
        if self.value and not re.search(self.value, parameter.value):
            errors.append("value {} is invalid", parameter.value)
        if self.secure and not parameter.secure:
            errors.append("is not secure")
        if self.key_id and not re.search(self.key_id, parameter.key_id):
            errors.append("key id {} is invalid".format(parameter.key_id))
        if self.allowed_pattern and self.allowed_pattern != parameter.allowed_pattern:
            errors.append("allowed pattern {} is invalid".format(parameter.allowed_pattern))
        return not bool(errors), errors

def _batch(iterable, n):
    l = len(iterable)
    for ndx in range(0, l, n):
        yield iterable[ndx:min(ndx + n, l)]