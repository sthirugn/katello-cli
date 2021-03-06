#
# Katello Organization actions
# Copyright 2013 Red Hat, Inc.
#
# This software is licensed to you under the GNU General Public License,
# version 2 (GPLv2). There is NO WARRANTY for this software, express or
# implied, including the implied warranties of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. You should have received a copy of GPLv2
# along with this software; if not, see
# http://www.gnu.org/licenses/old-licenses/gpl-2.0.txt.
#
# Red Hat trademarks are not licensed under GPLv2. No permission is
# granted to use or replicate Red Hat trademarks that are incorporated
# in this software or its documentation.
#

import os
from optparse import OptionValueError

from katello.client import constants
from katello.client.api.changeset import ChangesetAPI
from katello.client.cli.base import opt_parser_add_org, opt_parser_add_environment
from katello.client.core.base import BaseAction, Command

from katello.client.api.utils import get_environment, get_changeset, get_content_view
from katello.client.lib.async import AsyncTask, evaluate_task_status
from katello.client.lib.ui.progress import run_spinner_in_bg, wait_for_async_task
from katello.client.lib.utils.data import test_record
from katello.client.lib.ui.formatters import format_date
from katello.client.lib.ui import printer
from katello.client.lib.utils.encoding import u_str
from katello.client.lib.ui.printer import batch_add_columns

# base changeset action ========================================================
class ChangesetAction(BaseAction):
    def __init__(self):
        super(ChangesetAction, self).__init__()
        self.api = ChangesetAPI()

# ==============================================================================
class List(ChangesetAction):
    description = _('list new changesets of an environment')

    def setup_parser(self, parser):
        opt_parser_add_org(parser, required=1)
        opt_parser_add_environment(parser, required=1)

    def check_options(self, validator):
        validator.require(('org', 'environment'))

    def run(self):
        orgName = self.get_option('org')
        envName = self.get_option('environment')
        verbose = self.get_option('verbose')

        env = get_environment(orgName, envName)
        changesets = self.api.changesets(orgName, env['id'])


        batch_add_columns(self.printer, {'id': _("ID")}, {'name': _("Name")}, {'action_type': _("Action Type")})
        self.printer.add_column('updated_at', _("Last Updated"), formatter=format_date)
        batch_add_columns(self.printer, {'state': _("State")}, \
            {'environment_id': _("Environment ID")}, {'environment_name': _("Environment Name")})
        if verbose:
            self.printer.add_column('description', _("Description"), multiline=True)

        self.printer.set_header(_("Changeset List"))
        self.printer.print_items(changesets)
        return os.EX_OK


# ==============================================================================
class Info(ChangesetAction):
    description = _('detailed information about a changeset')

    def setup_parser(self, parser):
        opt_parser_add_org(parser, required=1)
        opt_parser_add_environment(parser, required=1)
        parser.add_option('--name', dest='name', help=_("changeset name (required)"))
        parser.add_option('--dependencies', dest='deps', action='store_true',
                               help=_("will display dependent packages"))

    def check_options(self, validator):
        validator.require(('org', 'name', 'environment'))

    @classmethod
    def format_item_list(cls, key, items):
        return "\n".join([i[key] for i in items])

    def get_dependencies(self, cset_id):
        deps = self.api.dependencies(cset_id)
        return self.format_item_list('display_name', deps)

    def run(self):
        orgName = self.get_option('org')
        envName = self.get_option('environment')
        csName = self.get_option('name')
        displayDeps = self.has_option('deps')

        cset = get_changeset(orgName, envName, csName)

        cset['environment_name'] = envName

        cset["content_views"] = self.format_item_list("name", cset["content_views"])
        if displayDeps:
            cset["dependencies"] = self.get_dependencies(cset["id"])
        batch_add_columns(self.printer, {'id': _("ID")}, {'name': _("Name")}, {'action_type': _("Action Type")})
        self.printer.add_column('description', _("Description"), multiline=True, show_with=printer.VerboseStrategy)
        self.printer.add_column('updated_at', _("Last Updated"), formatter=format_date)
        batch_add_columns(self.printer, {'state': _("State")}, \
            {'environment_id': _("Environment ID")}, {'environment_name': _("Environment Name")})
        batch_add_columns(self.printer, {'content_views': _("Content Views")},
            multiline=True, show_with=printer.VerboseStrategy)
        if displayDeps:
            self.printer.add_column('dependencies', _("Dependencies"), \
                multiline=True, show_with=printer.VerboseStrategy)

        self.printer.set_header(_("Changeset Info"))
        self.printer.print_item(cset)

        return os.EX_OK


# ==============================================================================
class Create(ChangesetAction):
    description = _('create a new changeset for an environment')

    def setup_parser(self, parser):
        opt_parser_add_org(parser, required=1)
        opt_parser_add_environment(parser, required=1)
        parser.add_option('--name', dest='name',
                               help=_("changeset name (required)"))
        parser.add_option('--description', dest='description',
                               help=_("changeset description"))
        parser.add_option('--promotion', dest='type_promotion', action="store_true", default=False,
                               help=constants.OPT_HELP_PROMOTION)
        parser.add_option('--deletion', dest='type_deletion', action="store_true", default=False,
                               help=constants.OPT_ERR_PROMOTION_OR_DELETE)



    def check_options(self, validator):
        validator.require(('org', 'name', 'environment'))

    def run(self):
        orgName = self.get_option('org')
        envName = self.get_option('environment')
        csName = self.get_option('name')
        csDescription = self.get_option('description')
        csType = constants.PROMOTION

        # Check for duplicate type flags
        if self.get_option('type_promotion') and self.get_option('type_deletion'):
            raise OptionValueError(constants.OPT_ERR_PROMOTION_OR_DELETE)
        if self.get_option('type_promotion'):
            csType = constants.PROMOTION
        elif self.get_option('type_deletion'):
            csType = constants.DELETION

        env = get_environment(orgName, envName)
        cset = self.api.create(orgName, env["id"], csName, csType, csDescription)
        test_record(cset,
            _("Successfully created changeset [ %(csName)s ] for environment [ %(env_name)s ]")
                % {'csName':csName, 'env_name':env["name"]},
            _("Could not create changeset [ %(csName)s ] for environment [ %(env_name)s ]")
                % {'csName':csName, 'env_name':env["name"]}
        )

        return os.EX_OK


# ==============================================================================
class UpdateContent(ChangesetAction):
    class PatchBuilder(object):
        @staticmethod
        def build_patch(action, itemBuilder, items):
            patch = {}
            patch['content_views'] = [itemBuilder.content_view(i) for i in (
                items[action + "_content_view"] + items[action + "_content_view_label"] +
                items[action + "_content_view_id"])]
            return patch

    class PatchItemBuilder(object):
        def __init__(self, org_name, env_name, type_in):
            self.org_name = org_name
            self.env_name = env_name
            self.type = type_in
            # Use current env if we are doing a deletion otherwise use the prior
            if self.type == 'deletion':
                self.env_name = get_environment(org_name, env_name)['name']
            else:
                self.env_name = get_environment(org_name, env_name)['prior']

        def content_view_id(self, options):
            view = get_content_view(self.org_name, **options)
            return view['id']

    class AddPatchItemBuilder(PatchItemBuilder):
        def content_view(self, options):
            return {
                'content_view_id': self.content_view_id(options)
            }


    class RemovePatchItemBuilder(PatchItemBuilder):
        def content_view(self, options):
            return {
                'content_id': self.content_view_id(options)
            }


    content_types = ['content_view', 'content_view_id', 'content_view_label']

    description = _('updates content of a changeset')

    def __init__(self):
        super(UpdateContent, self).__init__()
        self.items = {}


    # pylint: disable=W0613
    def _store_item(self, option, opt_str, value, parser):
        if option.dest == "add_content_view" or option.dest == "remove_content_view":
            self.items[option.dest].append({"view_name": u_str(value)})
        elif option.dest == "add_content_view_label" or option.dest == "remove_content_view_label":
            self.items[option.dest].append({"view_label": u_str(value)})
        elif option.dest == "add_content_view_id" or option.dest == "remove_content_view_id":
            self.items[option.dest].append({"view_id": u_str(value)})
        else:
            self.items[option.dest].append({"name": u_str(value)})

        setattr(parser.values, option.dest, value)

    def setup_parser(self, parser):
        parser.add_option('--name', dest='name',
                               help=_("changeset name (required)"))
        opt_parser_add_org(parser, required=1)
        opt_parser_add_environment(parser, required=1)
        parser.add_option('--description', dest='description',
                               help=_("changeset description"))
        parser.add_option('--new_name', dest='new_name',
                               help=_("new changeset name"))

        parser.add_option('--add_content_view', dest='add_content_view', type="string",
                               action="callback", callback=self._store_item,
                               help=_("name of a content view to be added to the changeset"))
        parser.add_option('--add_content_view_label', dest='add_content_view_label', type="string",
                               action="callback", callback=self._store_item,
                               help=_("label of a content view to be added to the changeset"))
        parser.add_option('--add_content_view_id', dest='add_content_view_id', type="string",
                               action="callback", callback=self._store_item,
                               help=_("label of a content view to be added to the changeset"))

        parser.add_option('--remove_content_view', dest='remove_content_view', type="string",
                               action="callback", callback=self._store_item,
                               help=_("name of a content view to be removed from the changeset"))
        parser.add_option('--remove_content_view_label', dest='remove_content_view_label', type="string",
                               action="callback", callback=self._store_item,
                               help=_("label of a content view to be removed from the changeset"))
        parser.add_option('--remove_content_view_id', dest='remove_content_view_id', type="string",
                               action="callback", callback=self._store_item,
                               help=_("id of a content view to be removed from the changeset"))

        self.reset_items()

    def reset_items(self):
        self.items = {}
        for ct in self.content_types:
            self.items['add_' + ct] = []
            self.items['remove_'+ct] = []

    def check_options(self, validator):
        validator.require(('name', 'org', 'environment'))
        validator.mutually_exclude('add_content_view', 'add_content_view_label',
                                   'add_content_view_id')
        validator.mutually_exclude('remove_content_view', 'remove_content_view_label',
                                   'remove_content_view_id')

    def run(self):
        #reset stored patch items (neccessary for shell mode)
        items = self.items.copy()
        self.reset_items()

        csName = self.get_option('name')
        orgName = self.get_option('org')
        envName = self.get_option('environment')
        csNewName = self.get_option('new_name')
        csDescription = self.get_option('description')

        cset = get_changeset(orgName, envName, csName)
        csType = cset['action_type']

        self.update(cset["id"], csNewName, csDescription)
        addPatch = self.PatchBuilder.build_patch('add',
            self.AddPatchItemBuilder(orgName, envName, csType), items)
        removePatch = self.PatchBuilder.build_patch('remove',
            self.RemovePatchItemBuilder(orgName, envName, csType), items)

        self.update_content(cset["id"], addPatch, self.api.add_content)
        self.update_content(cset["id"], removePatch, self.api.remove_content)

        print _("Successfully updated changeset [ %s ]") % csName
        return os.EX_OK


    def update(self, csId, newName, description):
        self.api.update(csId, newName, description)


    # pylint: disable=R0201
    def update_content(self, csId, patch, updateMethod):
        for contentType, items in patch.iteritems():
            for i in items:
                updateMethod(csId, contentType, i)


# ==============================================================================
class Delete(ChangesetAction):
    description = _('deletes a changeset')

    def setup_parser(self, parser):
        parser.add_option('--name', dest='name',
                               help=_("changeset name (required)"))
        opt_parser_add_org(parser, required=1)
        opt_parser_add_environment(parser, required=1)

    def check_options(self, validator):
        validator.require(('name', 'org', 'environment'))

    def run(self):
        csName = self.get_option('name')
        orgName = self.get_option('org')
        envName = self.get_option('environment')

        cset = get_changeset(orgName, envName, csName)

        msg = self.api.delete(cset["id"])
        print msg
        return os.EX_OK


# ==============================================================================
class Apply(ChangesetAction):
    description = _('applies a changeset based on the type (promotion, deletion)')

    def setup_parser(self, parser):
        parser.add_option('--name', dest='name',
                               help=_("changeset name (required)"))
        opt_parser_add_org(parser, required=1)
        opt_parser_add_environment(parser, required=1)

    def check_options(self, validator):
        validator.require(('name', 'org', 'environment'))

    def run(self):
        csName = self.get_option('name')
        orgName = self.get_option('org')
        envName = self.get_option('environment')

        cset = get_changeset(orgName, envName, csName)

        task = self.api.apply(cset["id"])
        task = AsyncTask(task)

        run_spinner_in_bg(wait_for_async_task, [task], message=_("Applying the changeset, please wait... "))

        return evaluate_task_status(task,
            failed = _("Changeset [ %s ] promotion failed") % csName,
            ok =     _("Changeset [ %s ] applied") % csName
        )

# ==============================================================================
class Promote(Apply):
    description = _('promotes a changeset to the next environment - DEPRECATED')

    def run(self):
        csName = self.get_option('name')
        orgName = self.get_option('org')
        envName = self.get_option('environment')

        # Block attempts to call this on deletion changesets, otherwise continue
        cset = get_changeset(orgName, envName, csName)
        if 'type' in cset and cset['type'] == constants.DELETION:
            print _("This is a deletion changeset and does not support promotion")
            return os.EX_DATAERR

        super(Promote, self).run()



# changeset command ============================================================
class Changeset(Command):
    description = _('changeset specific actions in the katello server')