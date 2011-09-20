# Copyright (C) 2011 Oregon State University et al.
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301,
# USA.

__author__ = 'bojan'

import ganeti_web.views.users
import ganeti_web.views.jobs
import ganeti_web.views.node
import ganeti_web.views.virtual_machine
from object_permissions.registration import get_group_perms
import object_log.views
import ganeti_web.views.users
from ganeti_web.views.jobs import status, clear
import ganeti_web.views.jobs
import ganeti_web.views.node
import ganeti_web.views.virtual_machine
import object_permissions
from tastypie.utils.dict import dict_strip_unicode_keys
from tastypie.models import ApiKey
import api.utils
import object_permissions.views.groups
import object_permissions.views.permissions
import utils
from tastypie import fields
from tastypie.authentication import ApiKeyAuthentication
from tastypie.authorization import DjangoAuthorization
from tastypie.exceptions import NotFound
from django.http import HttpResponse, Http404
import ganeti_web.views.users
from ganeti_web.views.virtual_machine import list_
import ganeti_web.views.jobs
import ganeti_web.views.node
import ganeti_web.views.virtual_machine
from tastypie.resources import ModelResource, HttpAccepted, HttpBadRequest
from django.contrib.auth.models import User, Group
from ganeti_web.models import VirtualMachine, SSHKey, Cluster, Node, Job, ClusterUser
from django.core.exceptions import ObjectDoesNotExist, MultipleObjectsReturned
from tastypie.http import HttpMultipleChoices, HttpNotFound

class UserResource(ModelResource):
    """
    Defines user resource, providing ssh_keys additionally
    """
    class Meta:
        queryset = User.objects.all()
        resource_name = 'user'
        allowed_methods = ['get', 'put', 'post', 'delete']
        authentication = ApiKeyAuthentication()
        authorization = DjangoAuthorization()
        #authorization = SuperuserAuthorization()
        #validation = UserValidation()
        filtering = {}


    def dehydrate(self, bundle):
        ssh_keys = []
        for key in SSHKey.objects.filter(user__pk=bundle.obj.id):
            ssh_keys.append({ 'key':key.key, 'id':key.id })
        bundle.data['ssh_keys'] = ssh_keys
        try:
            bundle.data['api_key'] = ApiKey.objects.get(user__pk=bundle.obj.id).key
        except ApiKey.DoesNotExist:
            bundle.data['api_key'] = {}
        used_resources = []

        # group memberships
        bundle.data['groups'] = []
        groups = bundle.obj.groups
        for group in groups.all():
            bundle.data['groups'].append(GroupResource().get_resource_uri(group))

        # user actions
        user_actions = object_log.views.list_user_actions(bundle.request, bundle.obj.id, rest=True)
        bundle.data['user_actions'] = api.utils.extract_log_actions(bundle.request, bundle.obj.id, user_actions)

        # actions on user
        actions_on_user = object_log.views.list_for_user(bundle.request, bundle.obj.id, rest=True)
        bundle.data['actions_on_user'] = api.utils.extract_log_actions(bundle.request, bundle.obj.id, actions_on_user)

        # user permissions
        perm_objects = bundle.obj.get_all_objects_any_perms(groups=False)
        obj_res_instances = {VirtualMachine:VMResource, Group:GroupResource, Cluster:ClusterResource}

        cluster_user = ClusterUser.objects.get(name=bundle.obj.username)

        perm_results = {}
        for key in perm_objects.keys():
            objects = perm_objects.get(key)
            temp_obj = []
            for object in objects:
                if obj_res_instances.has_key(key):
                    temp_obj_perms = []
                    for loc_perm in bundle.obj.get_all_permissions(object):
                        temp_obj_perms.append(loc_perm)
                    temp_obj.append({'object':obj_res_instances.get(key)().get_resource_uri(object),'permissions':temp_obj_perms})
                    used_resources.append({'object':obj_res_instances.get(key)().get_resource_uri(object), 'type':key.__name__ ,'resource':cluster_user.used_resources(object,only_running = True)})
            perm_results[key.__name__]=temp_obj
        bundle.data['permissions'] = perm_results

        bundle.data['used_resources'] = used_resources

        return bundle


    def put_detail(self, request, **kwargs):
        try:
            deserialized = self.deserialize(request, request.raw_post_data, format=request.META.get('CONTENT_TYPE', 'application/json'))
        except Exception:
            return HttpBadRequest()
        deserialized = self.alter_deserialized_detail_data(request, deserialized)
        bundle = self.build_bundle(data=dict_strip_unicode_keys(deserialized))
        self.is_valid(bundle, request)
        if bundle.data.has_key('api_key'):
            return HttpBadRequest()
        self.obj_update(bundle, request=request, pk=kwargs.get('pk'))

        return HttpAccepted

    def post_detail(self, request, **kwargs):
        try:
            deserialized = self.deserialize(request, request.raw_post_data, format=request.META.get('CONTENT_TYPE', 'application/json'))
        except Exception:
            return HttpBadRequest()
        deserialized = self.alter_deserialized_detail_data(request, deserialized)
        bundle = self.build_bundle(data=dict_strip_unicode_keys(deserialized))

        # action: generate api key for particular user
        if (bundle.data.has_key('action')) & (bundle.data.get('action')=='generate_api_key'):
            return api.utils.generate_api_key(request, kwargs.get('pk'))

        # clean users api key
        if (bundle.data.has_key('action')) & (bundle.data.get('action')=='clean_api_key'):
            return api.utils.clean_api_key(request, kwargs.get('pk'))

        # action: add user to group
        if (bundle.data.has_key('action')) & bundle.data.has_key('group') & (bundle.data.get('action')=='add_to_group'):
            #print GroupResource().get_via_uri(bundle.data.get('group'))
            User.objects.get(id=kwargs.get('pk')).groups.add(GroupResource().get_via_uri(bundle.data.get('group')))

        # action: remove user from the group
        if (bundle.data.has_key('action')) & bundle.data.has_key('group') & (bundle.data.get('action')=='remove_from_group'):
            User.objects.get(id=kwargs.get('pk')).groups.remove(GroupResource().get_via_uri(bundle.data.get('group')))


        return HttpResponse(status=204)


    def post_list(self, request, **kwargs):
        try:
            deserialized = self.deserialize(request, request.raw_post_data, format=request.META.get('CONTENT_TYPE', 'application/json'))
        except Exception:
            return HttpBadRequest()
        deserialized = self.alter_deserialized_detail_data(request, deserialized)
        bundle = self.build_bundle(data=dict_strip_unicode_keys(deserialized))

        # action: generate api key for particular user
        if (bundle.data.has_key('action')) & (bundle.data.get('action')=='generate_api_key') & (bundle.data.has_key('userid')):
            return api.utils.generate_api_key(request, bundle.data.get('userid'))

        # clean users api key
        if (bundle.data.has_key('action')) & (bundle.data.get('action')=='clean_api_key') & (bundle.data.has_key('userid')):
            return api.utils.clean_api_key(request, bundle.data.get('userid'))

        return HttpResponse(status=204)


    def build_schema(self):
        dict = super(UserResource, self).build_schema()
        dict['fields']['groups'] = { 'help_text': 'Returns the groups the user is member of',
                                    'read_only': True,
                                    'type': 'related',
                                    'nullable': True }
        dict['fields']['actions_on_user'] = { 'help_text': 'Returns the actions done on the user. The list is composed of objects, containing elements as described here.',
                                    'read_only': True,
                                    'type': 'list',
                                    'object' : {
                                        'obj1': {'help_text':'Describes action object', 'read_only':True, 'type':'related', 'nullable':True},
                                        'obj2': {'help_text':'Describes action object', 'read_only':True, 'type':'related', 'nullable':True},
                                        'user': {'help_text':'User performed the action', 'read_only':True, 'type':'related', 'nullable':True},
                                        'timestamp': {'help_text':'A date and time of action', 'read_only':True, 'type':'datetime', 'nullable':True},
                                        'action_name': {'help_text':'Describes action name using internal descriptions', 'read_only':True, 'type':'string', 'nullable':True}
                                    },
                                    'nullable': False }
        dict['fields']['user_actions'] = { 'help_text': 'Returns the actions done by the user. The list is composed of objects, containing elements as described here.',
                                    'read_only': True,
                                    'type': 'list',
                                    'object' : {
                                        'obj1': {'help_text':'Describes action object', 'read_only':True, 'type':'related', 'nullable':True},
                                        'obj2': {'help_text':'Describes action object', 'read_only':True, 'type':'related', 'nullable':True},
                                        'user': {'help_text':'User performed the action', 'read_only':True, 'type':'related', 'nullable':True},
                                        'timestamp': {'help_text':'A date and time of action', 'read_only':True, 'type':'datetime', 'nullable':True},
                                        'action_name': {'help_text':'Describes action name using internal descriptions', 'read_only':True, 'type':'string', 'nullable':True}
                                    },
                                    'nullable': False }

        dict['fields']['ssh_keys'] = { 'help_text': 'SSH keys for user\'s account. The list may be composed of several objects, each designed by unique id.',
                                    'read_only': False,
                                    'type': 'list',
                                    'nullable': True }
        dict['fields']['api_key'] = { 'help_text': 'Returns the api key of the user',
                                    'read_only': True,
                                    'type': 'string',
                                    'nullable': True }
        dict['fields']['used_resources'] = { 'help_text': 'Returns the resources used by the objects user has access to in the form of the list.',
                                    'read_only': True,
                                    'type': 'list',
                                    'object' :{
                                        'object': {'help_text':'Describes object consuming resources', 'read_only':True, 'type':'related', 'nullable':False},
                                        'type': {'help_text':'Describes type of the object consuming resources', 'read_only':True, 'type':'string', 'nullable':False},
                                        'resource': {'help_text':'Contains a list of particular resources consumed by the object', 'read_only':True, 'type':'list',
                                                           'virtual_cpus' : {'help_text':'Virtual CPUs used by the object', 'read_only':True, 'type':'integer', 'nullable':True},
                                                           'disk' : {'help_text':'Disk space used by the object', 'read_only':True, 'type':'integer', 'nullable':True},
                                                           'ram' : {'help_text':'Memory (RAM) used by the object', 'read_only':True, 'type':'integer', 'nullable':True},
                                                           'nullable':False},
                                        
                                        },
                                    'nullable': True }
        dict['fields']['permissions'] = { 'help_text': 'Returns the status of users permissions on different families of objects',
                                    'read_only': True,
                                    'type': 'list',
                                    'Cluster': {'help_text': 'Contains the list of Cluster objects user has permissions on.', 'type':'list', 'nullable':False, 'read_only':True,
                                    'object': {'help_text': 'Related cluster object user has permissions on.', 'type':'related', 'nullable':False, 'read_only':True},
                                    'permissions': {'help_text': 'List containing particular permissions on designated cluster object. Permissions are described by value fields, using internal string notation.', 'type':'string', 'nullable':False, 'read_only':True}
                                    },
                                    'VirtualMachine': {'help_text': 'Contains the list of VirtualMachine objects user has permissions on.', 'type':'list', 'nullable':False, 'read_only':True,
                                    'object': {'help_text': 'Related VirtualMachine object user has permissions on.', 'type':'related', 'nullable':False, 'read_only':True},
                                    'permissions': {'help_text': 'List containing particular permissions on designated VirtualMachine object. Permissions are described by value fields, using internal string notation.', 'type':'string', 'nullable':False, 'read_only':True}
                                    },
                                    'Group': {'help_text': 'Contains the list of Group objects user has permissions on.', 'type':'list', 'nullable':False, 'read_only':True},
                                    'nullable': True }

        #print dict
        #utils.generate_wiki_basic_table(dict['fields'])
        return dict

    
class GroupResource(ModelResource):
    """
    Defines group resource
    """
    class Meta:
        queryset = Group.objects.all()
        resource_name = 'group'
        allowed_methods = ['get', 'put', 'post', 'delete']
        authentication = ApiKeyAuthentication()
        authorization = DjangoAuthorization()

    def dehydrate(self, bundle):

        # permissions on virtual machines and clusters
        perms_info = object_permissions.views.groups.all_permissions(bundle.request, bundle.data['id'], rest=True)

        if perms_info.has_key('error'):
            return bundle
        bundle.data['permissions']={}
        bundle.data['users']=[]
        bundle.data['used_resources']=[]
        used_resources = []
        
        cluster_user = ClusterUser.objects.get(name=bundle.obj.name)
        obj_res_instances = {'VirtualMachine':VMResource, 'Group':GroupResource, 'Cluster':ClusterResource}

        perm_results = {}
        if perms_info.has_key('perm_dict'):
            for key in perms_info.get('perm_dict').keys():
                objects = perms_info.get('perm_dict').get(key)
                temp_obj = []
                for object in objects:
                    if obj_res_instances.has_key(key):
                        temp_obj.append({'object':obj_res_instances.get(key)().get_resource_uri(object),'permissions':get_group_perms(bundle.obj, object)})
                        used_resources.append({'object':obj_res_instances.get(key)().get_resource_uri(object), 'type':key, 'resource':cluster_user.used_resources(object,only_running = False)})
                perm_results[key]=temp_obj
        bundle.data['permissions'] = perm_results


        bundle.data['permissions'] = perm_results

        bundle.data['used_resources'] = used_resources


        # group users
        users = User.objects.filter(groups=bundle.obj.id)
        for user in users:
            bundle.data['users'].append(UserResource().get_resource_uri(user))

        # used resources by group objects
        bundle.data['used_resources'] = used_resources

        # actions on group
        actions_on_group = object_log.views.list_for_group(bundle.request, bundle.obj.id, True)
        bundle.data['actions_on_group'] = api.utils.extract_log_actions(bundle.request, bundle.obj.id, actions_on_group)

        return bundle


    def put_detail(self, request, **kwargs):
        try:
            deserialized = self.deserialize(request, request.raw_post_data, format=request.META.get('CONTENT_TYPE', 'application/json'))
        except Exception:
            return HttpBadRequest()
        deserialized = self.alter_deserialized_detail_data(request, deserialized)
        bundle = self.build_bundle(data=dict_strip_unicode_keys(deserialized))
        self.is_valid(bundle, request)
        if bundle.data.has_key('api_key'):
            return HttpBadRequest()
        self.obj_update(bundle, request=request, pk=kwargs.get('pk')) # updated_bundle = ...
        return HttpAccepted

    def post_detail(self, request, **kwargs):
        try:
            deserialized = self.deserialize(request, request.raw_post_data, format=request.META.get('CONTENT_TYPE', 'application/json'))
        except Exception:
            return HttpBadRequest()
        deserialized = self.alter_deserialized_detail_data(request, deserialized)
        bundle = self.build_bundle(data=dict_strip_unicode_keys(deserialized))

        # action: add user to group
        if (bundle.data.has_key('action')) & bundle.data.has_key('user') & (bundle.data.get('action')=='add_user'):
            Group.objects.get(id=kwargs.get('pk')).user_set.add(UserResource().get_via_uri(bundle.data.get('user')))

        # action: remove user from the group
        if (bundle.data.has_key('action')) & bundle.data.has_key('user') & (bundle.data.get('action')=='remove_user'):
            Group.objects.get(id=kwargs.get('pk')).user_set.remove(UserResource().get_via_uri(bundle.data.get('user')))

        return HttpResponse(status=204)



    def build_schema(self):
        dict = super(GroupResource, self).build_schema()

        dict['fields']['actions_on_group'] = { 'help_text': 'Returns the actions done on the group. The list is composed of objects, containing elements as described here.',
                                    'read_only': True,
                                    'type': 'list',
                                    'object' : {
                                        'obj1': {'help_text':'Describes action object', 'read_only':True, 'type':'related', 'nullable':True},
                                        'obj2': {'help_text':'Describes action object', 'read_only':True, 'type':'related', 'nullable':True},
                                        'user': {'help_text':'User performed the action', 'read_only':True, 'type':'related', 'nullable':True},
                                        'timestamp': {'help_text':'A date and time of action', 'read_only':True, 'type':'datetime', 'nullable':True},
                                        'action_name': {'help_text':'Describes action name using internal descriptions', 'read_only':True, 'type':'string', 'nullable':True}
                                    },
                                    'nullable': False }

        dict['fields']['users'] = { 'help_text': 'Returns a list of the users belonging to the group.',
                                    'read_only' : False,'type': 'related', 'nullable':True }
        dict['fields']['used_resources'] = { 'help_text': 'Returns the resources used by the objects the group has access to in the form of the list.',
                                    'read_only': True,
                                    'type': 'list',
                                    'object' :{
                                        'object': {'help_text':'Describes object consuming resources', 'read_only':True, 'type':'related', 'nullable':False},
                                        'type': {'help_text':'Describes type of the object consuming resources', 'read_only':True, 'type':'string', 'nullable':False},
                                        'resource': {'help_text':'Contains a list of particular resources consumed by the object', 'read_only':True, 'type':'list',
                                                           'virtual_cpus' : {'help_text':'Virtual CPUs used by the object', 'read_only':True, 'type':'integer', 'nullable':True},
                                                           'disk' : {'help_text':'Disk space used by the object', 'read_only':True, 'type':'integer', 'nullable':True},
                                                           'ram' : {'help_text':'Memory (RAM) used by the object', 'read_only':True, 'type':'integer', 'nullable':True},
                                                           'nullable':False},

                                        },
                                    'nullable': True }
        dict['fields']['permissions'] = { 'help_text': 'Returns the status of users permissions on different families of objects',
                                    'read_only': True,
                                    'type': 'list',
                                    'Cluster': {'help_text': 'Contains the list of Cluster objects user has permissions on.', 'type':'list', 'nullable':False, 'read_only':True,
                                    'object': {'help_text': 'Related cluster object user has permissions on.', 'type':'related', 'nullable':False, 'read_only':True},
                                    'permissions': {'help_text': 'List containing particular permissions on designated cluster object. Permissions are described by value fields, using internal string notation.', 'type':'string', 'nullable':False, 'read_only':True}
                                    },
                                    'VirtualMachine': {'help_text': 'Contains the list of VirtualMachine objects user has permissions on.', 'type':'list', 'nullable':False, 'read_only':True,
                                    'object': {'help_text': 'Related VirtualMachine object user has permissions on.', 'type':'related', 'nullable':False, 'read_only':True},
                                    'permissions': {'help_text': 'List containing particular permissions on designated VirtualMachine object. Permissions are described by value fields, using internal string notation.', 'type':'string', 'nullable':False, 'read_only':True}
                                    },
                                    'Group': {'help_text': 'Contains the list of Group objects user has permissions on.', 'type':'list', 'nullable':False, 'read_only':True},
                                    'nullable': True }
        #utils.generate_wiki_basic_table(dict['fields'])
        return dict
    

class ClusterResource(ModelResource):

    class Meta:
        resource_name = 'cluster'
        object_class = Cluster
        allowed_methods=['get']
        queryset = Cluster.objects.all()
        fields = ['username','id', 'disk', 'description','hostname', 'ram','slug','port','virtual_cpus','slug']

    def dehydrate(self, bundle):
        bundle.data['software_version'] = bundle.obj.info['software_version']
        bundle.data['default_hypervisor'] = bundle.obj.info['default_hypervisor']
        bundle.data['master'] = bundle.obj.info['master']
        bundle.data['nodes_count'] = bundle.obj.nodes.count()
        bundle.data['vm_count'] = bundle.obj.virtual_machines.count()
        bundle.data['info'] = bundle.obj.info
        bundle.data['quota'] = bundle.obj.get_quota(user=User.objects.get(username=bundle.request.META['USER']).id if (bundle.request.META.has_key("USER")) else None)
        bundle.data['default_quota'] = bundle.obj.get_default_quota()
        bundle.data['available_ram'] = bundle.obj.available_ram
        bundle.data['available_disk'] = bundle.obj.available_disk
        bundle.data['missing_db'] = bundle.obj.missing_in_db
        bundle.data['missing_ganeti'] = bundle.obj.missing_in_ganeti
        bundle.data['nodes_missing_db'] = bundle.obj.nodes_missing_in_db
        bundle.data['nodes_missing_ganeti'] = bundle.obj.nodes_missing_in_ganeti
        return bundle

    def get_object_list(self, request):
        objects = Cluster.objects.all()
        return objects

    def obj_get_list(self, request=None, **kwargs):
        return self.get_object_list(request)

    def build_schema(self):
        dict = super(ClusterResource, self).build_schema()
        dict['fields']['software_version'] = { 'help_text': 'Returns a software version.',
                                    'read_only' : True,'type': 'string', 'nullable':False }
        dict['fields']['default_hypervisor'] = { 'help_text': 'Returns a default hypervisor for the cluster.',
                                    'read_only' : True,'type': 'string', 'nullable':False }
        dict['fields']['master'] = { 'help_text': 'Returns master node',
                                    'read_only' : True,'type': 'string', 'nullable':False }
        dict['fields']['nodes_count'] = { 'help_text': 'Returns nodes count for the cluster.',
                                    'read_only' : True,'type': 'Integer', 'nullable':True }
        dict['fields']['vm_count'] = { 'help_text': 'Returns a number of virtual machines on the cluster.',
                                    'read_only' : True,'type': 'Integer', 'nullable':True }
        dict['fields']['info'] = { 'help_text': 'Complex container exposing many information related to the cluster. More details with example can be found in documentation/wiki.',
                                    'read_only' : True,'type': 'list', 'nullable':True }
        dict['fields']['quota'] = { 'help_text': 'Returns a list containing objects describing quotas for the user performing the request.',
                                    'read_only' : True,'type': 'list', 'nullable':True }
        dict['fields']['default_quota'] = { 'help_text': 'Returns a list containing objects describing default quotas.',
                                    'read_only' : True,'type': 'list', 'nullable':True }
        dict['fields']['available_ram'] = { 'help_text': 'Returns a list with elements describing RAM status, including total, allocated, used and free memory.',
                                    'read_only' : True,'type': 'list', 'nullable':True }
        dict['fields']['available_disk'] = { 'help_text': 'Returns a list with elements describing disk status, including total, allocated, used and free disk space.',
                                    'read_only' : True,'type': 'list', 'nullable':True }
        dict['fields']['missing_db'] = { 'help_text': 'Returns a list with names of missing nodes in DB.',
                                    'read_only' : True,'type': 'list', 'nullable':True }
        dict['fields']['missing_ganeti'] = { 'help_text': 'Returns a list with names of missing nodes in ganeti.',
                                    'read_only' : True,'type': 'list', 'nullable':True }
        #utils.generate_wiki_basic_table(dict['fields'])
        return dict


class NodeResource(ModelResource):
    cluster = fields.ForeignKey(ClusterResource, 'cluster', null=True)
    last_job = fields.ForeignKey('api.resource.JobResource', 'last_job', null=True)

    # TODO: Node actions: migrate, evacuate, role

    class Meta:
        queryset = Node.objects.all()
        object_class = Node
        resource_name = 'node'
        allowed_methods = ['get']
        fields = {'cpus', 'ram_total', 'ram_free', 'disk_total', 'disk_free', 'role', 'offline', 'id'}
        authentication = ApiKeyAuthentication()
        authorization = DjangoAuthorization()

    def dehydrate(self, bundle):
        node = bundle.obj
        node_detail = ganeti_web.views.node.detail(bundle.request, node.cluster.slug, node.hostname, True)
        bundle.data['cluster'] = ClusterResource().get_resource_uri(node_detail['cluster'])
        bundle.data['node_count'] = node_detail['node_count']
        bundle.data['admin'] = node_detail['admin']
        bundle.data['modify'] = node_detail['modify']
        bundle.data['info'] = node_detail['node'].info
        bundle.data['hostname'] = node_detail['node'].hostname
        primary_list = ganeti_web.views.node.primary(bundle.request, node.cluster.slug, node.hostname, True)
        secondary_list = ganeti_web.views.node.secondary(bundle.request, node.cluster.slug, node.hostname, True)
        plist = []
        slist = []
        for vms in primary_list:
            plist.append({'resource':VMResource().get_resource_uri(vms),'hostname':vms.hostname})
        for vms in secondary_list:
            slist.append({'resource':VMResource().get_resource_uri(vms),'hostname':vms.hostname})
        bundle.data['primary_list'] = plist
        bundle.data['secondary_list'] = slist
        log = ganeti_web.views.node.object_log(bundle.request, node.cluster.slug, node.hostname, True)
        bundle.data['actions_on_node'] = api.utils.extract_log_actions(bundle.request, bundle.obj.id, log)

        return bundle


    def obj_get(self, request=None, **kwargs):
        node = super(NodeResource, self).obj_get(request, **kwargs)
        node_detail = ganeti_web.views.node.detail(request, node.cluster.slug, node.hostname, True)
        return node_detail['node']

    def build_schema(self):
        dict = super(NodeResource, self).build_schema()
        dict['fields']['cluster'] = { 'help_text': 'Cluster the node belongs to',
                                    'read_only' : True,'type': 'related', 'nullable':False }
        dict['fields']['node_count'] = { 'help_text': 'Number of the nodes in the cluster',
                                    'read_only' : True,'type': 'Integer', 'nullable':False }
        dict['fields']['Admin'] = { 'help_text': 'Determines if the user has admin status on the node',
                                    'read_only' : True,'type': 'boolean', 'nullable':False }
        dict['fields']['modify'] = { 'help_text': 'Determines if the user is able to modify node parameters',
                                    'read_only' : True,'type': 'boolean', 'nullable':False }
        dict['fields']['info'] = { 'help_text': 'This complex field returns various information related to the node.',
                                    'read_only' : True,'type': 'list', 'nullable':False }
        dict['fields']['hostname'] = { 'help_text': 'Hostname of the node',
                                    'read_only' : True,'type': 'string', 'nullable':False }
        dict['fields']['primary_list'] = { 'help_text': 'List of virtual machines (primary node). Contains vm link (related) and hostname for particular object.',
                                    'read_only' : True,'type': 'list', 'nullable':True }
        dict['fields']['secondary_list'] = { 'help_text': 'List of virtual machines (secondary node). Contains vm link (related) and hostname for particular object.',
                                    'read_only' : True,'type': 'list', 'nullable':True }

        dict['fields']['actions_on_node'] = { 'help_text': 'Returns the actions done on the node. The list is composed of objects, containing elements as described here.',
                            'read_only': True,
                            'type': 'list',
                            'object' : {
                                'obj1': {'help_text':'Describes action object', 'read_only':True, 'type':'related', 'nullable':True},
                                'obj2': {'help_text':'Describes action object', 'read_only':True, 'type':'related', 'nullable':True},
                                'user': {'help_text':'User performed the action', 'read_only':True, 'type':'related', 'nullable':True},
                                'timestamp': {'help_text':'A date and time of action', 'read_only':True, 'type':'datetime', 'nullable':True},
                                'action_name': {'help_text':'Describes action name using internal descriptions', 'read_only':True, 'type':'string', 'nullable':True}
                            },
                            'nullable': False }
        #utils.generate_wiki_basic_table(dict['fields'])
        return dict


class VMResource(ModelResource):
    cluster = fields.ForeignKey('api.resources.ClusterResource', 'cluster', null=True)
    primary_node = fields.ForeignKey('api.resources.NodeResource', 'primary_node', null=True)
    secondary_node = fields.ForeignKey('api.resources.NodeResource', 'secondary_node', null=True)
    last_job = fields.ForeignKey('api.resources.JobResource', 'last_job', null=True)

    class Meta:
        queryset = VirtualMachine.objects.all()
        object_class = VirtualMachine
        resource_name = 'vm'
        allowed_methods = ['get','delete', 'post']
        fields = {'slug','cluster', 'id', 'ram','disk_size','hostname','operating_system', 'virtual_cpus','status', 'pending_delete', 'deleted'}
        authentication = ApiKeyAuthentication()
        authorization = DjangoAuthorization()

    def dehydrate(self, bundle):
        return bundle

    def obj_get(self, request=None, **kwargs):
        vm = super(VMResource, self).obj_get(request=request, **kwargs)
        vm_detail = ganeti_web.views.virtual_machine.detail(request, vm.cluster.slug, vm.hostname, True)
        return vm_detail['instance'] #TODO CHECK
        #return vm

    def obj_get_list(self, request=None, **kwargs):
        vms = list_(request, True)
        return vms

    def obj_delete(self, request, **kwargs):
        try:
            vm = self.obj_get(request, **kwargs)
        except NotFound:
            raise NotFound("Object not found")
        vm_detail = ganeti_web.views.virtual_machine.detail(request, vm.cluster.slug, vm.hostname, True)
        response = ganeti_web.views.virtual_machine.delete(request, vm.cluster.slug, vm_detail['instance'], True)
        return response

    def get_detail(self, request, **kwargs):
        try:
            obj = self.cached_obj_get(request=request, **self.remove_api_resource_names(kwargs))
        except ObjectDoesNotExist:
            return HttpNotFound()
        except MultipleObjectsReturned:
            return HttpMultipleChoices("More than one resource is found at this URI.")

        bundle = self.build_bundle(obj=obj, request=request)
        bundle = self.full_dehydrate(bundle)
        bundle = self.alter_detail_data_to_serialize(request, bundle)

        vm = bundle.obj
        vm_detail = ganeti_web.views.virtual_machine.detail(bundle.request, vm.cluster.slug, vm.hostname, True)
        bundle.data['admin'] = vm_detail['admin']
        log = ganeti_web.views.virtual_machine.object_log(bundle.request, vm.cluster.slug, vm_detail['instance'], True)
        ##obj_res_instances = {'VirtualMachine':VMResource, 'User':UserResource, 'Group':GroupResource, 'Cluster':ClusterResource, 'Node':NodeResource, 'Job':JobResource}
        bundle.data['actions_on_vm'] = api.utils.extract_log_actions(bundle.request, bundle.obj.id, log)

        bundle.data['cluster_admin'] = vm_detail['cluster_admin']
        bundle.data['remove'] = vm_detail['remove']
        bundle.data['power'] = vm_detail['power']
        bundle.data['modify'] = vm_detail['modify']
        bundle.data['migrate'] = vm_detail['migrate']
        permissions = {'users':[], 'groups':[]}

        if vm_detail.has_key('job'):
            bundle.data['job'] = vm_detail['job']
            if vm_detail['job'] is not None:
                bundle.data['job'] = JobResource().get_resource_uri(vm_detail['job'])

        perms = ganeti_web.views.virtual_machine.users(bundle.request, vm.cluster.slug, vm_detail['instance'], rest = True)

        if perms['users'].__len__() > 0:
            for user in perms['users']:
                permissions['users'].append(UserResource().get_resource_uri(user))

        bundle.data['permissions'] = permissions

        return self.create_response(request, bundle)

    def post_detail(self, request, **kwargs):

        try:
            deserialized = self.deserialize(request, request.raw_post_data, format=request.META.get('CONTENT_TYPE', 'application/json'))
        except Exception:
            return HttpBadRequest()
        try:
            vm = self.obj_get(request,id=kwargs.get('pk'))
            vm_detail = ganeti_web.views.virtual_machine.detail(request, vm.cluster.slug, vm.hostname, True)
        except NotFound:
            return utils.serialize_and_reply(request, "Could not find object", 404)

        deserialized = self.alter_deserialized_detail_data(request, deserialized)

        # action: instance reboot
        if (deserialized.has_key('action')) & (deserialized.get('action')=='reboot'):
            response = ganeti_web.views.virtual_machine.reboot(request, vm.cluster.slug, vm_detail['instance'], True)
            return response

        # action: instance rename
        if (deserialized.has_key('action')) & (deserialized.get('action')=='rename'):

            if (deserialized.has_key('hostname')) & (deserialized.has_key('ip_check')) & (deserialized.has_key('name_check')):
                extracted_params = {'hostname':deserialized.get('hostname'), 'ip_check':deserialized.get('ip_check'), 'name_check':deserialized.get('name_check')}
            else:
                return HttpBadRequest
            response = ganeti_web.views.virtual_machine.rename(request, vm.cluster.slug, vm_detail['instance'], True, extracted_params)
            return response

        # action: instance startup
        if (deserialized.has_key('action')) & (deserialized.get('action')=='startup'):
            try:
                response = ganeti_web.views.virtual_machine.startup(request, vm.cluster.slug, vm_detail['instance'], True)
            except Http404:
                return utils.serialize_and_reply(request, "Could not find resource", code=404)
            return utils.serialize_and_reply(request, response['msg'], code=response['code']) #SERIALIZATION

        # action: instance shutdown
        if (deserialized.has_key('action')) & (deserialized.get('action')=='shutdown'):
            try:
                response = ganeti_web.views.virtual_machine.shutdown(request, vm.cluster.slug, vm_detail['instance'], True)
            except Http404:
                return utils.serialize_and_reply(request, "Could not find resource", code=404)
            return utils.serialize_and_reply(request, response['msg'], code=response['code'])

        return HttpAccepted

    def post_list(self, request, **kwargs):
        try:
            deserialized = self.deserialize(request, request.raw_post_data, format=request.META.get('CONTENT_TYPE', 'application/json'))
        except Exception:
            return HttpBadRequest()
        deserialized = self.alter_deserialized_detail_data(request, deserialized)


        # TODO: move detail and list code in separate place and call it from both detail/list functions
        if deserialized.has_key('action'):
            if deserialized.has_key('id'):
                try:
                    vm = self.obj_get(request,id=deserialized.get('id'))
                    vm_detail = ganeti_web.views.virtual_machine.detail(request, vm.cluster.slug, vm.hostname, True)
                except NotFound:
                    raise NotFound("Object not found")
                # reboot instance
                if deserialized.get('action')=='reboot':
                    response = ganeti_web.views.virtual_machine.reboot(request, vm.cluster.slug, vm_detail['instance'], True)
                    return response

                # rename instance
                if (deserialized.has_key('hostname')) & (deserialized.has_key('ip_check')) & (deserialized.has_key('name_check')):
                    extracted_params = {'hostname':deserialized.get('hostname'), 'ip_check':deserialized.get('ip_check'), 'name_check':deserialized.get('name_check')}
                else:
                    return HttpBadRequest
                response = ganeti_web.views.virtual_machine.rename(request, vm.cluster.slug, vm_detail['instance'], True, extracted_params)
                return response

    def build_schema(self):
        dict = super(VMResource, self).build_schema()
        dict['fields']['admin'] = { 'help_text': 'Determines if the current user has admin permissions over vm.',
                                    'read_only' : True,'type': 'Boolean', 'nullable':False }
        dict['fields']['actions_on_vm'] = { 'help_text': 'Returns the actions done on the user. The list is composed of objects, containing elements as described here.',
                                    'read_only': True,
                                    'type': 'list',
                                    'object' : {
                                        'obj1': {'help_text':'Describes action object', 'read_only':True, 'type':'related', 'nullable':True},
                                        'obj2': {'help_text':'Describes action object', 'read_only':True, 'type':'related', 'nullable':True},
                                        'user': {'help_text':'User performed the action', 'read_only':True, 'type':'related', 'nullable':True},
                                        'timestamp': {'help_text':'A date and time of action', 'read_only':True, 'type':'datetime', 'nullable':True},
                                        'action_name': {'help_text':'Describes action name using internal descriptions', 'read_only':True, 'type':'string', 'nullable':True}
                                    },
                                    'nullable': True }
        dict['fields']['cluster_admin'] = { 'help_text': 'Determines if the current user has admin permissions over cluster.',
                                    'read_only' : True,'type': 'Boolean', 'nullable':False }
        dict['fields']['remove'] = { 'help_text': 'Determines if the current user has permissions to remove vm.',
                                    'read_only' : True,'type': 'Boolean', 'nullable':False }
        dict['fields']['power'] = { 'help_text': 'Determines if the current user has admin permissions to power vm.',
                                    'read_only' : True,'type': 'Boolean', 'nullable':False }
        dict['fields']['modify'] = { 'help_text': 'Determines if the current user has admin permissions to modify vm.',
                                    'read_only' : True,'type': 'Boolean', 'nullable':False }
        dict['fields']['migrate'] = { 'help_text': 'Determines if the current user has admin permissions to migrate.',
                                    'read_only' : True,'type': 'Boolean', 'nullable':False }
        dict['fields']['permissions'] = { 'help_text': 'Lists the objects (users and groups) having permissions over vm. Contains sublists users and groups, each having objects pointing to related user/group.',
                                    'read_only' : True,'type': 'list', 'nullable':False }
        dict['fields']['job'] = { 'help_text': 'Points to the jobs related to the vm, if any.',
                                    'read_only' : True,'type': 'Boolean', 'nullable':True }

        #utils.generate_wiki_basic_table(dict['fields'])
        return dict

    
class JobResource(ModelResource):
    cluster = fields.ForeignKey(ClusterResource, 'cluster')

    def dehydrate(self, bundle):
        job = bundle.obj
        job_detail = ganeti_web.views.jobs.detail(bundle.request, job.cluster.slug, job.job_id, True)
        if bundle.obj.info is not None:
            try:
                if len(bundle.obj.info['opresult'][0][1])>1:
                    locError = {'error_type':bundle.obj.info['opresult'][0][0],'error_message':bundle.obj.info['opresult'][0][1][0], 'error_family':bundle.obj.info['opresult'][0][1][1]}
                else:
                    locError = {'error_type':bundle.obj.info['opresult'][0][0],'error_message':bundle.obj.info['opresult'][0][1][0]}
                bundle.data['opresult'] = locError
            except Exception:
                bundle.data['opresult'] = {}

            try:
                bundle.data['summary'] = bundle.obj.info['summary'][0]
            except Exception:
                bundle.data['summary'] = {}
            bundle.data['ops'] = bundle.obj.info['ops']
        bundle.data['cluster_admin'] = job_detail['cluster_admin']
        return bundle

    class Meta:
        queryset = Job.objects.all()
        object_class = Job
        resource_name = 'job'
        allowed_methods = ['get', 'delete']
        fields = {'status', 'finished', 'job_id', 'cleared'}
        authentication = ApiKeyAuthentication()
        authorization = DjangoAuthorization()

    def obj_get(self, request, **kwargs):
        job = super(JobResource, self).obj_get(request, **kwargs)
        job_status = status(request, job.cluster.slug, job.job_id, True)
        return job_status

    def obj_delete(self, request, **kwargs):
        try:
            job = self.obj_get(request, **kwargs)
        except NotFound:
            raise NotFound("Object not found")
        print job.info
        res = clear(request, job.cluster.slug, job.job_id, True)
        if res:
            job.delete()

    def build_schema(self):
        dict = super(JobResource, self).build_schema()
        dict['fields']['cluster_admin'] = { 'help_text': 'Determines if the current user has admin permissions over related cluster.',
                                    'read_only' : True,'type': 'Boolean', 'nullable':False }
        dict['fields']['opresult'] = { 'help_text': 'Describes the error occurred during job execution.',
                                    'error_type':{ 'help_text': 'Describes the error type', 'read_only' : True,'type': 'string', 'nullable':False},
                                    'error_message':{ 'help_text': 'Error message', 'read_only' : True,'type': 'string', 'nullable':True},
                                    'error_family':{ 'help_text': 'Error family', 'read_only' : True,'type': 'string', 'nullable':True},
                                    'read_only' : True,'type': 'list', 'nullable':True }
        dict['fields']['summarys'] = { 'help_text': 'Describes the job summary.',
                                    'read_only' : True,'type': 'string', 'nullable':True }
        dict['fields']['ops'] = { 'help_text': 'Complex field containing details about job. The field contents depend on the job type. More details can be found in the wiki.',
                            'read_only' : True,'type': 'list', 'nullable':False }
        utils.generate_wiki_basic_table(dict['fields'])
        return dict
