# Copyright (C) 2010 Oregon State University et al.
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

from django import forms
from django.contrib.formtools.wizard.views import CookieWizardView
from django.core.urlresolvers import reverse
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db.models import Q
from django.forms import (Form, BooleanField, CharField, ChoiceField,
                          IntegerField, ModelChoiceField, ValidationError)
from django.http import HttpResponseRedirect
# Per #6579, do not change this import without discussion.
from django.utils import simplejson as json
from django.utils.translation import ugettext_lazy as _

from object_log.models import LogItem
log_action = LogItem.objects.log_action

from ganeti_web.backend.queries import (cluster_qs_for_user,
                                        owner_qs_for_cluster)
from ganeti_web.backend.templates import template_to_instance
from ganeti_web.caps import has_cdrom2, requires_maxmem
from ganeti_web.constants import (EMPTY_CHOICE_FIELD, HV_DISK_TEMPLATES,
                                  HV_NIC_MODES, KVM_CHOICES, HV_USB_MICE,
                                  HV_SECURITY_MODELS, KVM_FLAGS,
                                  HV_DISK_CACHES, MODE_CHOICES, HVM_CHOICES)
from ganeti_web.fields import DataVolumeField, MACAddressField
from ganeti_web.models import (Cluster, ClusterUser, Node,
                               VirtualMachineTemplate, VirtualMachine)
from ganeti_web.utilities import (cluster_default_info, cluster_os_list,
                                  get_hypervisor, hv_prettify)
from ganeti_web.util.client import (REPLACE_DISK_AUTO, REPLACE_DISK_PRI,
                                    REPLACE_DISK_CHG, REPLACE_DISK_SECONDARY)
from ganeti_web.views.generic import LoginRequiredMixin

username_or_mtime = Q(username='') | Q(mtime__isnull=True)

class VirtualMachineForm(forms.ModelForm):
    """
    Parent class that holds all vm clean methods
      and shared form fields.
    """
    memory = DataVolumeField(label=_('Memory'), min_value=100)

    class Meta:
        model = VirtualMachineTemplate

    def create_disk_fields(self, count):
        """
        dynamically add fields for disks
        """
        self.disk_fields = range(count)
        for i in range(count):
            disk_size = DataVolumeField(min_value=100, required=True,
                                        label=_("Disk/%s Size" % i))
            self.fields['disk_size_%s'%i] = disk_size

    def create_nic_fields(self, count, defaults=None):
        """
        dynamically add fields for nics
        """
        self.nic_fields = range(count)
        for i in range(count):
            nic_mode = forms.ChoiceField(label=_('NIC/%s Mode' % i), choices=HV_NIC_MODES)
            nic_link = forms.CharField(label=_('NIC/%s Link' % i), max_length=255)
            if defaults is not None:
                nic_link.initial = defaults['nic_link']
            self.fields['nic_mode_%s'%i] = nic_mode
            self.fields['nic_link_%s'%i] = nic_link

    def clean_hostname(self):
        data = self.cleaned_data
        hostname = data.get('hostname')
        cluster = data.get('cluster')
        if hostname and cluster:
            # Verify that this hostname is not in use for this cluster.  It can
            # only be reused when recovering a VM that failed to deploy.
            #
            # Recoveries are only allowed when the user is the owner of the VM
            try:
                vm = VirtualMachine.objects.get(cluster=cluster, hostname=hostname)

                # detect vm that failed to deploy
                if not vm.pending_delete and vm.template is not None:
                    current_owner = vm.owner.cast()
                    if current_owner == self.owner:
                        data['vm_recovery'] = vm
                    else:
                        msg = _("Owner cannot be changed when recovering a failed deployment")
                        self._errors["owner"] = self.error_class([msg])
                else:
                    raise ValidationError(_("Hostname is already in use for this cluster"))

            except VirtualMachine.DoesNotExist:
                # doesn't exist, no further checks needed
                pass

        # Spaces in hostname will always break things. 
        if ' ' in hostname:
            self.errors["hostname"] = self.error_class(
                ["Hostname contains illegal character"])
        return hostname

    def clean_vcpus(self):
        vcpus = self.cleaned_data.get("vcpus", None)

        if vcpus is not None and vcpus < 1:
            self._errors["vcpus"] = self.error_class(
                ["At least one CPU must be present"])
        else:
            return vcpus

    def clean_initrd_path(self):
        data = self.cleaned_data['initrd_path']
        if data and not data.startswith('/') and data != 'no_initrd_path':
            msg = u"%s." % _('This field must start with a "/"')
            self._errors['initrd_path'] = self.error_class([msg])
        return data

    def clean_security_domain(self):
        data = self.cleaned_data['security_domain']
        security_model = self.cleaned_data['security_model']
        msg = None

        if data and security_model != 'user':
            msg = u'%s.' % _(
                'This field can not be set if Security Mode is not set to User')
        elif security_model == 'user':
            if not data:
                msg = u'%s.' % _('This field is required')
            elif not data[0].isalpha():
                msg = u'%s.' % _('This field must being with an alpha character')

        if msg:
            self._errors['security_domain'] = self.error_class([msg])
        return data

    def clean_vnc_x509_path(self):
        data = self.cleaned_data['vnc_x509_path']
        if data and not data.startswith('/'):
            msg = u'%s,' % _('This field must start with a "/"')
            self._errors['vnc_x509_path'] = self.error_class([msg])
        return data


def check_quota_modify(form):
    """ method for validating user is within their quota when modifying """
    data = form.cleaned_data
    cluster = form.cluster
    owner = form.owner
    vm = form.vm

    # check quota
    if owner is not None:
        start = data['start']
        quota = cluster.get_quota(owner)
        if quota.values():
            used = owner.used_resources(cluster, only_running=True)

            if (start and quota['ram'] is not None and
                (used['ram'] + data['memory']-vm.ram) > quota['ram']):
                    del data['memory']
                    q_msg = u"%s" % _("Owner does not have enough ram remaining on this cluster. You must reduce the amount of ram.")
                    form._errors["ram"] = form.error_class([q_msg])

            if 'disk_size' in data and data['disk_size']:
                if quota['disk'] and used['disk'] + data['disk_size'] > quota['disk']:
                    del data['disk_size']
                    q_msg = u"%s" % _("Owner does not have enough diskspace remaining on this cluster.")
                    form._errors["disk_size"] = form.error_class([q_msg])

            if (start and quota['virtual_cpus'] is not None and
                (used['virtual_cpus'] + data['vcpus'] - vm.virtual_cpus) >
                quota['virtual_cpus']):
                    del data['vcpus']
                    q_msg = u"%s" % _("Owner does not have enough virtual cpus remaining on this cluster. You must reduce the amount of virtual cpus.")
                    form._errors["vcpus"] = form.error_class([q_msg])


class ModifyVirtualMachineForm(VirtualMachineForm):
    """
    Base modify class.
        If hvparam_fields (itirable) set on child, then
        each field on the form will be initialized to the
        value in vm.info.hvparams
    """
    always_required = ('vcpus', 'memory')
    empty_field = EMPTY_CHOICE_FIELD

    nic_count = forms.IntegerField(initial=1, widget=forms.HiddenInput())
    os = forms.ChoiceField(label=_('Operating System'), choices=[empty_field])

    class Meta:
        model = VirtualMachineTemplate
        exclude = ('start', 'owner', 'cluster', 'hostname', 'name_check',
        'iallocator', 'iallocator_hostname', 'disk_template', 'pnode', 'nics',
        'snode','disk_size', 'nic_mode', 'template_name', 'hypervisor', 'disks',
        'description', 'no_install')

    def __init__(self, vm, initial=None, *args, **kwargs):
        super(VirtualMachineForm, self).__init__(initial, *args, **kwargs)

        # Set owner on form
        try:
            self.owner
        except AttributeError:
            self.owner = vm.owner

        # Setup os choices
        os_list = cluster_os_list(vm.cluster)
        self.fields['os'].choices = os_list

        for field in self.always_required:
            self.fields[field].required = True
        # If the required property is set on a child class,
        #  require those form fields   
        try:
            if self.required:
                for field in self.required:
                    self.fields[field].required = True
        except AttributeError:
            pass

        # Need to set initial values from vm.info as these are not saved
        #  per the vm model.
        if vm.info:
            info = vm.info
            hvparam = info['hvparams']
            # XXX Convert ram string since it comes out
            #  from ganeti as an int and the DataVolumeField does not like
            #  ints.
            self.fields['vcpus'].initial = info['beparams']['vcpus']
            self.fields['memory'].initial = str(info['beparams']['memory'])

            # always take the larger nic count.  this ensures that if nics are
            # being removed that they will be in the form as Nones
            self.nics = len(info['nic.links'])
            nic_count = int(initial.get('nic_count', 1)) if initial else 1
            nic_count = self.nics if self.nics > nic_count else nic_count
            self.fields['nic_count'].initial = nic_count
            self.nic_fields = xrange(nic_count)
            for i in xrange(nic_count):
                link = forms.CharField(label=_('NIC/%s Link' % i), max_length=255, required=True)
                self.fields['nic_link_%s' % i] = link
                mac = MACAddressField(label=_('NIC/%s Mac' % i), required=True)
                self.fields['nic_mac_%s' % i] = mac
                if i < self.nics:
                    mac.initial = info['nic.macs'][i]
                    link.initial = info['nic.links'][i]

            self.fields['os'].initial = info['os']
            
            try:
                if self.hvparam_fields:
                    for field in self.hvparam_fields:
                        self.fields[field].initial = hvparam.get(field)
            except AttributeError:
                pass
            
    def clean(self):
        data = self.cleaned_data
        kernel_path = data.get('kernel_path')
        initrd_path = data.get('initrd_path')

        # Make sure if initrd_path is set, kernel_path is aswell
        if initrd_path and not kernel_path:
            msg = u"%s." % _("Kernel Path must be specified along with Initrd Path")
            self._errors['kernel_path'] = self.error_class([msg])
            self._errors['initrd_path'] = self.error_class([msg])
            del data['initrd_path']

        vnc_tls = data.get('vnc_tls')
        vnc_x509_path = data.get('vnc_x509_path')
        vnc_x509_verify = data.get('vnc_x509_verify')

        if not vnc_tls and vnc_x509_path:
            msg = u'%s.' % _('This field can not be set without VNC TLS enabled')
            self._errors['vnc_x509_path'] = self.error_class([msg])
        if vnc_x509_verify and not vnc_x509_path:
            msg = u'%s.' % _('This field is required')
            self._errors['vnc_x509_path'] = self.error_class([msg])

        if self.owner:
            data['start'] = 'reboot' in self.data or self.vm.is_running
            check_quota_modify(self)
            del data['start']

        for i in xrange(data['nic_count']):
            mac_field = 'nic_mac_%s' % i
            link_field = 'nic_link_%s' % i
            mac = data[mac_field] if mac_field in data else None
            link = data[link_field] if link_field in data else None
            if mac and not link:
                self._errors[link_field] = self.error_class([_('This field is required')])
            elif link and not mac:
                self._errors[mac_field] = self.error_class([_('This field is required')])
        data['nic_count_original'] = self.nics

        return data


class HvmModifyVirtualMachineForm(ModifyVirtualMachineForm):
    hvparam_fields = ('boot_order', 'cdrom_image_path', 'nic_type', 
        'disk_type', 'vnc_bind_address', 'acpi', 'use_localtime')
    required = ('disk_type', 'boot_order', 'nic_type')
    empty_field = EMPTY_CHOICE_FIELD
    disk_types = HVM_CHOICES['disk_type']
    nic_types = HVM_CHOICES['nic_type']
    boot_devices = HVM_CHOICES['boot_order']

    acpi = forms.BooleanField(label='ACPI', required=False)
    use_localtime = forms.BooleanField(label='Use Localtime', required=False)
    vnc_bind_address = forms.IPAddressField(label='VNC Bind Address',
        required=False)
    disk_type = forms.ChoiceField(label=_('Disk Type'), choices=disk_types)
    nic_type = forms.ChoiceField(label=_('NIC Type'), choices=nic_types)
    boot_order = forms.ChoiceField(label=_('Boot Device'), choices=boot_devices)

    class Meta(ModifyVirtualMachineForm.Meta):
        exclude = ModifyVirtualMachineForm.Meta.exclude + ('kernel_path', 
            'root_path', 'kernel_args', 'serial_console', 'cdrom2_image_path')

    def __init__(self, vm, *args, **kwargs):
        super(HvmModifyVirtualMachineForm, self).__init__(vm, *args, **kwargs)


class PvmModifyVirtualMachineForm(ModifyVirtualMachineForm):
    hvparam_fields = ('root_path', 'kernel_path', 'kernel_args', 
        'initrd_path')

    initrd_path = forms.CharField(label='initrd Path', required=False)
    kernel_args = forms.CharField(label='Kernel Args', required=False)

    class Meta(ModifyVirtualMachineForm.Meta):
        exclude = ModifyVirtualMachineForm.Meta.exclude + ('disk_type', 
            'nic_type', 'boot_order', 'cdrom_image_path', 'serial_console',
            'cdrom2_image_path')

    def __init__(self, vm, *args, **kwargs):
        super(PvmModifyVirtualMachineForm, self).__init__(vm, *args, **kwargs)


class KvmModifyVirtualMachineForm(PvmModifyVirtualMachineForm,
                                  HvmModifyVirtualMachineForm):
    hvparam_fields = ('acpi', 'disk_cache', 'initrd_path', 
        'kernel_args', 'kvm_flag', 'mem_path', 
        'migration_downtime', 'security_domain', 
        'security_model', 'usb_mouse', 'use_chroot', 
        'use_localtime', 'vnc_bind_address', 'vnc_tls', 
        'vnc_x509_path', 'vnc_x509_verify', 'disk_type', 
        'boot_order', 'nic_type', 'root_path', 
        'kernel_path', 'serial_console', 
        'cdrom_image_path',
        'cdrom2_image_path',
    )
    disk_caches = HV_DISK_CACHES
    kvm_flags = KVM_FLAGS
    security_models = HV_SECURITY_MODELS
    usb_mice = HV_USB_MICE
    disk_types = KVM_CHOICES['disk_type']
    nic_types = KVM_CHOICES['nic_type']
    boot_devices = KVM_CHOICES['boot_order']

    disk_cache = forms.ChoiceField(label='Disk Cache', required=False,
        choices=disk_caches)
    kvm_flag = forms.ChoiceField(label='KVM Flag', required=False,
        choices=kvm_flags)
    mem_path = forms.CharField(label='Mem Path', required=False)
    migration_downtime = forms.IntegerField(label='Migration Downtime',
        required=False)
    security_model = forms.ChoiceField(label='Security Model',
        required=False, choices=security_models)
    security_domain = forms.CharField(label='Security Domain', required=False)
    usb_mouse = forms.ChoiceField(label='USB Mouse', required=False,
        choices=usb_mice)
    use_chroot = forms.BooleanField(label='Use Chroot', required=False)
    vnc_tls = forms.BooleanField(label='VNC TLS', required=False)
    vnc_x509_path = forms.CharField(label='VNC x509 Path', required=False)
    vnc_x509_verify = forms.BooleanField(label='VNC x509 Verify',
        required=False)
    
    class Meta(ModifyVirtualMachineForm.Meta):
        pass
    
    def __init__(self, vm, *args, **kwargs):
        super(KvmModifyVirtualMachineForm, self).__init__(vm, *args, **kwargs)
        self.fields['disk_type'].choices = self.disk_types
        self.fields['nic_type'].choices = self.nic_types
        self.fields['boot_order'].choices = self.boot_devices
    

class ModifyConfirmForm(forms.Form):

    def clean(self):
        raw = self.data['rapi_dict']
        data = json.loads(raw)

        cleaned = self.cleaned_data
        cleaned['rapi_dict'] = data

        # XXX copy properties into cleaned data so that check_quota_modify can
        # be used
        cleaned['memory'] = data['memory']
        cleaned['vcpus'] = data['vcpus']
        cleaned['start'] = 'reboot' in data or self.vm.is_running
        check_quota_modify(self)

        # Build NICs dicts.  Add changes for existing nics and mark new or
        # removed nics
        #
        # XXX Ganeti only allows a single remove or add but this code will
        # format properly for unlimited adds or removes in the hope that this
        # limitation is removed sometime in the future.
        nics = []
        nic_count_original = data.pop('nic_count_original')
        nic_count = data.pop('nic_count')
        for i in xrange(nic_count):
            nic = dict(link=data.pop('nic_link_%s' % i))
            if 'nic_mac_%s' % i in data:
                nic['mac'] = data.pop('nic_mac_%s' % i)
            index = i if i < nic_count_original else 'add'
            nics.append((index, nic))
        for i in xrange(nic_count_original-nic_count):
            nics.append(('remove',{}))
            try:
                del data['nic_mac_%s' % (nic_count+i)]
            except KeyError:
                pass
            del data['nic_link_%s' % (nic_count+i)]
            
        data['nics'] = nics
        return cleaned


class MigrateForm(forms.Form):
    """ Form used for migrating a Virtual Machine """
    mode = forms.ChoiceField(choices=MODE_CHOICES)
    cleanup = forms.BooleanField(initial=False, required=False,
                                 label=_("Attempt recovery from failed migration"))


class RenameForm(forms.Form):
    """ form used for renaming a Virtual Machine """
    hostname = forms.CharField(label=_('Instance Name'), max_length=255,
                               required=True)
    ip_check = forms.BooleanField(initial=True, required=False, label=_('IP Check'))
    name_check = forms.BooleanField(initial=True, required=False, label=_('DNS Name Check'))

    def __init__(self, vm, *args, **kwargs):
        self.vm = vm
        super(RenameForm, self).__init__(*args, **kwargs)

    def clean_hostname(self):
        data = self.cleaned_data
        hostname = data.get('hostname', None)
        if hostname and hostname == self.vm.hostname:
            raise ValidationError(_("The new hostname must be different than the current hostname"))
        return hostname


class ChangeOwnerForm(forms.Form):
    """ Form used when modifying the owner of a virtual machine """
    owner = forms.ModelChoiceField(queryset=ClusterUser.objects.all(), label=_('Owner'))


class ReplaceDisksForm(forms.Form):
    """
    Form used when replacing disks for a virtual machine
    """
    empty_field = EMPTY_CHOICE_FIELD

    MODE_CHOICES = (
        (REPLACE_DISK_AUTO, _('Automatic')),
        (REPLACE_DISK_PRI, _('Replace disks on primary')),
        (REPLACE_DISK_SECONDARY, _('Replace disks secondary')),
        (REPLACE_DISK_CHG, _('Replace secondary with new disk')),
    )

    mode = forms.ChoiceField(choices=MODE_CHOICES, label=_('Mode'))
    disks = forms.MultipleChoiceField(label=_('Disks'), required=False)
    node = forms.ChoiceField(label=_('Node'), choices=[empty_field], required=False)
    iallocator = forms.BooleanField(initial=False, label=_('Iallocator'), required=False)
    
    def __init__(self, instance, *args, **kwargs):
        super(ReplaceDisksForm, self).__init__(*args, **kwargs)
        self.instance = instance

        # set disk choices based on the instance
        disk_choices = [(i, 'disk/%s' % i) for i,v in enumerate(instance.info['disk.sizes'])]
        self.fields['disks'].choices = disk_choices

        # set choices based on the instances cluster
        cluster = instance.cluster
        nodelist = [str(h) for h in cluster.nodes.values_list('hostname', flat=True)]
        nodes = zip(nodelist, nodelist)
        nodes.insert(0, self.empty_field)
        self.fields['node'].choices = nodes

        defaults = cluster_default_info(cluster, get_hypervisor(instance))
        if defaults['iallocator'] != '' :
            self.fields['iallocator'].initial = True
            self.fields['iallocator_hostname'] = forms.CharField(
                                    initial=defaults['iallocator'],
                                    required=False,
                                    widget = forms.HiddenInput())
    
    def clean(self):
        data = self.cleaned_data
        mode = data.get('mode')
        if mode == REPLACE_DISK_CHG:
            iallocator = data.get('iallocator')
            node = data.get('node')
            if not (iallocator or node):
                msg = _('Node or iallocator is required when replacing secondary with new disk')
                self._errors['mode'] = self.error_class([msg])

            elif iallocator and node:
                msg = _('Choose either node or iallocator')
                self._errors['mode'] = self.error_class([msg])
                
        return data

    def clean_disks(self):
        """ format disks into a comma delimited string """
        disks = self.cleaned_data.get('disks')
        if disks is not None:
            disks = ','.join(disks)
        return disks

    def clean_node(self):
        node = self.cleaned_data.get('node')
        return node if node else None

    def save(self):
        """
        Start a replace disks job using the data in this form.
        """
        data = self.cleaned_data
        mode = data['mode']
        disks = data['disks']
        node = data['node']
        if data['iallocator']:
            iallocator = data['iallocator_hostname']
        else:
            iallocator = None
        return self.instance.replace_disks(mode, disks, node, iallocator)


class VMWizardClusterForm(Form):
    cluster = ModelChoiceField(label=_('Cluster'),
                               queryset=Cluster.objects.all(),
                               empty_label=None)

    def _configure_for_user(self, user):
        self.fields["cluster"].queryset = cluster_qs_for_user(user)

    def clean_cluster(self):
        """
        Ensure that the cluster is available.
        """

        cluster = self.cleaned_data.get('cluster', None)
        if not getattr(cluster, "info", None):
            msg = _("This cluster is currently unavailable. Please check"
                    " for Errors on the cluster detail page.")
            self._errors['cluster'] = self.error_class([msg])

        return cluster


class VMWizardOwnerForm(Form):
    template_name = CharField(label=_("Template Name"), max_length=255,
                              required=False)
    hostname = CharField(label=_('Instance Name'), max_length=255,
                         required=False)
    owner = ModelChoiceField(label=_('Owner'),
                             queryset=ClusterUser.objects.all(),
                             empty_label=None)

    def _configure_for_cluster(self, cluster):
        if not cluster:
            return

        self.cluster = cluster

        qs = owner_qs_for_cluster(cluster)
        self.fields["owner"].queryset = qs

    def _configure_for_template(self, template):
        if not template:
            return

        self.fields["template_name"].initial = template.template_name

    def clean_hostname(self):
        hostname = self.cleaned_data.get('hostname')
        if hostname:
            # Confirm that the hostname is not already in use.
            try:
                vm = VirtualMachine.objects.get(cluster=self.cluster,
                                                hostname=hostname)
            except VirtualMachine.DoesNotExist:
                # Well, *I'm* convinced.
                pass
            else:
                raise ValidationError(
                    _("Hostname is already in use for this cluster"))

        # Spaces in hostname will always break things.
        if ' ' in hostname:
            self.errors["hostname"] = self.error_class(
                ["Hostnames cannot contain spaces."])
        return hostname

    def clean(self):
        if (not self.cleaned_data.get("template_name") and
            not self.cleaned_data.get("hostname")):
            raise ValidationError("What should be created?")
        return self.cleaned_data


class VMWizardBasicsForm(Form):
    hv = ChoiceField(label=_("Hypervisor"), choices=[])
    os = ChoiceField(label=_('Operating System'), choices=[])
    vcpus = IntegerField(label=_("Virtual CPU Count"), initial=1, min_value=1)
    memory = DataVolumeField(label=_('Memory (MiB)'))
    disk_template = ChoiceField(label=_('Disk Template'),
                                choices=HV_DISK_TEMPLATES)
    disk_size = DataVolumeField(label=_("Disk Size (MB)"))

    def _configure_for_cluster(self, cluster):
        if not cluster:
            return

        self.cluster = cluster

        # Get a look at the list of available hypervisors, and set the initial
        # hypervisor appropriately.
        hvs = cluster.info["enabled_hypervisors"]
        prettified = [hv_prettify(hv) for hv in hvs]
        hv = cluster.info["default_hypervisor"]
        self.fields["hv"].choices = zip(hvs, prettified)
        self.fields["hv"].initial = hv

        # Get the OS list.
        self.fields["os"].choices = cluster_os_list(cluster)

        # Set the default CPU count based on the backend parameters.
        beparams = cluster.info["beparams"]["default"]
        self.fields["vcpus"].initial = beparams["vcpus"]

        # If this cluster operates on the "maxmem" parameter instead of
        # "memory", use that for now.
        if requires_maxmem(cluster):
            self.fields["memory"].initial = beparams["maxmem"]
        else:
            self.fields["memory"].initial = beparams["memory"]

        # If there are ipolicy limits in place, add validators for them.
        if "ipolicy" in cluster.info:
            if "max" in cluster.info["ipolicy"]:
                v = cluster.info["ipolicy"]["max"]["disk-size"]
                self.fields["disk_size"].validators.append(
                    MaxValueValidator(v))
                v = cluster.info["ipolicy"]["max"]["memory-size"]
                self.fields["memory"].validators.append(MaxValueValidator(v))
            if "min" in cluster.info["ipolicy"]:
                v = cluster.info["ipolicy"]["min"]["disk-size"]
                self.fields["disk_size"].validators.append(
                    MinValueValidator(v))
                v = cluster.info["ipolicy"]["min"]["memory-size"]
                self.fields["memory"].validators.append(MinValueValidator(v))

    def _configure_for_template(self, template):
        if not template:
            return

        self.fields["os"].initial = template.os
        self.fields["vcpus"].initial = template.vcpus
        self.fields["memory"].initial = template.memory
        self.fields["disk_template"].initial = template.disk_template
        # XXX disk size


class VMWizardAdvancedForm(Form):
    ip_check = BooleanField(label=_('Verify IP'), initial=False,
                            required=False)
    name_check = BooleanField(label=_('Verify hostname through DNS'),
                              initial=False, required=False)
    pnode = ModelChoiceField(label=_("Primary Node"),
                             queryset=Node.objects.all(), empty_label=None)
    snode = ModelChoiceField(label=_("Secondary Node"),
                             queryset=Node.objects.all(), empty_label=None)

    def _configure_for_cluster(self, cluster):
        if not cluster:
            return

        self.cluster = cluster

        qs = Node.objects.filter(cluster=cluster)
        self.fields["pnode"].queryset = qs
        self.fields["snode"].queryset = qs

    def _configure_for_template(self, template):
        if not template:
            return

        self.fields["ip_check"].initial = template.ip_check
        self.fields["name_check"].initial = template.name_check
        self.fields["pnode"].initial = template.pnode
        self.fields["snode"].initial = template.snode

    def _configure_for_disk_template(self, template):
        if template != "drbd":
            del self.fields["snode"]

    def clean(self):
        # Ganeti will error on VM creation if an IP address check is requested
        # but a name check is not.
        if (self.cleaned_data.get("ip_check") and not
            self.cleaned_data.get("name_check")):
            msg = ["Cannot perform IP check without name check"]
            self.errors["ip_check"] = self.error_class(msg)

        return self.cleaned_data


class VMWizardPVMForm(Form):
    kernel_path = CharField(label=_("Kernel path"), max_length=255)
    root_path = CharField(label=_("Root path"), max_length=255)

    def _configure_for_cluster(self, cluster):
        if not cluster:
            return

        self.cluster = cluster
        params = cluster.info["hvparams"]["xen-pvm"]

        self.fields["kernel_path"].initial = params["kernel_path"]
        self.fields["root_path"].initial = params["root_path"]

    def _configure_for_template(self, template):
        if not template:
            return

        self.fields["kernel_path"].initial = template.kernel_path
        self.fields["root_path"].initial = template.root_path


class VMWizardHVMForm(Form):
    boot_order = CharField(label=_("Preferred boot device"), max_length=255,
                           required=False)
    cdrom_image_path = CharField(label=_("CD-ROM image path"), max_length=512,
                                required=False)
    disk_type = ChoiceField(label=_("Disk type"),
                            choices=HVM_CHOICES["disk_type"])
    nic_type = ChoiceField(label=_("NIC type"),
                           choices=HVM_CHOICES["nic_type"])

    def _configure_for_cluster(self, cluster):
        if not cluster:
            return

        self.cluster = cluster
        params = cluster.info["hvparams"]["xen-pvm"]

        self.fields["boot_order"].initial = params["boot_order"]
        self.fields["disk_type"].initial = params["disk_type"]
        self.fields["nic_type"].initial = params["nic_type"]

    def _configure_for_template(self, template):
        if not template:
            return

        self.fields["boot_order"].initial = template.boot_order
        self.fields["cdrom_image_path"].initial = template.cdrom_image_path
        self.fields["disk_type"].initial = template.disk_type
        self.fields["nic_type"].initial = template.nic_type



class VMWizardKVMForm(Form):
    kernel_path = CharField(label=_("Kernel path"), max_length=255)
    root_path = CharField(label=_("Root path"), max_length=255)
    serial_console = BooleanField(label=_("Enable serial console"),
                                  required=False)
    boot_order = CharField(label=_("Preferred boot device"), max_length=255,
                           required=False)
    cdrom_image_path = CharField(label=_("CD-ROM image path"), max_length=512,
                                required=False)
    cdrom2_image_path = CharField(label=_("Second CD-ROM image path"),
                                  max_length=512, required=False)
    disk_type = ChoiceField(label=_("Disk type"),
                            choices=KVM_CHOICES["disk_type"])
    nic_type = ChoiceField(label=_("NIC type"),
                           choices=KVM_CHOICES["nic_type"])

    def _configure_for_cluster(self, cluster):
        if not cluster:
            return

        self.cluster = cluster
        params = cluster.info["hvparams"]["kvm"]

        self.fields["boot_order"].initial = params["boot_order"]
        self.fields["disk_type"].initial = params["disk_type"]
        self.fields["kernel_path"].initial = params["kernel_path"]
        self.fields["nic_type"].initial = params["nic_type"]
        self.fields["root_path"].initial = params["root_path"]
        self.fields["serial_console"].initial = params["serial_console"]

        # Remove cdrom2 if the cluster doesn't have it; see #11655.
        if not has_cdrom2(cluster):
            del self.fields["cdrom2_image_path"]

    def _configure_for_template(self, template):
        if not template:
            return

        self.fields["kernel_path"].initial = template.kernel_path
        self.fields["root_path"].initial = template.root_path
        self.fields["serial_console"].initial = template.serial_console
        self.fields["boot_order"].initial = template.boot_order
        self.fields["cdrom_image_path"].initial = template.cdrom_image_path
        self.fields["cdrom2_image_path"].initial = template.cdrom2_image_path
        self.fields["disk_type"].initial = template.disk_type
        self.fields["nic_type"].initial = template.nic_type

    def clean(self):
        data = super(VMWizardKVMForm, self).clean()

        # Force cdrom disk type to IDE; see #9297.
        data['cdrom_disk_type'] = 'ide'

        # If booting from CD-ROM, require the first CD-ROM image to be
        # present.
        if (data.get("boot_order") == "cdrom" and
            not data.get("cdrom_image_path")):
            msg = u"%s." % _("Image path required if boot device is CD-ROM")
            self._errors["cdrom_image_path"] = self.error_class([msg])

        return data


class VMWizardView(LoginRequiredMixin, CookieWizardView):
    template_name = "ganeti/forms/vm_wizard.html"

    def _get_template(self):
        name = self.kwargs.get("template")
        if name:
            return VirtualMachineTemplate.objects.get(template_name=name)
        return None

    def _get_cluster(self):
        data = self.get_cleaned_data_for_step("0")
        if data:
            return data["cluster"]
        return None

    def _get_hv(self):
        data = self.get_cleaned_data_for_step("2")
        if data:
            return data["hv"]
        return None

    def _get_disk_template(self):
        data = self.get_cleaned_data_for_step("2")
        if data:
            return data["disk_template"]
        return None

    def get_form(self, step=None, data=None, files=None):
        s = int(self.steps.current) if step is None else int(step)

        if s == 0:
            form = VMWizardClusterForm(data=data)
            form._configure_for_user(self.request.user)
            # XXX this should somehow become totally invalid if the user
            # doesn't have perms on the template.
        elif s == 1:
            form = VMWizardOwnerForm(data=data)
            form._configure_for_cluster(self._get_cluster())
            form._configure_for_template(self._get_template())
        elif s == 2:
            form = VMWizardBasicsForm(data=data)
            form._configure_for_cluster(self._get_cluster())
            form._configure_for_template(self._get_template())
        elif s == 3:
            form = VMWizardAdvancedForm(data=data)
            form._configure_for_cluster(self._get_cluster())
            form._configure_for_template(self._get_template())
            form._configure_for_disk_template(self._get_disk_template())
        elif s == 4:
            cluster = self._get_cluster()
            hv = self._get_hv()
            form = None

            if cluster and hv:
                if hv == "kvm":
                    form = VMWizardKVMForm(data=data)
                elif hv == "xen-pvm":
                    form = VMWizardPVMForm(data=data)
                elif hv == "xen-hvm":
                    form = VMWizardHVMForm(data=data)

            if form:
                form._configure_for_cluster(cluster)
                form._configure_for_template(self._get_template())
            else:
                form = Form()
        else:
            form = super(VMWizardView, self).get_form(step, data, files)

        return form

    def get_context_data(self, form, **kwargs):
        context = super(VMWizardView, self).get_context_data(form=form,
                                                             **kwargs)
        summary = {
            "cluster": self._get_cluster(),
            "owner_form": self.get_cleaned_data_for_step("1"),
            "basics_form": self.get_cleaned_data_for_step("2"),
            "advanced_form": self.get_cleaned_data_for_step("3"),
            "hv_form": self.get_cleaned_data_for_step("4"),
        }
        context["summary"] = summary

        return context

    def done(self, forms, template=None, **kwargs):
        """
        Create a template. Optionally, bind a template to a VM instance
        created from the template. Optionally, name the template and save it.
        One or both of those is done depending on what the user has requested.
        """

        # Hack: accepting kwargs in order to be able to work in several
        # different spots.

        if template is None:
            template = VirtualMachineTemplate()
        else:
            template = self._get_template()

        user = self.request.user

        cluster = forms[0].cleaned_data["cluster"]
        owner = forms[1].cleaned_data["owner"]
        hostname = forms[1].cleaned_data["hostname"]
        template_name = forms[1].cleaned_data["template_name"]

        template.cluster = cluster
        template.memory = forms[2].cleaned_data["memory"]
        template.vcpus = forms[2].cleaned_data["vcpus"]
        template.disk_template = forms[2].cleaned_data["disk_template"]

        disk_size = forms[2].cleaned_data["disk_size"]
        template.disks = [
            {
                "size": disk_size,
            },
        ]

        template.nics = [
            {
                "link": "br0",
                "mode": "bridged",
            },
        ]

        template.os = forms[2].cleaned_data["os"]
        template.ip_check = forms[3].cleaned_data["ip_check"]
        template.name_check = forms[3].cleaned_data["name_check"]
        template.pnode = forms[3].cleaned_data["pnode"].hostname

        hvparams = forms[4].cleaned_data

        template.boot_order = hvparams.get("boot_order")
        template.cdrom2_image_path = hvparams.get("cdrom2_image_path")
        template.cdrom_image_path = hvparams.get("cdrom_image_path")
        template.kernel_path = hvparams.get("kernel_path")
        template.root_path = hvparams.get("root_path")
        template.serial_console = hvparams.get("serial_console")

        if "snode" in forms[3].cleaned_data:
            template.snode = forms[3].cleaned_data["snode"].hostname

        if template_name:
            template.template_name = template_name

        template.save()

        if hostname:
            vm = template_to_instance(template, hostname, owner)
            log_action('CREATE', user, vm)
            return HttpResponseRedirect(reverse('instance-detail',
                                                args=[cluster.slug,
                                                      vm.hostname]))
        else:
            return HttpResponseRedirect(reverse("template-detail",
                                                args=[cluster.slug,
                                                      template]))


def vm_wizard():
    forms = (
        VMWizardClusterForm,
        VMWizardOwnerForm,
        VMWizardBasicsForm,
        VMWizardAdvancedForm,
        Form,
    )
    return VMWizardView.as_view(forms)
