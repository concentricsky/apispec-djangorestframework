# encoding: utf-8


import json
import re
from collections import OrderedDict
from importlib import import_module

from apispec import APISpec
from django.apps import apps
from django.conf import settings
from django.urls import reverse
from rest_framework.fields import Field
try:
    from rest_framework.schemas.generators import EndpointEnumerator
except ImportError:
    from rest_framework.schemas import EndpointInspector as EndpointEnumerator
from rest_framework.serializers import ListSerializer, SerializerMetaclass, BaseSerializer


class APISpecDRFBuilder(object):
    @classmethod
    def get_serializer_spec(cls, serializer_class, include_read_only=True, include_write_only=False):
        """
        Generate a definition based on a serializer class decorated with @apispec_definition
        """
        return {
            'properties': cls.get_serializer_properties(serializer_class(),
                                                        include_read_only=include_read_only,
                                                        include_write_only=include_write_only),
            'required': cls.get_required_properties(serializer_class())
        }

    @classmethod
    def get_required_properties(cls, serializer):
        return [field_name for field_name, field in list(serializer.get_fields().items()) if field.required]

    @classmethod
    def get_serializer_properties(cls, serializer, include_read_only=True, include_write_only=True):
        assert isinstance(serializer, BaseSerializer)

        return OrderedDict([
            (field_name, cls.get_field_property(field))
            for field_name, field in list(serializer.get_fields().items())
            if (not field.read_only or include_read_only) and (not field.write_only or include_write_only)
        ])

    @classmethod
    def get_field_property(cls, field):
        assert isinstance(field, Field)

        if isinstance(field, ListSerializer):
            field_properties = {
                'type': "array",
                'items': {
                    '$ref': "#/definitions/{}".format(cls.get_ref_name(field.child.__class__))
                }
            }
        else:
            field_properties = {
                'type': "string",  # TODO: Support numeric fields
                'format': cls.get_ref_name(field.__class__)
            }

        help_text = getattr(field, 'help_text', None)
        if help_text:
            field_properties['description'] = help_text

        return field_properties

    @classmethod
    def get_ref_name(cls, serializer_cls):
        if cls.has_apispec_definition(serializer_cls):
            definition_name, definition_kwargs = cls.get_apispec_definition(serializer_cls)
            return definition_name
        else:
            return serializer_cls.__name__

    @classmethod
    def has_apispec_definition(cls, serializer_cls):
        return hasattr(serializer_cls, 'Meta') and hasattr(serializer_cls.Meta, 'apispec_definition')

    @classmethod
    def get_apispec_definition(cls, serializer_cls):
        if cls.has_apispec_definition(serializer_cls):
            return serializer_cls.Meta.apispec_definition
        return None, {}

    @classmethod
    def valid_scopes_for_view(cls, view, method=None):
        valid_scopes = getattr(view, "valid_scopes", [])
        if isinstance(valid_scopes, dict) and method is not None:
            for m in (method, method.lower(), method.upper()):
                if "@apispec_scopes" in valid_scopes:
                    if m in valid_scopes['@apispec_scopes']:
                        return valid_scopes['@apispec_scopes'][m]
                    return []
                elif m in valid_scopes:
                    return valid_scopes[m]
            return []

        return valid_scopes

    @classmethod
    def merge_specs(cls, base, override):
        """
        Apply override dict on top of base dict up to a depth of 1.

        Dictionary values are updated on a per-key basis in the merged list.  Lists values are concatenated.
        """
        merged = base.copy()

        for key, override_value in list(override.items()):
            if key not in base:
                merged[key] = override_value
            else:
                base_value = base[key]
                if isinstance(override_value, dict) and isinstance(base_value, dict):
                    merged[key].update(override_value)
                elif isinstance(override_value, list) and isinstance(base_value, list):
                    merged[key] = base_value + override_value
                else:
                    raise ValueError("Base and override values are not of same type")

        return merged


class APISpecDRF(APISpec, APISpecDRFBuilder):
    def __init__(self, version, preamble, *args, **kwargs):
        self.version = version
        self.preamble = preamble
        title = kwargs.pop('title', '{version} API Docs'.format(version=version))
        self.include_oauth2_security = kwargs.pop('include_oauth2_security', False)
        super(APISpecDRF, self).__init__(title, version, *args, **kwargs)
        self.scrape_serializers()
        self.scrape_endpoints()
        self.info.update({"description": preamble})

    def to_dict(self):
        # sort models alphabetically
        self._definitions = OrderedDict([(k, self._definitions[k]) for k in sorted(self._definitions.keys())])
        ret = super(APISpecDRF, self).to_dict()

        if self.include_oauth2_security:
            scope_descriptions = getattr(settings, 'OAUTH2_PROVIDER', {}).get('SCOPES', {})
            excluded_scopes = getattr(settings, 'API_DOCS_EXCLUDED_SCOPES', [])
            filtered_scopes = {k: v for k,v in list(scope_descriptions.items()) if k not in excluded_scopes}
            ret['securityDefinitions'] = {
                'oauth2': {
                    "type": "oauth2",
                    "flow": "authorizationCode",
                    "authorizationUrl": reverse('docs_authorize_redirect'),
                    "tokenUrl": reverse("oauth2_provider:token"),
                    "scopes": OrderedDict([
                        (s, filtered_scopes.get(s,s)) for s in self.scrape_endpoints_for_scopes()
                    ])
                }
            }
            ret['security'] = [
                {
                    'oauth2': [],
                }
            ]
        else:
            ret['security'] = []
        return ret

    def scrape_serializers(self):
        """
        Iterate over installed apps looking for serializers with Meta.apispec_definition
        """
        for app_config in apps.get_app_configs():
            module_name = '{app}.serializers_{version}'.format(app=app_config.name, version=self.version)
            try:
                app_serializer_module = import_module(module_name)
            except ImportError:
                pass
            else:
                for cls_name in dir(app_serializer_module):
                    cls = getattr(app_serializer_module, cls_name)

                    if isinstance(cls, SerializerMetaclass) and self.has_apispec_definition(cls):
                        definition_name, definition_kwargs = self.get_apispec_definition(cls)
                        serializer_spec = self.get_serializer_spec(cls)
                        merged_spec = self.merge_specs(serializer_spec, definition_kwargs)

                        #TODO: Is it possible to avoid the sillyness below required to extract extra_fields?
                        self.definition(definition_name,
                                        properties=merged_spec.pop('properties', None),
                                        enum=merged_spec.pop('enum', None),
                                        merged_spec=merged_spec.pop('description', None),
                                        extra_fields=merged_spec)

    def get_path_parameters(self, path):
        """
        Accepts a path with curly bracket-delimited parameters and returns a list of parameter names.

        For example:

        extract_path_parameters("/{foo}/bar/{baz}") -> [foo, baz]
        """
        return re.findall('{([^{]*)}', path)

    def get_path_parameter_list(self, path):
        return [{
            'in': 'path',
            'name': param,
            'required': True,
            'type': "string"
        } for param in self.get_path_parameters(path)]

    def scrape_endpoints_for_scopes(self):
        """
        Iterate over the registered API endpoints output all the scopes
        """
        inspector = EndpointEnumerator()
        all_scopes = set()
        for path, http_method, func in inspector.get_api_endpoints():
            all_scopes |= set(self.valid_scopes_for_view(func.cls, method=http_method))
        excluded_scopes = set(getattr(settings, 'API_DOCS_EXCLUDED_SCOPES', []))
        return sorted(list(all_scopes ^ excluded_scopes))

    def scrape_endpoints(self):
        """
        Iterate over the registered API endpoints for this version and generate path specs
        """
        inspector = EndpointEnumerator()
        for path, http_method, func in inspector.get_api_endpoints():
            http_method = http_method.lower()

            if not path.startswith('/api-auth/') and not path.startswith("/{}/".format(self.version)):  # skip if it doesnt match version
                continue

            method_func = getattr(func.cls, http_method, None)
            if self.has_apispec_wrapper(method_func):
                operation_spec = self.get_operation_spec(path, http_method, view_cls=func.cls)
                merged_spec = self.merge_specs(operation_spec, self.get_apispec_kwargs(method_func))

                path_spec = {
                    'path': path,
                    'operations': {
                        http_method: merged_spec
                    }
                }
                self.add_path(**path_spec)

    def has_apispec_wrapper(self, method_func):
        return hasattr(method_func, '_apispec_wrapped')

    def get_apispec_args(self, method_func):
        if self.has_apispec_wrapper(method_func):
            return method_func._apispec_args
        return []

    def get_apispec_kwargs(self, method_func):
        if self.has_apispec_wrapper(method_func):
            return method_func._apispec_kwargs
        return {}

    def get_operation_spec(self, path, http_method, view_cls):
        spec = {
            'parameters': self.get_path_parameter_list(path),
        }

        valid_scopes = self.valid_scopes_for_view(view_cls, method=http_method)
        if valid_scopes:
            spec['security'] = [
                {"oauth2": valid_scopes}
            ]
        else:
            spec['security'] = []

        return spec

    def write_to(self, outfile):
        outfile.write(json.dumps(self.to_dict(), indent=4))
